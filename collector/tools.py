"""저장된 데이터를 조회하고 알람을 확인(ACK)하는 도구.

    python -m collector.tools alarms                  # 닫히지 않은 알람
    python -m collector.tools alarms --all            # 모든 알람 (이력)
    python -m collector.tools ack 3 --by 홍길동        # 3번 알람 확인
    python -m collector.tools events --limit 20       # 통신 이벤트
    python -m collector.tools samples ARM-01 --limit 5
    python -m collector.tools production --minutes 10 # 최근 10분 생산량 (설비 재시작에도 정확)

수집 서버와 같은 DB 파일(--db, 기본 data/factory.db)을 읽는다.
수집 서버가 돌고 있어도 함께 쓸 수 있다.
"""
from __future__ import annotations

import argparse
import sys
import time
import unicodedata

from .store import Store, alarm_state

SEVERITY = {1: "P1", 2: "P2", 3: "P3"}


def fmt_time(ts: float | None) -> str:
    return "-" if ts is None else time.strftime("%m-%d %H:%M:%S", time.localtime(ts))


def fmt_time_short(ts: float | None) -> str:
    """오늘이면 시각만, 아니면 날짜와 시:분. 알람 표가 터미널 너비를 넘지 않게 짧게 쓴다."""
    if ts is None:
        return "-"
    t = time.localtime(ts)
    same_day = time.strftime("%Y%m%d", t) == time.strftime("%Y%m%d")
    return time.strftime("%H:%M:%S" if same_day else "%m-%d %H:%M", t)


def display_width(text: str) -> int:
    """터미널에서 차지하는 칸 수. 한글 같은 전각 문자는 2칸이다."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def ljust_w(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def rjust_w(text: str, width: int) -> str:
    return " " * max(0, width - display_width(text)) + text


# (머리글, 칸 폭, 정렬). 합계가 100칸 안에 들어오도록 잡았다.
# 발생은 최초 발생 시각이고, 횟수는 반복 발생 횟수(같은 알람이 해제 후 다시 발생하면 늘어난다).
ALARM_COLUMNS = [("ID", 4, "r"), ("등급", 4, "l"), ("설비", 6, "l"), ("종류", 15, "l"),
                 ("상태", 13, "l"), ("발생", 11, "l"), ("확인", 16, "l"), ("해제", 11, "l"),
                 ("횟수", 4, "r"), ("값", 6, "r")]


def format_row(cells: list[str]) -> str:
    parts = [rjust_w(c, w) if a == "r" else ljust_w(c, w)
             for c, (_, w, a) in zip(cells, ALARM_COLUMNS)]
    return " ".join(parts).rstrip()


def cmd_alarms(store: Store, args: argparse.Namespace) -> int:
    rows = store.all_alarms(args.limit) if args.all else store.open_alarms()
    if not rows:
        print("표시할 알람이 없습니다.")
        return 0
    print(format_row([h for h, _, _ in ALARM_COLUMNS]))
    for r in rows:
        acked = "-" if r["acked_at"] is None else f"{fmt_time_short(r['acked_at'])} {r['acked_by']}"
        value = "-" if r["last_value"] is None else f"{r['last_value']:.1f}"
        print(format_row([str(r["id"]), SEVERITY.get(r["severity"], "?"), r["device"], r["kind"],
                          alarm_state(r), fmt_time_short(r["raised_at"]), acked,
                          fmt_time_short(r["cleared_at"]), str(r["repeat_count"]), value]))
        if args.detail:
            last = (f"  (마지막 발생 {fmt_time_short(r['last_raised_at'])})"
                    if r["repeat_count"] > 1 else "")
            print(f"     {r['message']}{last}")
    return 0


def cmd_ack(store: Store, args: argparse.Namespace) -> int:
    if store.get_alarm(args.id) is None:
        print(f"{args.id}번 알람이 없습니다.")
        return 1
    if not store.ack_alarm(args.id, args.by, time.time()):
        print(f"{args.id}번 알람은 이미 확인되었습니다.")
        return 1
    print(f"{args.id}번 알람을 확인했습니다. (확인자 {args.by})")
    return 0


def cmd_events(store: Store, args: argparse.Namespace) -> int:
    for r in store.recent_events(args.limit):
        print(f"{fmt_time(r['ts'])}  [{r['device']}] {r['kind']}: {r['message']}")
    return 0


def cmd_samples(store: Store, args: argparse.Namespace) -> int:
    rows = store.latest_samples(args.device, args.limit)
    if not rows:
        print(f"{args.device}의 측정값이 없습니다.")
        return 0
    print(f"{'시각':<15} {'관절1':>6} {'관절2':>6} {'집게':>3} {'단계':>3} {'카운트':>6} {'온도':>6} {'상태':>3}")
    for r in rows:
        print(f"{fmt_time(r['ts']):<15} {r['joint1']:>6.1f} {r['joint2']:>6.1f} {r['gripper']:>3} "
              f"{r['phase']:>3} {r['count']:>6} {r['temp']:>6.1f} {r['status']:>3}")
    return 0


def cmd_production(store: Store, args: argparse.Namespace) -> int:
    now = time.time()
    t0 = now - args.minutes * 60
    print(f"최근 {args.minutes}분 생산량 (카운트 초기화·한 바퀴 돎 보정)")
    total = 0
    for device in store.devices():
        n = store.production_between(device, t0, now)
        total += n
        print(f"  {device:<7} {n:>5}개")
    print(f"  {'합계':<7} {total:>5}개")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="수집 데이터 조회와 알람 확인")
    parser.add_argument("--db", default="data/factory.db")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("alarms", help="알람 목록")
    p.add_argument("--all", action="store_true", help="닫힌 알람까지 모두")
    p.add_argument("--detail", action="store_true", help="알람 내용을 줄 아래에 함께 표시")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_alarms)

    p = sub.add_parser("ack", help="알람 확인(ACK)")
    p.add_argument("id", type=int)
    p.add_argument("--by", required=True, help="확인한 사람")
    p.set_defaults(func=cmd_ack)

    p = sub.add_parser("events", help="통신 이벤트")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("samples", help="최근 측정값")
    p.add_argument("device")
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_samples)

    p = sub.add_parser("production", help="최근 생산량")
    p.add_argument("--minutes", type=float, default=10)
    p.set_defaults(func=cmd_production)

    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    store = Store(args.db)
    try:
        return args.func(store, args)
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
