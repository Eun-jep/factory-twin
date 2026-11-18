"""브라우저가 WebSocket으로 보내는 명령을 처리한다.

지금은 알람 확인(ACK) 하나다.
    {"type": "ack", "id": 3, "by": "홍길동"}  ->  {"type": "ack_result", "id": 3, "ok": true, "reason": null}

브라우저가 보낸 값은 믿지 않고 모두 검사한다 (종류, 범위, 길이, 제어 문자).
확인자 이름(by)은 사용자가 직접 적는 값이다. 로그인이 없어서 실제 본인인지는 확인할 수 없다.
(이 프로젝트의 한계: 이 화면은 이 PC에서만 열리고 인증이 없다.)
"""
from __future__ import annotations

from .alarms import AlarmManager
from .store import Store

MAX_NAME_LENGTH = 30


def clean_name(value) -> str | None:
    """확인자 이름을 검사한다. 이상하면 None."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not 1 <= len(name) <= MAX_NAME_LENGTH:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in name):       # 줄바꿈 같은 제어 문자 금지
        return None
    return name


def error(message: str) -> dict:
    return {"type": "error", "message": message}


def handle_command(msg: dict, manager: AlarmManager, store: Store, now: float | None = None) -> dict:
    """명령 하나를 처리하고 브라우저로 돌려줄 답을 만든다. 절대 예외를 밖으로 내지 않는다."""
    kind = msg.get("type")
    if kind != "ack":
        return error("알 수 없는 명령입니다")

    alarm_id = msg.get("id")
    if not isinstance(alarm_id, int) or isinstance(alarm_id, bool) or alarm_id < 1:
        return error("알람 번호가 올바르지 않습니다")
    by = clean_name(msg.get("by"))
    if by is None:
        return error(f"확인자 이름은 1~{MAX_NAME_LENGTH}자여야 하고 줄바꿈 같은 문자는 쓸 수 없습니다")

    if store.get_alarm(alarm_id) is None:
        return {"type": "ack_result", "id": alarm_id, "ok": False, "reason": "없는 알람입니다"}
    ok = manager.ack(alarm_id, by, now)
    return {"type": "ack_result", "id": alarm_id, "ok": ok,
            "reason": None if ok else "이미 확인된 알람입니다"}
