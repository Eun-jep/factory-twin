"""저장소 시험.

    python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import snap  # noqa: E402

from collector.store import Store, alarm_state, count_delta  # noqa: E402


class CountDeltaTest(unittest.TestCase):
    def test_cases(self):
        cases = [
            ("증가", 5, 8, 3),
            ("그대로", 8, 8, 0),
            ("16비트 한 바퀴", 65530, 3, 9),            # 65530 -> 65535 (5) -> 0 -> 3 : 6 + 3 = 9
            ("설비 재시작으로 초기화", 40, 3, 3),         # 재시작 후 센 3개만 더한다
            ("0으로 초기화", 100, 0, 0),
            ("한 바퀴 경계", 0xF000, 0x0FFF, 0x1000 + 0x0FFF),
            ("높은 값에서 높지 않은 값으로는 초기화", 0xF000, 0x1000, 0x1000),
        ]
        for label, prev, new, expected in cases:
            with self.subTest(label):
                self.assertEqual(count_delta(prev, new), expected)


class SamplesTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)

    def test_roundtrip_and_order(self):
        for i in range(5):
            self.store.add_sample("ARM-01", snap(100.0 + i, count=i, temp=40.0 + i))
        self.store.add_sample("ARM-02", snap(100.0, count=9))
        rows = self.store.latest_samples("ARM-01", 3)
        self.assertEqual([r["count"] for r in rows], [4, 3, 2])          # 최신 먼저
        self.assertEqual(rows[0]["temp"], 44.0)
        self.assertEqual(rows[0]["joint1"], 90.0)
        self.assertEqual(self.store.sample_count("ARM-01"), 5)
        self.assertEqual(self.store.sample_count(), 6)
        self.assertEqual(self.store.devices(), ["ARM-01", "ARM-02"])

    def test_samples_between_is_inclusive_and_ordered(self):
        for i in range(10):
            self.store.add_sample("ARM-01", snap(float(i), count=i))
        rows = self.store.samples_between("ARM-01", 3.0, 6.0)
        self.assertEqual([r["count"] for r in rows], [3, 4, 5, 6])

    def test_production_handles_restart_reset(self):
        for ts, count in [(1, 0), (2, 1), (3, 2), (4, 0), (5, 1)]:      # 4초에 설비 재시작
            self.store.add_sample("ARM-01", snap(float(ts), count=count))
        # 0->1(1) + 1->2(1) + 2->0 초기화(0) + 0->1(1) = 3
        self.assertEqual(self.store.production_between("ARM-01", 1.0, 5.0), 3)

    def test_production_reset_to_nonzero_value(self):
        # 재시작 직후 첫 읽기가 이미 몇 개를 센 뒤일 수 있다: 6 -> 2 는 "재시작 후 2개 생산"
        for ts, count in [(1, 5), (2, 6), (3, 2), (4, 3)]:
            self.store.add_sample("ARM-01", snap(float(ts), count=count))
        self.assertEqual(self.store.production_between("ARM-01", 1.0, 4.0), 1 + 2 + 1)

    def test_production_includes_sample_before_window(self):
        for ts, count in [(1, 0), (2, 1), (3, 2), (4, 0), (5, 1)]:
            self.store.add_sample("ARM-01", snap(float(ts), count=count))
        # 구간 [3,5] 앞의 표본(2초, 카운트 1)에서 이어서 센다: 1->2(1) + 2->0(0) + 0->1(1) = 2
        self.assertEqual(self.store.production_between("ARM-01", 3.0, 5.0), 2)

    def test_production_handles_wraparound(self):
        for ts, count in [(1, 65534), (2, 65535), (3, 1)]:
            self.store.add_sample("ARM-01", snap(float(ts), count=count))
        self.assertEqual(self.store.production_between("ARM-01", 1.0, 3.0), 1 + 2)

    def test_production_of_empty_window_is_zero(self):
        self.assertEqual(self.store.production_between("ARM-01", 0.0, 10.0), 0)


class EventsTest(unittest.TestCase):
    def test_recent_events_newest_first(self):
        store = Store()
        self.addCleanup(store.close)
        store.add_event(1.0, "ARM-01", "connected", "접속됨")
        store.add_event(2.0, "ARM-02", "read_failed", "시간 초과")
        rows = store.recent_events(10)
        self.assertEqual([r["kind"] for r in rows], ["read_failed", "connected"])
        self.assertEqual(rows[0]["device"], "ARM-02")


class AlarmLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)

    def state(self, alarm_id):
        return alarm_state(self.store.get_alarm(alarm_id))

    def test_ack_then_clear(self):
        a = self.store.raise_alarm("ARM-01", "TEMP_WARNING", 2, "온도 주의", 66.0, 10.0)
        self.assertEqual(self.state(a), "활성·미확인")
        self.assertTrue(self.store.ack_alarm(a, "홍길동", 11.0))
        self.assertFalse(self.store.ack_alarm(a, "다른사람", 12.0))      # 이미 확인됨
        self.assertEqual(self.store.get_alarm(a)["acked_by"], "홍길동")   # 처음 확인한 사람이 남는다
        self.assertEqual(self.state(a), "활성·확인됨")
        self.assertTrue(self.store.clear_alarm(a, 13.0))
        self.assertFalse(self.store.clear_alarm(a, 14.0))                 # 이미 해제됨
        self.assertEqual(self.state(a), "종료")
        self.assertEqual(self.store.open_alarms(), [])

    def test_clear_then_ack(self):
        a = self.store.raise_alarm("ARM-01", "COUNTER_RESET", 3, "초기화", 0.0, 10.0)
        self.store.clear_alarm(a, 10.0)
        self.assertEqual(self.state(a), "해제됨·미확인")
        self.assertEqual([r["id"] for r in self.store.open_alarms()], [a])   # 아직 닫히지 않음
        self.assertTrue(self.store.ack_alarm(a, "홍길동", 20.0))
        self.assertEqual(self.state(a), "종료")
        self.assertEqual(self.store.open_alarms(), [])

    def test_open_alarms_sorted_by_severity(self):
        low = self.store.raise_alarm("ARM-01", "COUNTER_RESET", 3, "p3", None, 1.0)
        high = self.store.raise_alarm("ARM-02", "EQUIPMENT_FAULT", 1, "p1", None, 5.0)
        mid = self.store.raise_alarm("ARM-03", "TEMP_WARNING", 2, "p2", None, 3.0)
        self.assertEqual([r["id"] for r in self.store.open_alarms()], [high, mid, low])

    def test_find_reopenable_rules(self):
        a = self.store.raise_alarm("ARM-01", "TEMP_WARNING", 2, "x", 66.0, 1.0)
        self.assertIsNone(self.store.find_reopenable("ARM-01", "TEMP_WARNING", 0.0))   # 아직 활성
        self.store.clear_alarm(a, 10.0)
        self.assertEqual(self.store.find_reopenable("ARM-01", "TEMP_WARNING", 5.0)["id"], a)
        self.assertEqual(self.store.find_reopenable("ARM-01", "TEMP_WARNING", 10.0)["id"], a)   # 경계 포함
        self.assertIsNone(self.store.find_reopenable("ARM-01", "TEMP_WARNING", 10.5))  # 창 밖
        self.assertIsNone(self.store.find_reopenable("ARM-01", "EQUIPMENT_FAULT", 5.0))  # 다른 종류
        self.assertIsNone(self.store.find_reopenable("ARM-02", "TEMP_WARNING", 5.0))   # 다른 설비
        self.store.ack_alarm(a, "홍길동", 11.0)
        self.assertIsNone(self.store.find_reopenable("ARM-01", "TEMP_WARNING", 5.0))   # 확인된 알람

    def test_find_reopenable_picks_most_recent(self):
        old = self.store.raise_alarm("ARM-01", "TEMP_WARNING", 2, "old", None, 1.0)
        self.store.clear_alarm(old, 2.0)
        new = self.store.raise_alarm("ARM-01", "TEMP_WARNING", 2, "new", None, 3.0)
        self.store.clear_alarm(new, 4.0)
        self.assertEqual(self.store.find_reopenable("ARM-01", "TEMP_WARNING", 0.0)["id"], new)

    def test_reopen_alarm_updates_fields(self):
        a = self.store.raise_alarm("ARM-01", "TEMP_WARNING", 2, "처음", 66.0, 1.0)
        self.store.clear_alarm(a, 2.0)
        self.assertTrue(self.store.reopen_alarm(a, "다시", 70.0, 5.0))
        row = self.store.get_alarm(a)
        self.assertEqual(row["repeat_count"], 2)
        self.assertIsNone(row["cleared_at"])
        self.assertEqual(row["message"], "다시")
        self.assertEqual(row["last_raised_at"], 5.0)
        self.assertEqual((row["raised_at"], row["value"], row["last_value"]), (1.0, 66.0, 70.0))
        self.assertFalse(self.store.reopen_alarm(a, "또", 71.0, 6.0))       # 이미 활성이면 다시 열 수 없다
        self.assertEqual(self.store.get_alarm(a)["repeat_count"], 2)

    def test_ack_unknown_alarm_returns_false(self):
        self.assertFalse(self.store.ack_alarm(999, "홍길동", 1.0))
        self.assertIsNone(self.store.get_alarm(999))

    def test_active_alarms_excludes_cleared(self):
        a = self.store.raise_alarm("ARM-01", "TEMP_WARNING", 2, "x", None, 1.0)
        b = self.store.raise_alarm("ARM-02", "TEMP_WARNING", 2, "y", None, 2.0)
        self.store.clear_alarm(a, 3.0)
        self.assertEqual([r["id"] for r in self.store.active_alarms()], [b])


class MigrationTest(unittest.TestCase):
    OLD_SCHEMA = """
    CREATE TABLE alarms (
        id INTEGER PRIMARY KEY, device TEXT NOT NULL, kind TEXT NOT NULL, severity INTEGER NOT NULL,
        message TEXT, value REAL, last_value REAL, raised_at REAL NOT NULL,
        acked_at REAL, acked_by TEXT, cleared_at REAL);
    INSERT INTO alarms (device, kind, severity, message, value, last_value, raised_at, cleared_at)
        VALUES ('ARM-01', 'TEMP_WARNING', 2, '이전 버전 알람', 66.0, 67.0, 123.0, 130.0);
    """

    def test_old_database_is_upgraded_without_losing_data(self):
        import sqlite3
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            path = str(Path(d) / "old.db")
            raw = sqlite3.connect(path)
            raw.executescript(self.OLD_SCHEMA)
            raw.commit()
            raw.close()

            for _ in range(2):                                   # 두 번 열어도 문제없어야 한다
                store = Store(path)
                try:
                    row = store.get_alarm(1)
                    self.assertEqual(row["message"], "이전 버전 알람")   # 기존 데이터 보존
                    self.assertEqual(row["repeat_count"], 1)             # 새 열은 기본값
                    self.assertEqual(row["last_raised_at"], 123.0)       # 최초 발생 시각으로 채움
                    again = store.raise_alarm("ARM-02", "COMM_LOST", 1, "x", None, 200.0)
                    self.assertEqual(store.get_alarm(again)["repeat_count"], 1)
                    self.assertEqual(store.find_reopenable("ARM-01", "TEMP_WARNING", 100.0)["id"], 1)
                finally:
                    store.close()


class PersistenceTest(unittest.TestCase):
    def test_data_survives_reopen(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            path = str(Path(d) / "sub" / "t.db")              # 폴더가 없어도 만든다
            store = Store(path)
            store.add_sample("ARM-01", snap(1.0, count=7))
            alarm_id = store.raise_alarm("ARM-01", "TEMP_WARNING", 2, "x", 66.0, 1.0)
            store.close()

            again = Store(path)
            try:
                self.assertEqual(again.latest_samples("ARM-01", 1)[0]["count"], 7)
                self.assertEqual(again.get_alarm(alarm_id)["kind"], "TEMP_WARNING")
            finally:
                again.close()


if __name__ == "__main__":
    unittest.main()
