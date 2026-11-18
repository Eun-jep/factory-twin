"""시계열 저장소 (SQLite, 표준 라이브러리만 사용).

테이블
- samples  설비 측정값 (설비, 시각, 관절 각도, 단계, 카운트, 온도, 상태 ...)
- events   통신 이벤트 (접속, 끊김, 실패, 복구)
- alarms   알람 (발생, 확인, 해제 시각을 각각 기록)

알람 상태는 컬럼에서 계산한다 (alarm_state):
    해제 전·미확인  -> 해제 전·확인됨  -> 종료        (확인 후 해제)
    해제 전·미확인  -> 해제됨·미확인   -> 종료        (해제 후 확인)
확인(ACK)과 해제는 서로 독립이라, 값이 정상으로 돌아와도 사람이 확인하기 전에는 닫히지 않는다.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .models import Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY,
    device TEXT NOT NULL, ts REAL NOT NULL,
    joint1 REAL, joint2 REAL, gripper INTEGER, phase INTEGER, box INTEGER,
    count INTEGER, temp REAL, status INTEGER, cycle REAL, conveyor INTEGER
);
CREATE INDEX IF NOT EXISTS idx_samples_device_ts ON samples (device, ts);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL, device TEXT NOT NULL, kind TEXT NOT NULL, message TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);

CREATE TABLE IF NOT EXISTS alarms (
    id INTEGER PRIMARY KEY,
    device TEXT NOT NULL, kind TEXT NOT NULL, severity INTEGER NOT NULL,
    message TEXT, value REAL, last_value REAL,
    raised_at REAL NOT NULL,
    acked_at REAL, acked_by TEXT,
    cleared_at REAL,
    repeat_count INTEGER NOT NULL DEFAULT 1,
    last_raised_at REAL
);
CREATE INDEX IF NOT EXISTS idx_alarms_active ON alarms (device, kind, cleared_at);
"""


def count_delta(prev: int, new: int) -> int:
    """생산 카운트가 prev에서 new로 바뀌는 동안 늘어난 개수.

    카운트는 16비트 누적 값이라 두 가지 이유로 줄어들 수 있다.
    - 65536에서 0으로 한 바퀴 돎: 이어서 센다.
    - 설비(PLC)가 재시작해서 0부터 다시 시작: 재시작 후 센 개수(new)만 더한다.
    """
    if new >= prev:
        return new - prev
    if prev >= 0xF000 and new < 0x1000:      # 한 바퀴 돎
        return 0x10000 - prev + new
    return new                               # 재시작으로 초기화


