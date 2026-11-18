"""알람 관리: 설비 상태와 통신 상태에서 알람을 만들고 해제한다.

알람 종류
- EQUIPMENT_FAULT  P1  설비가 '이상'(정지)을 보고함
- TEMP_WARNING     P2  설비가 '주의'(온도 상한 근접)를 보고함
- COMM_LOST        P1  통신 실패가 연속으로 comm_lost_after번 이상
- COUNTER_RESET    P3  생산 카운트가 줄어듦 (한 바퀴 돎은 제외) -> 설비 재시작 추정

규칙 (docs의 순서도 4번)
- 같은 설비·종류의 알람이 이미 열려 있으면 새로 만들지 않고 값만 갱신한다 (중복 방지).
- 알람이 해제된 지 reopen_window초 안에 같은 알람이 다시 발생하고 그 알람을 아직 아무도
  확인하지 않았다면, 새로 만들지 않고 기존 알람을 다시 열면서 반복 횟수를 올린다
  (채터링으로 같은 알람이 수십 줄 쌓이는 것을 막는다). 이미 확인된 알람은 다시 열지 않고
  새 알람으로 만든다 (확인한 사람이 재발생을 놓치면 안 되므로).
- 조건이 사라져도 clear_delay초 동안 계속 사라져 있어야 해제한다. 값이 기준선 근처에서
  흔들려도 알람이 켜졌다 꺼졌다 하지 않게 하기 위함이다. 통신 복구는 바로 해제한다.
- 확인(ACK)과 해제는 독립이다. 해제돼도 사람이 확인하기 전에는 닫히지 않는다.
- 상태는 모두 DB에 있으므로 수집 서버를 다시 켜도 열린 알람을 이어 받는다.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from .device import EV_CONNECT_FAILED, EV_DISCONNECTED, EV_READ_FAILED, Event
from .models import Snapshot
from .store import Store

log = logging.getLogger(__name__)

SEV_P1, SEV_P2, SEV_P3 = 1, 2, 3

KIND_FAULT = "EQUIPMENT_FAULT"
KIND_TEMP_WARNING = "TEMP_WARNING"
KIND_COMM_LOST = "COMM_LOST"
KIND_COUNTER_RESET = "COUNTER_RESET"

STATUS_WARNING = 1
STATUS_FAULT = 2
FAILURE_EVENTS = {EV_READ_FAILED, EV_CONNECT_FAILED, EV_DISCONNECTED}


class AlarmManager:
    def __init__(
        self,
        store: Store,
        *,
        clear_delay: float = 3.0,
        comm_lost_after: int = 3,
        reopen_window: float = 60.0,
        on_alarm: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.store = store
        self.clear_delay = clear_delay
        self.comm_lost_after = comm_lost_after
        self.reopen_window = reopen_window
        self._on_alarm = on_alarm
        self._active: dict[tuple[str, str], int] = {}
        self._false_since: dict[tuple[str, str], float] = {}
        self._failures: dict[str, int] = {}
        self._last_count: dict[str, int] = {}
        for row in store.active_alarms():                    # 이전 실행에서 열려 있던 알람을 이어 받는다
            self._active[(row["device"], row["kind"])] = row["id"]

    # ---- 입력 ----------------------------------------------------------------
    def on_snapshot(self, device: str, snap: Snapshot) -> None:
        now = snap.timestamp
        self._failures[device] = 0
        self._update(device, KIND_COMM_LOST, SEV_P1, False, now, "", None, delay=0.0)
        self._update(device, KIND_FAULT, SEV_P1, snap.status == STATUS_FAULT, now,
                     f"설비 이상 정지 (온도 {snap.temp_c:.1f}C)", snap.temp_c)
        self._update(device, KIND_TEMP_WARNING, SEV_P2, snap.status == STATUS_WARNING, now,
                     f"온도 {snap.temp_c:.1f}C 주의 기준 초과", snap.temp_c)
        self._check_counter_reset(device, snap)

    def on_event(self, event: Event) -> None:
        if event.kind not in FAILURE_EVENTS:
            return
        n = self._failures.get(event.device, 0) + 1
        self._failures[event.device] = n
        if n >= self.comm_lost_after:
            self._update(event.device, KIND_COMM_LOST, SEV_P1, True, event.time,
                         f"통신 끊김 (연속 실패 {n}회): {event.message}", float(n))

    def ack(self, alarm_id: int, by: str, now: float | None = None) -> bool:
        ok = self.store.ack_alarm(alarm_id, by, time.time() if now is None else now)
        if ok:
            self._notify("acked", alarm_id)
        return ok

    # ---- 내부 ----------------------------------------------------------------
    def _update(self, device: str, kind: str, severity: int, condition: bool, now: float,
                message: str, value: float | None, delay: float | None = None) -> None:
        key = (device, kind)
        alarm_id = self._active.get(key)

        if condition:
            self._false_since.pop(key, None)
            if alarm_id is None:
                alarm_id, reopened = self._raise_or_reopen(device, kind, severity, message, value, now)
                self._active[key] = alarm_id
                self._notify("reopened" if reopened else "raised", alarm_id)
            else:
                self.store.update_alarm_value(alarm_id, value)
            return

        if alarm_id is None:
            return
        since = self._false_since.setdefault(key, now)
        wait = self.clear_delay if delay is None else delay
        if now - since >= wait:
            self.store.clear_alarm(alarm_id, now)
            del self._active[key]
            self._false_since.pop(key, None)
            self._notify("cleared", alarm_id)

    def _check_counter_reset(self, device: str, snap: Snapshot) -> None:
        prev = self._last_count.get(device)
        self._last_count[device] = snap.count
        if prev is None or snap.count >= prev:
            return
        if prev >= 0xF000 and snap.count < 0x1000:           # 16비트 한 바퀴 돎은 이상이 아니다
            return
        alarm_id, reopened = self._raise_or_reopen(
            device, KIND_COUNTER_RESET, SEV_P3,
            f"생산 카운트 초기화 감지 ({prev} -> {snap.count}), 설비 재시작으로 추정",
            float(snap.count), snap.timestamp)
        self.store.clear_alarm(alarm_id, snap.timestamp)     # 일회성: 해제된 채 확인을 기다린다
        self._notify("reopened" if reopened else "raised", alarm_id)

    def _raise_or_reopen(self, device: str, kind: str, severity: int, message: str,
                         value: float | None, now: float) -> tuple[int, bool]:
        """같은 알람이 막 해제됐는데 아직 아무도 확인하지 않았다면 새로 만들지 않고 다시 연다.
        돌려주는 값: (알람 번호, 다시 열었는가)"""
        prev = self.store.find_reopenable(device, kind, now - self.reopen_window)
        if prev is not None and self.store.reopen_alarm(prev["id"], message, value, now):
            return prev["id"], True
        return self.store.raise_alarm(device, kind, severity, message, value, now), False

    def _notify(self, action: str, alarm_id: int) -> None:
        if self._on_alarm is None:
            return
        row = self.store.get_alarm(alarm_id)
        try:
            self._on_alarm(action, dict(row))
        except Exception:                                    # 콜백 오류가 수집을 멈추면 안 된다
            log.exception("on_alarm 콜백 오류")
