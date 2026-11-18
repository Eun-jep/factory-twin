"""수집 서버 실행.

    python -m collector                              # 로봇 6대 (127.0.0.1:5021~5026), 웹 화면 http://127.0.0.1:8000
    python -m collector --devices ARM-01:127.0.0.1:5021,ARM-02:127.0.0.1:5022
    python -m collector --interval 0.2 --timeout 0.5
    python -m collector --db :memory:                # 저장하지 않음
    python -m collector --no-web                     # 웹 화면 끄기
    python -m collector --web-port 8080              # 웹 화면 포트 바꾸기

읽은 값은 SQLite(기본 data/factory.db)에 저장하고, 알람을 판정하고, 브라우저로 실시간 전송한다.
Ctrl+C로 종료한다. 이벤트와 알람은 바로 출력하고, 설비별 요약은 --report 초마다 출력한다.
저장된 데이터 조회와 알람 확인은 python -m collector.tools 를 쓴다.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from .alarms import AlarmManager
from .api import make_api
from .commands import handle_command
from .device import DeviceWorker, Event, stagger_delay
from .models import Snapshot
from .retention import DEFAULT_RETENTION_DAYS, prune_loop
from .stats import KpiCache
from .store import Store, alarm_state
from .webfeed import (FeedServer, alarm_message, build_state, event_message, packet_state, snapshot_message)

DEFAULT_DEVICES = ",".join(f"ARM-{n:02d}:127.0.0.1:{5020 + n}" for n in range(1, 7))     # ARM-01~06, 포트 5021~5026
ACTION_NAMES = {"raised": "발생", "reopened": "재발생", "cleared": "해제", "acked": "확인"}
WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


def parse_devices(text: str) -> list[tuple[str, str, int]]:
    devices = []
    for item in text.split(","):
        name, host, port = item.strip().split(":")
        devices.append((name, host, int(port)))
    return devices


def stamp(epoch: float | None = None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(epoch))


def print_event(event: Event) -> None:
    print(f"{stamp(event.time)}  [{event.device}] {event.kind}: {event.message}", flush=True)


def print_alarm(action: str, row: dict) -> None:
    repeat = row.get("repeat_count", 1)
    nth = f", {repeat}회째" if repeat > 1 else ""
    print(f"{stamp()}  ** 알람 {ACTION_NAMES.get(action, action)} #{row['id']} P{row['severity']} "
          f"[{row['device']}] {row['kind']}: {row['message']}  ({alarm_state(row)}{nth})", flush=True)


async def report(workers: list[DeviceWorker], store: Store, period: float) -> None:
    while True:
        await asyncio.sleep(period)
        print(f"--- {stamp()} 요약 ---", flush=True)
        for w in workers:
            s, snap = w.stats, w.stats.last_snapshot
            age = s.data_age(time.time())
            if snap is None:
                detail = "데이터 없음"
            elif s.state.name != "ONLINE":
                # 끊긴 설비의 마지막 값은 현재 값이 아니므로 값 대신 경과 시간만 보여 준다
                detail = f"마지막 수신 {age:.0f}초 전 (값은 오래됨)"
            else:
                detail = (f"단계={snap.phase_name} 카운트={snap.count} 온도={snap.temp_c:.1f}C "
                          f"상태={snap.status_name}")
            print(f"  {w.name:<7}{s.state.value:<8} {detail} | 지연 {s.avg_latency_ms:.0f}ms "
                  f"성공 {s.reads_ok} 실패 {s.reads_failed} 재접속 {s.reconnects}", flush=True)
        opened = store.open_alarms()
        unacked = sum(1 for r in opened if r["acked_at"] is None)
        print(f"  열린 알람 {len(opened)}건 (미확인 {unacked}건)", flush=True)


async def publish_state(feed: FeedServer, workers: list[DeviceWorker], store: Store,
                        kpi_cache: KpiCache, period: float) -> None:
    """전체 상태를 주기적으로 보낸다. 개별 알림을 놓친 접속자도 곧 맞는 상태를 보게 된다."""
    while True:
        feed.publish(build_state(workers, store, kpi=kpi_cache.get()))
        await asyncio.sleep(period)


async def amain(args: argparse.Namespace) -> None:
    store = Store(args.db)
    device_specs = parse_devices(args.devices)
    workers: list[DeviceWorker] = []
    kpi_cache = KpiCache(store, workers, interval=args.interval)
    feed: FeedServer | None = None

    def hello() -> dict:
        """접속하자마자 보내는 첫 메시지: 전체 상태 + 접속 직전의 최근 통신 이벤트 (최신이 먼저)."""
        state = build_state(workers, store, kpi=kpi_cache.get())
        state["events"] = [{"ts": r["ts"], "device": r["device"], "kind": r["kind"], "message": r["message"]}
                           for r in store.recent_events(30)]
        return state

    if not args.no_web:
        feed = FeedServer(
            WEB_ROOT, args.web_host, args.web_port, hello_provider=hello,
            api=make_api(store, [name for name, _, _ in device_specs],
                         packets=lambda name: packet_state(next(w for w in workers if w.name == name))),
            on_command=lambda msg: handle_command(msg, manager, store),   # manager는 아래에서 만든다
        )

    def publish(message: dict) -> None:
        if feed is not None:
            feed.publish(message)

    def on_alarm(action: str, row: dict) -> None:
        print_alarm(action, row)
        publish(alarm_message(action, row))

    manager = AlarmManager(store, clear_delay=args.clear_delay, reopen_window=args.reopen_window,
                           on_alarm=on_alarm)

    def on_snapshot(name: str, snap: Snapshot) -> None:
        store.add_sample(name, snap)
        manager.on_snapshot(name, snap)
        publish(snapshot_message(name, snap))

    def on_event(event: Event) -> None:
        print_event(event)
        store.add_event(event.time, event.device, event.kind, event.message)
        manager.on_event(event)
        publish(event_message(event))

    for index, (name, host, port) in enumerate(device_specs):
        # 설비마다 시작을 엇갈려서(한 주기 안에 고르게) 모든 설비를 같은 순간에 읽고 저장하지 않게 한다
        workers.append(DeviceWorker(name, host, port, interval=args.interval, timeout=args.timeout,
                                    start_delay=stagger_delay(index, len(device_specs), args.interval),
                                    backoff_max=args.backoff_max,
                                    on_snapshot=on_snapshot, on_event=on_event))

    extra = []
    if args.retention_days > 0:                    # 오래된 측정값·이벤트를 자동으로 지운다 (알람은 남김)
        extra.append(asyncio.create_task(prune_loop(
            store, args.retention_days,
            on_pruned=lambda n: print(f"{stamp()}  보관 기간({args.retention_days:g}일)이 지난 측정값·이벤트 {n}건 삭제", flush=True))))
    if feed is not None:
        try:
            await feed.start()
        except OSError as e:
            store.close()
            raise SystemExit(f"웹 화면을 시작하지 못했습니다 (포트 {args.web_port}): {e}")
        extra.append(asyncio.create_task(publish_state(feed, workers, store, kpi_cache, 1.0)))
    tasks = [asyncio.create_task(w.run()) for w in workers]
    reporter = asyncio.create_task(report(workers, store, args.report))
    where = "저장 안 함" if args.db == ":memory:" else f"저장 {args.db}"
    print(f"수집 시작: 설비 {len(workers)}대, 폴링 {args.interval}초, 제한 시간 {args.timeout}초, "
          f"{where} (Ctrl+C로 종료)", flush=True)
    if feed is not None:
        print(f"웹 화면: http://{feed.host}:{feed.port}/", flush=True)
    try:
        await asyncio.gather(*tasks)
    finally:
        for w in workers:
            w.stop()
        reporter.cancel()
        for t in extra:
            t.cancel()
        await asyncio.gather(*tasks, reporter, *extra, return_exceptions=True)
        if feed is not None:
            await feed.stop()
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Modbus TCP 설비 수집 서버")
    parser.add_argument("--devices", default=DEFAULT_DEVICES, help="이름:호스트:포트 목록(쉼표로 구분)")
    parser.add_argument("--interval", type=float, default=0.5, help="폴링 간격(초)")
    parser.add_argument("--timeout", type=float, default=1.0, help="응답 제한 시간(초)")
    parser.add_argument("--backoff-max", type=float, default=4.0,
                        help="끊긴 설비에 다시 접속하기 전 기다리는 시간의 상한(초). 1초에서 시작해 두 배씩 늘어난다. "
                             "작을수록 설비가 켜졌을 때 빨리 복구되고, 클수록 꺼진 설비에 덜 접속한다")
    parser.add_argument("--report", type=float, default=2.0, help="요약 출력 간격(초)")
    parser.add_argument("--db", default="data/factory.db", help="저장 파일 (':memory:'면 저장 안 함)")
    parser.add_argument("--clear-delay", type=float, default=3.0,
                        help="조건이 사라진 뒤 알람을 해제하기까지 기다리는 시간(초)")
    parser.add_argument("--reopen-window", type=float, default=60.0,
                        help="해제 후 이 시간(초) 안에 같은 알람이 다시 발생하면 새로 만들지 않고 다시 연다")
    parser.add_argument("--retention-days", type=float, default=DEFAULT_RETENTION_DAYS,
                        help="측정값·통신 이벤트를 보관하는 기간(일). 이보다 오래된 것은 자동으로 지운다. 0이면 지우지 않음. 알람은 지우지 않는다")
    parser.add_argument("--web-host", default="127.0.0.1", help="웹 화면 주소 (기본: 이 PC에서만 접속)")
    parser.add_argument("--web-port", type=int, default=8000, help="웹 화면 포트")
    parser.add_argument("--no-web", action="store_true", help="웹 화면을 끈다")
    args = parser.parse_args()

    sys.stdout.reconfigure(errors="replace")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass
    print("수집 종료", flush=True)


if __name__ == "__main__":
    main()