def alarm_state(row) -> str:
    acked = row["acked_at"] is not None
    cleared = row["cleared_at"] is not None
    if not cleared:
        return "활성·확인됨" if acked else "활성·미확인"
    return "종료" if acked else "해제됨·미확인"


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
                self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(SCHEMA)
            self._migrate()
            self._db.commit()

    def _migrate(self) -> None:
        """이전 버전으로 만든 DB에 새 열을 추가한다. 기존 데이터는 그대로 둔다."""
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(alarms)")}
        if "repeat_count" not in cols:
            self._db.execute("ALTER TABLE alarms ADD COLUMN repeat_count INTEGER NOT NULL DEFAULT 1")
        if "last_raised_at" not in cols:
            self._db.execute("ALTER TABLE alarms ADD COLUMN last_raised_at REAL")
            self._db.execute("UPDATE alarms SET last_raised_at = raised_at WHERE last_raised_at IS NULL")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---- 측정값 -------------------------------------------------------------
    def add_sample(self, device: str, s: Snapshot) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO samples (device, ts, joint1, joint2, gripper, phase, box, count,"
                " temp, status, cycle, conveyor) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (device, s.timestamp, s.joint1_deg, s.joint2_deg, int(s.gripper_closed), s.phase,
                 int(s.box_detected), s.count, s.temp_c, s.status, s.cycle_s, s.conveyor_mm_s),
            )
            self._db.commit()

    def latest_samples(self, device: str, limit: int = 10) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM samples WHERE device=? ORDER BY ts DESC, id DESC LIMIT ?",
                (device, limit)).fetchall()

    def samples_between(self, device: str, t0: float, t1: float) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM samples WHERE device=? AND ts>=? AND ts<=? ORDER BY ts, id",
                (device, t0, t1)).fetchall()

    def first_sample_ts(self, device: str) -> float | None:
        """그 설비의 가장 오래된 측정 시각. 측정값이 없으면 None."""
        with self._lock:
            return self._db.execute("SELECT MIN(ts) FROM samples WHERE device=?", (device,)).fetchone()[0]

    def count_samples(self, device: str, t0: float, t1: float, *, status_not: int | None = None) -> int:
        """구간 [t0, t1]의 측정값 개수. status_not을 주면 그 상태가 아닌 것만 센다."""
        sql = "SELECT COUNT(*) FROM samples WHERE device=? AND ts>=? AND ts<=?"
        args: list = [device, t0, t1]
        if status_not is not None:
            sql += " AND status<>?"
            args.append(status_not)
        with self._lock:
            return self._db.execute(sql, args).fetchone()[0]

    def devices(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute(
                "SELECT DISTINCT device FROM samples ORDER BY device").fetchall()]

    def sample_count(self, device: str | None = None) -> int:
        with self._lock:
            if device is None:
                return self._db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
            return self._db.execute("SELECT COUNT(*) FROM samples WHERE device=?",
                                    (device,)).fetchone()[0]

    def production_between(self, device: str, t0: float, t1: float) -> int:
        """구간 [t0, t1] 동안 생산한 개수. 설비 재시작으로 카운트가 초기화돼도 맞게 센다."""
        with self._lock:
            prev = self._db.execute(
                "SELECT count FROM samples WHERE device=? AND ts<? ORDER BY ts DESC, id DESC LIMIT 1",
                (device, t0)).fetchone()
            rows = self._db.execute(
                "SELECT count FROM samples WHERE device=? AND ts>=? AND ts<=? ORDER BY ts, id",
                (device, t0, t1)).fetchall()
        last = prev["count"] if prev else None
        total = 0
        for r in rows:
            if last is not None:
                total += count_delta(last, r["count"])
            last = r["count"]
        return total

    # ---- 이벤트 -------------------------------------------------------------
    def add_event(self, ts: float, device: str, kind: str, message: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO events (ts, device, kind, message) VALUES (?,?,?,?)",
                             (ts, device, kind, message))
            self._db.commit()

    def recent_events(self, limit: int = 50) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()

    # ---- 보관 정책 ----------------------------------------------------------
    def prune_old(self, before_ts: float, limit: int = 5000) -> int:
        """before_ts보다 **오래된**(같은 시각은 남김) 측정값과 이벤트를 지운다. 설비마다, 이벤트는 따로 최대 limit건씩.
        지운 총 건수를 돌려준다 (0이면 더 지울 것이 없다). 알람은 지우지 않는다.
        설비별로 지우는 이유: 측정값 색인이 (설비, 시각) 순이라서, 설비를 지정해야 전체를 훑지 않고 빠르게 찾는다."""
        deleted = 0
        with self._lock:
            for (device,) in self._db.execute("SELECT DISTINCT device FROM samples").fetchall():
                cur = self._db.execute(
                    "DELETE FROM samples WHERE id IN "
                    "(SELECT id FROM samples WHERE device=? AND ts<? LIMIT ?)", (device, before_ts, limit))
                deleted += cur.rowcount
            cur = self._db.execute(
                "DELETE FROM events WHERE id IN (SELECT id FROM events WHERE ts<? LIMIT ?)", (before_ts, limit))
            deleted += cur.rowcount
            self._db.commit()
        return deleted

    # ---- 알람 ---------------------------------------------------------------
    def raise_alarm(self, device: str, kind: str, severity: int, message: str,
                    value: float | None, ts: float) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO alarms (device, kind, severity, message, value, last_value, raised_at,"
                " last_raised_at) VALUES (?,?,?,?,?,?,?,?)",
                (device, kind, severity, message, value, value, ts, ts))
            self._db.commit()
            return int(cur.lastrowid)

    def find_reopenable(self, device: str, kind: str, since: float) -> sqlite3.Row | None:
        """since 이후에 해제됐고 아직 아무도 확인하지 않은 같은 알람 중 가장 최근 것.
        이미 확인된 알람은 다시 열지 않는다 (확인한 사람이 재발생을 놓치면 안 되므로)."""
        with self._lock:
            return self._db.execute(
                "SELECT * FROM alarms WHERE device=? AND kind=? AND cleared_at IS NOT NULL"
                " AND acked_at IS NULL AND cleared_at>=? ORDER BY cleared_at DESC, id DESC LIMIT 1",
                (device, kind, since)).fetchone()

    def reopen_alarm(self, alarm_id: int, message: str, value: float | None, ts: float) -> bool:
        """해제된 알람을 다시 활성으로 돌리고 반복 횟수를 1 올린다. 최초 발생 시각은 그대로 둔다."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE alarms SET cleared_at=NULL, repeat_count=repeat_count+1, message=?,"
                " last_value=?, last_raised_at=? WHERE id=? AND cleared_at IS NOT NULL",
                (message, value, ts, alarm_id))
            self._db.commit()
            return cur.rowcount > 0

    def update_alarm_value(self, alarm_id: int, value: float | None) -> None:
        with self._lock:
            self._db.execute("UPDATE alarms SET last_value=? WHERE id=?", (value, alarm_id))
            self._db.commit()

    def clear_alarm(self, alarm_id: int, ts: float) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE alarms SET cleared_at=? WHERE id=? AND cleared_at IS NULL", (ts, alarm_id))
            self._db.commit()
            return cur.rowcount > 0

    def ack_alarm(self, alarm_id: int, by: str, ts: float) -> bool:
        """확인(ACK). 이미 확인된 알람이거나 없는 알람이면 False."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE alarms SET acked_at=?, acked_by=? WHERE id=? AND acked_at IS NULL",
                (ts, by, alarm_id))
            self._db.commit()
            return cur.rowcount > 0

    def get_alarm(self, alarm_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute("SELECT * FROM alarms WHERE id=?", (alarm_id,)).fetchone()

    def active_alarms(self) -> list[sqlite3.Row]:
        """아직 해제되지 않은 알람."""
        with self._lock:
            return self._db.execute(
                "SELECT * FROM alarms WHERE cleared_at IS NULL ORDER BY raised_at").fetchall()

    def open_alarms(self) -> list[sqlite3.Row]:
        """아직 닫히지 않은 알람 (해제 전이거나, 해제됐어도 미확인). 중요도 순."""
        with self._lock:
            return self._db.execute(
                "SELECT * FROM alarms WHERE NOT (acked_at IS NOT NULL AND cleared_at IS NOT NULL)"
                " ORDER BY severity, raised_at").fetchall()

    def all_alarms(self, limit: int = 100) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM alarms ORDER BY raised_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
