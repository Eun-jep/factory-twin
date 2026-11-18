"""통신 장애 시나리오 실측: 지연, 연결 끊김, 깨진 응답, 설비 정지와 재시작.

    python tools/comm_scenarios.py                 # 모든 시나리오 (약 3분)
    python tools/comm_scenarios.py --only drop     # 하나만

시나리오마다 **진짜** 가상 설비 프로세스(simulator/modbus_sim.py) 3대와 **진짜** 수집기(python -m collector)를 띄운다.
1번 설비(ARM-01)에만 장애를 주고 2·3번(ARM-02, 03)은 정상으로 두어서, 장애가 다른 설비로 번지지 않는지도 같이 본다.
결과는 수집기가 저장한 DB(측정값, 이벤트, 알람)에서 읽는다. 사용자가 쓰는 포트(5021~5026, 8000)와
data/factory.db는 건드리지 않고, 포트 7101~7103과 임시 DB를 쓴다.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "tools"))

from measure import gap_stats  # noqa: E402
from modbus_sim import port_in_use  # noqa: E402

PORTS = {"ARM-01": 7101, "ARM-02": 7102, "ARM-03": 7103}
FAULTY, HEALTHY = "ARM-01", ["ARM-02", "ARM-03"]
WARMUP = 3.0        # 접속이 끝날 때까지 기다리는 시간(초)

# (이름, 설명, 1번 설비에 줄 시뮬레이터 옵션, 측정 시간(초), 수집기 응답 제한 시간)
SCENARIOS = {
    "baseline": ("정상 (기준)", [], 20, 1.0),
    "delay": ("응답 지연 300ms (제한 시간 1.0초보다 짧음)", ["--delay-ms", "300"], 20, 1.0),
    "timeout": ("응답 지연 1500ms (제한 시간 1.0초보다 김)", ["--delay-ms", "1500"], 25, 1.0),
    "drop": ("7번째 요청마다 응답 없이 연결 끊김", ["--drop-every", "7"], 25, 1.0),
    "garbage": ("7번째 요청마다 깨진 응답", ["--garbage-every", "7"], 25, 1.0),
    "restart": ("설비 전원 꺼짐 -> 8초 뒤 다시 켜짐", [], 35, 1.0),
}


def start_sim(name: str, port: int, extra: list[str], seed: int, log_dir: Path) -> subprocess.Popen:
    cmd = [sys.executable, str(ROOT / "simulator" / "modbus_sim.py"), "--name", name, "--port", str(port),
           "--seed", str(seed), "--heat-rate", "0.3", *extra]
    return subprocess.Popen(cmd, cwd=ROOT, stdout=open(log_dir / f"{name}-{port}-{time.time():.0f}.log", "w"),
                            stderr=subprocess.STDOUT)


def wait_listening(port: int, timeout: float = 8.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if port_in_use("127.0.0.1", port):
            return
        time.sleep(0.1)
    raise RuntimeError(f"포트 {port}가 열리지 않았습니다")


def query_results(db_path: str, start: float, end: float) -> dict:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out = {"samples": {}, "events": {}, "alarms": [], "event_rows": []}
    for name in PORTS:
        out["samples"][name] = [r[0] for r in db.execute(
            "SELECT ts FROM samples WHERE device=? AND ts BETWEEN ? AND ? ORDER BY ts", (name, start, end))]
        out["events"][name] = dict(db.execute(
            "SELECT kind, COUNT(*) FROM events WHERE device=? AND ts BETWEEN ? AND ? GROUP BY kind", (name, start, end)).fetchall())
    out["alarms"] = db.execute(
        "SELECT device, kind, severity, raised_at, cleared_at, repeat_count FROM alarms ORDER BY raised_at").fetchall()
    out["event_rows"] = db.execute(
        "SELECT ts, device, kind FROM events WHERE ts BETWEEN ? AND ? ORDER BY ts", (start, end)).fetchall()
    db.close()
    return out


def run_scenario(key: str, workdir: Path, off_seconds: float = 8.0, backoff_max: float | None = None) -> dict:
    title, sim_args, duration, timeout = SCENARIOS[key]
    if key == "restart":
        title = f"설비 전원 꺼짐 -> {off_seconds:g}초 뒤 다시 켜짐"
        duration = 8 + off_seconds + 14                      # 8초 정상 수집 + 꺼진 시간 + 켠 뒤 14초
    for port in PORTS.values():
        deadline = time.monotonic() + 5                          # 앞 시나리오가 막 끝났다면 포트가 비워질 때까지 잠깐 기다린다
        while port_in_use("127.0.0.1", port) and time.monotonic() < deadline:
            time.sleep(0.2)
        if port_in_use("127.0.0.1", port):
            raise SystemExit(f"포트 {port}를 이미 다른 프로그램이 쓰고 있습니다. 시험 포트(7101~7103)를 비워 주세요.")
    log_dir = workdir / key
    log_dir.mkdir(parents=True)
    db_path = str(log_dir / "scenario.db")
    sims: dict[str, subprocess.Popen] = {}
    collector = None
    try:
        for i, (name, port) in enumerate(PORTS.items()):
            sims[name] = start_sim(name, port, sim_args if name == FAULTY else [], i + 1, log_dir)
        for port in PORTS.values():
            wait_listening(port)
        spec = ",".join(f"{n}:127.0.0.1:{p}" for n, p in PORTS.items())
        collector = subprocess.Popen(
            [sys.executable, "-m", "collector", "--devices", spec, "--db", db_path, "--no-web",
             "--timeout", str(timeout), "--report", "30"]
            + (["--backoff-max", str(backoff_max)] if backoff_max is not None else []),
            cwd=ROOT, stdout=open(log_dir / "collector.log", "w", encoding="utf-8"), stderr=subprocess.STDOUT)
        time.sleep(WARMUP)
        start = time.time()
        restart_info = {}
        if key == "restart":
            time.sleep(8)                                      # 8초 정상 수집 후 전원을 끈다
            restart_info["off_at"] = time.time()
            sims[FAULTY].terminate()
            sims[FAULTY].wait(timeout=5)
            time.sleep(off_seconds)                            # 꺼져 있는 시간
            sims[FAULTY] = start_sim(FAULTY, PORTS[FAULTY], [], 1, log_dir)
            wait_listening(PORTS[FAULTY])
            restart_info["on_at"] = time.time()
            time.sleep(max(0.0, duration - (time.time() - start)))
        else:
            time.sleep(duration)
        end = time.time()
        collector.terminate()
        collector.wait(timeout=10)
        result = query_results(db_path, start, end)
        result.update({"key": key, "title": title, "duration": end - start, "restart": restart_info, "start": start})
        return result
    finally:
        # 끝낸 뒤 프로세스가 완전히 사라질 때까지 기다린다 (다음 시나리오가 같은 포트를 쓰기 때문)
        for p in ([collector] if collector is not None else []) + list(sims.values()):
            if p.poll() is None:
                p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=5)


def summarize(r: dict) -> list[str]:
    lines = [f"\n## {r['title']}  ({r['duration']:.0f}초)"]
    for name in PORTS:
        ts = r["samples"][name]
        stats = gap_stats(ts)
        rate = len(ts) / r["duration"]
        tag = "장애 주입 대상" if name == FAULTY else "정상 설비"
        gap_text = "-" if stats is None else f"최대 공백 {stats['max']:.2f}초, 간격 중앙값 {stats['p50']:.3f}초"
        longest = None
        if ts:                                                  # 측정 시작부터 첫 값까지, 마지막 값부터 끝까지의 공백도 센다
            longest = max([ts[0] - r["start"], r["start"] + r["duration"] - ts[-1]] + ([stats["max"]] if stats else []))
        lines.append(f"- {name} ({tag}): 측정값 {len(ts)}개 (초당 {rate:.2f}개), {gap_text}"
                     + ("" if longest is None else f", 가장 긴 수집 공백 {longest:.1f}초")
                     + f", 이벤트 {r['events'][name] or '없음'}")
    if r["alarms"]:
        for device, kind, severity, raised, cleared, repeat in r["alarms"]:
            state = "해제됨" if cleared else "열려 있음"
            lines.append(f"- 알람: [{device}] {kind} P{severity} 발생 +{raised - r['start']:.1f}초, {state}"
                         + (f" (+{cleared - r['start']:.1f}초)" if cleared else "") + (f", 반복 {repeat}회" if repeat > 1 else ""))
    else:
        lines.append("- 알람: 없음")
    if r["key"] == "restart" and r["restart"]:
        off, on = r["restart"]["off_at"], r["restart"]["on_at"]
        after = [t for t in r["samples"][FAULTY] if t > on]
        failed = [row for row in r["event_rows"] if row[1] == FAULTY and row[2] in ("disconnected", "read_failed", "connect_failed") and row[0] >= off]
        detect = f"{failed[0][0] - off:.1f}초" if failed else "-"
        recover = f"{after[0] - on:.1f}초" if after else "복구 못 함"
        lines.append(f"- 끊김을 알아챈 시간: 전원이 꺼진 뒤 {detect} / 설비를 다시 켠 뒤 값이 다시 들어온 시간: {recover}")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description="통신 장애 시나리오 실측")
    parser.add_argument("--only", choices=sorted(SCENARIOS), default=None, help="이 시나리오 하나만")
    parser.add_argument("--off-seconds", type=float, default=8.0, help="restart 시나리오에서 설비가 꺼져 있는 시간(초)")
    parser.add_argument("--backoff-max", type=float, default=None,
                        help="수집기의 재접속 대기 상한(초). 주지 않으면 수집기의 기본값(4초). 이전 값(8초)과 비교할 때 쓴다")
    args = parser.parse_args()
    workdir = Path(tempfile.mkdtemp(prefix="factory-comm-"))
    keys = [args.only] if args.only else list(SCENARIOS)
    try:
        for key in keys:
            result = run_scenario(key, workdir, args.off_seconds, args.backoff_max)
            print("\n".join(summarize(result)), flush=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
