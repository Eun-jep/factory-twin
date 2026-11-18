"""가상 설비 N대를 동시에 수집하는 부하 시험.

    python tools/loadtest.py --devices 30 --duration 60
    python tools/loadtest.py --devices 100 --duration 40 --web      # 웹 방송까지 포함

구성
- 가상 설비 N대: 이 프로그램 안에서 SimServer N개를 띄운다 (포트는 운영체제가 배정). 설비마다 프로세스를 따로 띄우면
  이 PC에 부담이 크므로, 시험 대상이 아닌 쪽(설비)은 한 프로세스에 모았다.
- 수집 서버: **진짜** `python -m collector`를 별도 프로세스로 띄운다 (시험 대상). 임시 DB를 쓴다.
- 측정: 수집기의 CPU와 메모리를 1초마다 재고, 끝난 뒤 DB의 측정값 시각으로 수집 간격(공백)을 분석한다.
  --web을 주면 웹 방송을 받는 WebSocket 접속자 한 명을 붙여 메시지 수와 크기도 잰다.

사용자가 쓰는 포트(5021~5026, 8000)와 data/factory.db는 건드리지 않는다.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "simulator"))
sys.path.insert(0, str(ROOT / "tools"))

from measure import ProcessMeter, free_port, gap_stats, percentile, ws_collect  # noqa: E402
from modbus_sim import SimServer  # noqa: E402
from robot_sim import RobotSim  # noqa: E402


def analyze_db(db_path: str, names: list[str], start: float, end: float) -> dict:
    """측정 구간 [start, end]의 DB 내용을 분석한다 (읽기 전용으로 연다)."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    per_device = {}
    all_gaps: list[float] = []
    total = 0
    for name in names:
        ts = [r[0] for r in db.execute(
            "SELECT ts FROM samples WHERE device=? AND ts BETWEEN ? AND ? ORDER BY ts", (name, start, end))]
        total += len(ts)
        stats = gap_stats(ts)
        per_device[name] = {"samples": len(ts), "gaps": stats}
        if len(ts) > 1:
            all_gaps.extend(b - a for a, b in zip(ts, ts[1:]))
    events = dict(db.execute("SELECT kind, COUNT(*) FROM events WHERE ts BETWEEN ? AND ? GROUP BY kind", (start, end)).fetchall())
    alarms = db.execute("SELECT COUNT(*) FROM alarms").fetchone()[0]
    db.close()
    return {"total_samples": total, "per_device": per_device, "all_gaps": all_gaps, "events": events, "alarms": alarms}


def run_load(devices: int = 30, duration: float = 60.0, warmup: float = 5.0, interval: float = 0.5,
             web: bool = False, workdir: str | None = None) -> dict:
    """부하 시험을 한 번 돌리고 결과를 dict로 돌려준다."""
    own_dir = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="factory-load-")
    db_path = str(Path(workdir) / "load.db")
    names = [f"SIM-{i + 1:03d}" for i in range(devices)]
    sims = [SimServer(RobotSim(n, seed=i + 1, heat_rate=0.3), "127.0.0.1", 0) for i, n in enumerate(names)]
    proc = None
    log_file = None
    try:
        for s in sims:
            s.start()
        spec = ",".join(f"{n}:127.0.0.1:{s.port}" for n, s in zip(names, sims))
        cmd = [sys.executable, "-m", "collector", "--devices", spec, "--db", db_path,
               "--interval", str(interval), "--report", "5"]
        web_port = None
        if web:
            web_port = free_port()
            cmd += ["--web-port", str(web_port)]
        else:
            cmd += ["--no-web"]
        log_file = open(Path(workdir) / "collector.log", "w", encoding="utf-8")
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log_file, stderr=subprocess.STDOUT)
        meter = ProcessMeter(proc)

        time.sleep(warmup)                                  # 접속이 모두 끝날 때까지 기다린 뒤 재기 시작한다
        if proc.poll() is not None:
            raise RuntimeError(f"수집기가 시작 직후 종료됐습니다 (로그: {workdir}/collector.log)")
        ws_result: dict = {}
        ws_thread = None
        if web:
            ws_thread = threading.Thread(
                target=lambda: ws_result.update(ws_collect("127.0.0.1", web_port, duration)), daemon=True)
            ws_thread.start()

        start = time.time()
        cpu0, wall0 = meter.cpu_seconds(), time.monotonic()
        cpu_prev, wall_prev = cpu0, wall0
        peaks = {"cpu_pct": 0.0, "mem_mb": 0.0}
        mem_samples: list[float] = []
        while time.monotonic() - wall0 < duration:
            time.sleep(1.0)
            cpu, wall = meter.cpu_seconds(), time.monotonic()
            if cpu is not None and cpu_prev is not None:
                peaks["cpu_pct"] = max(peaks["cpu_pct"], (cpu - cpu_prev) / (wall - wall_prev) * 100)
            mem = meter.memory_mb()
            if mem is not None:
                mem_samples.append(mem)
                peaks["mem_mb"] = max(peaks["mem_mb"], mem)
            cpu_prev, wall_prev = cpu, wall
            if proc.poll() is not None:
                raise RuntimeError(f"측정 중 수집기가 종료됐습니다 (로그: {workdir}/collector.log)")
        end = time.time()
        cpu1 = meter.cpu_seconds()
        if ws_thread:
            ws_thread.join(timeout=5)

        proc.terminate()
        proc.wait(timeout=10)
        analysis = analyze_db(db_path, names, start, end)
        elapsed = end - start
        gaps = analysis["all_gaps"]
        worst = max((d["gaps"]["max"] for d in analysis["per_device"].values() if d["gaps"]), default=None)
        samples_each = [d["samples"] for d in analysis["per_device"].values()]
        # WAL 모드의 로그 파일(.db-wal)은 미리 큰 크기를 잡아 두므로, 데이터를 메인 파일로 합친 뒤(체크포인트) 크기를 잰다
        tmp = sqlite3.connect(db_path)
        tmp.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        tmp.close()
        db_bytes = Path(db_path).stat().st_size
        result = {
            "devices": devices, "duration": round(elapsed, 1), "interval": interval,
            "samples": analysis["total_samples"],
            "samples_per_sec": analysis["total_samples"] / elapsed,
            "expected_per_sec": devices / interval,
            "fewest_samples_device": min(samples_each) if samples_each else 0,
            "most_samples_device": max(samples_each) if samples_each else 0,
            "gap_p50": percentile(gaps, 50), "gap_p95": percentile(gaps, 95),
            "gap_p99": percentile(gaps, 99), "gap_max": max(gaps) if gaps else None,
            "worst_device_max_gap": worst,
            "events": analysis["events"], "alarms": analysis["alarms"],
            "cpu_avg_pct": None if cpu1 is None or cpu0 is None else (cpu1 - cpu0) / elapsed * 100,
            "cpu_peak_pct": peaks["cpu_pct"] if meter.supported else None,
            "mem_mb_end": mem_samples[-1] if mem_samples else None, "mem_mb_peak": peaks["mem_mb"] if mem_samples else None,
            "db_mb": db_bytes / (1024 * 1024),
            "db_bytes_per_row": db_bytes / max(1, analysis["total_samples"]),
        }
        if web:
            sizes, times = ws_result.get("state_sizes", []), ws_result.get("state_times", [])
            state_gaps = [b - a for a, b in zip(times, times[1:])]
            result["ws"] = {
                "error": ws_result.get("error"), "messages": ws_result.get("messages", 0),
                "messages_per_sec": ws_result.get("messages", 0) / elapsed,
                "kb_per_sec": ws_result.get("bytes", 0) / 1024 / elapsed,
                "by_type": ws_result.get("by_type", {}),
                "state_kb_avg": (sum(sizes) / len(sizes) / 1024) if sizes else None,
                "state_gap_p95": percentile(state_gaps, 95), "state_gap_max": max(state_gaps) if state_gaps else None,
            }
        return result
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if log_file:
            log_file.close()
        for s in sims:
            s.stop()
        if own_dir:
            shutil.rmtree(workdir, ignore_errors=True)


