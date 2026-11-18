"""브라우저가 부르는 조회 주소 (HTTP GET, JSON).

    /api/history?device=ARM-01&minutes=10&points=300   차트용 측정값 이력
    /api/events?limit=50                               최근 통신 이벤트
    /api/alarms?limit=50&all=1                         알람 목록 (all=1이면 닫힌 알람까지)
    /api/packet?device=ARM-01                          패킷 뷰어: 그 설비의 마지막 정상·실패 패킷 (필드별 해석 포함)

각 처리 함수는 (HTTP 상태 코드, JSON으로 바꿀 딕셔너리)를 돌려준다.
브라우저가 보낸 값은 모두 검사한다: 설비 이름은 알고 있는 이름만, 숫자는 범위 안에서만 받는다.
"""
from __future__ import annotations

import re
import time
from typing import Callable

from .stats import history
from .store import Store
from .webfeed import alarm_dict

Handler = Callable[[dict[str, str]], tuple[int, dict]]


class ParamError(Exception):
    pass


def int_param(query: dict[str, str], name: str, default: int, low: int, high: int) -> int:
    """정수 파라미터를 읽고 low~high 범위를 확인한다. 없으면 default."""
    raw = query.get(name)
    if raw is None:
        return default
    text = raw.strip()
    # int()는 아랍 숫자("٣") 같은 다른 문자 체계의 숫자도 받아들이므로, 아스키 숫자만 허용한다
    if not re.fullmatch(r"-?[0-9]+", text):
        raise ParamError(f"{name}은(는) 정수여야 합니다")
    value = int(text)
    if not low <= value <= high:
        raise ParamError(f"{name}은(는) {low}~{high} 사이여야 합니다")
    return value


def make_api(store: Store, device_names: list[str], clock: Callable[[], float] = time.time,
             packets: Callable[[str], dict] | None = None) -> dict[str, Handler]:
    """packets: 설비 이름 -> 패킷 기록을 돌려주는 함수 (실행 중인 수집 워커에서 읽는다)."""
    names = set(device_names)

    def history_handler(query: dict[str, str]) -> tuple[int, dict]:
        device = query.get("device")
        if device not in names:
            return 404, {"error": "알 수 없는 설비입니다"}
        minutes = int_param(query, "minutes", 10, 1, 1440)
        points = int_param(query, "points", 300, 20, 2000)
        return 200, history(store, device, minutes, clock(), points)

    def events_handler(query: dict[str, str]) -> tuple[int, dict]:
        limit = int_param(query, "limit", 50, 1, 200)
        rows = store.recent_events(limit)
        return 200, {"events": [{"ts": r["ts"], "device": r["device"], "kind": r["kind"],
                                 "message": r["message"]} for r in rows]}

    def alarms_handler(query: dict[str, str]) -> tuple[int, dict]:
        limit = int_param(query, "limit", 50, 1, 200)
        rows = store.all_alarms(limit) if query.get("all") == "1" else store.open_alarms()[:limit]
        return 200, {"alarms": [alarm_dict(r) for r in rows]}

    def packet_handler(query: dict[str, str]) -> tuple[int, dict]:
        device = query.get("device")
        if device not in names:
            return 404, {"error": "알 수 없는 설비입니다"}
        if packets is None:
            return 404, {"error": "패킷 기록을 제공하지 않습니다"}
        return 200, {"device": device, **packets(device)}

    def guarded(handler: Handler) -> Handler:
        def run(query: dict[str, str]) -> tuple[int, dict]:
            try:
                return handler(query)
            except ParamError as e:
                return 400, {"error": str(e)}
        return run

    return {
        "/api/history": guarded(history_handler),
        "/api/events": guarded(events_handler),
        "/api/alarms": guarded(alarms_handler),
        "/api/packet": guarded(packet_handler),
    }