def fmt(value, spec: str = ".2f", unit: str = "") -> str:
    return "-" if value is None else format(value, spec) + unit


def print_result(r: dict) -> None:
    print(f"\n## 설비 {r['devices']}대 동시 수집 ({r['duration']}초, 폴링 {r['interval']}초)")
    print(f"- 저장된 측정값: {r['samples']}개 = 초당 {r['samples_per_sec']:.1f}개 (이상적으로는 초당 {r['expected_per_sec']:.1f}개 이하)")
    print(f"- 설비별 측정값 수: 최소 {r['fewest_samples_device']} ~ 최대 {r['most_samples_device']}")
    print(f"- 수집 간격(초): 중앙값 {fmt(r['gap_p50'], '.3f')}, 95% {fmt(r['gap_p95'], '.3f')}, 99% {fmt(r['gap_p99'], '.3f')}, "
          f"최대 {fmt(r['gap_max'], '.3f')} (설비별 최대 공백 중 가장 긴 것 {fmt(r['worst_device_max_gap'], '.3f')})")
    print(f"- 통신 이벤트: {r['events'] or '없음'} / 알람 {r['alarms']}건")
    print(f"- 수집기 CPU: 평균 {fmt(r['cpu_avg_pct'], '.1f', '%')} (한 코어 기준), 최고 {fmt(r['cpu_peak_pct'], '.1f', '%')}")
    print(f"- 수집기 메모리: 끝 {fmt(r['mem_mb_end'], '.0f', 'MB')}, 최고 {fmt(r['mem_mb_peak'], '.0f', 'MB')} / DB 크기 {r['db_mb']:.2f}MB (측정값 한 건당 약 {r['db_bytes_per_row']:.0f}바이트)")
    if "ws" in r:
        w = r["ws"]
        print(f"- 웹 방송(접속자 1명): 메시지 초당 {w['messages_per_sec']:.1f}개, 초당 {w['kb_per_sec']:.1f}KB, 종류 {w['by_type']}, "
              f"state 메시지 평균 {fmt(w['state_kb_avg'], '.1f', 'KB')}, state 간격 95% {fmt(w['state_gap_p95'], '.2f')}초 최대 {fmt(w['state_gap_max'], '.2f')}초"
              + (f" (오류: {w['error']})" if w["error"] else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="가상 설비 N대 동시 수집 부하 시험")
    parser.add_argument("--devices", type=int, default=30)
    parser.add_argument("--duration", type=float, default=60.0, help="측정 시간(초)")
    parser.add_argument("--warmup", type=float, default=5.0, help="측정 전 접속을 기다리는 시간(초)")
    parser.add_argument("--interval", type=float, default=0.5, help="수집기 폴링 간격(초)")
    parser.add_argument("--web", action="store_true", help="웹 방송을 켜고 WebSocket 접속자 1명을 붙인다")
    parser.add_argument("--workdir", default=None, help="임시 파일(DB, 로그)을 둘 새 폴더. 없으면 임시 폴더를 만들고 지운다")
    args = parser.parse_args()
    print_result(run_load(args.devices, args.duration, args.warmup, args.interval, args.web, args.workdir))


if __name__ == "__main__":
    main()
