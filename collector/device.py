"""설비 1대를 담당하는 수집 워커.

설비마다 워커를 하나씩 만들고, 각 워커가 자기 연결과 상태를 따로 관리한다.
그래서 한 설비가 끊겨도 다른 설비의 수집은 영향을 받지 않는다.

순서 (docs의 순서도 2번):
  접속 -> 읽기 -> 저장(콜백) -> 다음 시각까지 쉬기 -> 읽기 ...
  오류(시간 초과, 연결 끊김, 깨진 응답, 이상한 값) -> 연결 닫기 -> 대기 -> 다시 접속
  대기 시간은 1초에서 시작해 두 배씩 늘어 최대 4초. 읽기에 한 번 성공하면 1초로 돌아간다.
  예외 응답(잘못된 주소 등)은 연결이 정상이므로 재접속 없이 다음 주기에 다시 읽는다.

고정 주기: 읽기가 끝난 뒤 interval만큼 쉬는 방식이면 설비 응답이 느릴수록 주기가 늘어난다(응답 0.3초 + 0.5초 = 0.8초).
그래서 '다음에 읽을 시각'을 정해 두고 그 시각까지만 쉰다. 응답이 interval보다 길어 일정을 못 지킬 때는
밀린 만큼 몰아서 읽지 않고(설비에 부담) 지금부터 다시 시작한다.
설비가 여러 대일 때 모두 같은 순간에 읽지 않도록 start_delay로 시작 시각을 엇갈리게 할 수 있다.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .modbus import (
    ModbusConnectionError,
    ModbusError,
    ModbusExceptionResponse,
    ModbusTcpClient,
)
from .models import REGISTER_COUNT, Snapshot, parse_registers

log = logging.getLogger(__name__)

EV_CONNECTED = "connected"
EV_DISCONNECTED = "disconnected"
EV_CONNECT_FAILED = "connect_failed"
EV_READ_FAILED = "read_failed"
EV_RECOVERED = "recovered"
EV_STOPPED = "stopped"


class ConnState(Enum):
    CONNECTING = "접속 중"
    ONLINE = "정상"
    BACKOFF = "재접속 대기"
    STOPPED = "중지"


@dataclass(frozen=True)
class Event:
    time: float
    device: str
    kind: str
    message: str


@dataclass
class DeviceStats:
    state: ConnState = ConnState.CONNECTING
    reads_ok: int = 0
    reads_failed: int = 0
    connects: int = 0
    consecutive_failures: int = 0
    last_latency_ms: float = 0.0
    avg_latency_ms: float = 0.0
    last_error: str = ""
    last_snapshot: Snapshot | None = None
    last_ok_at: float | None = None      # 마지막으로 읽기에 성공한 시각 (epoch 초)

    def data_age(self, now: float) -> float | None:
        """마지막으로 값을 받은 지 몇 초 지났는가. 받은 적이 없으면 None.
        끊긴 설비의 마지막 값을 현재 값처럼 보여 주지 않으려고 쓴다."""
        return None if self.last_ok_at is None else max(0.0, now - self.last_ok_at)

    @property
    def reconnects(self) -> int:
        """처음 접속을 뺀, 다시 접속한 횟수."""
        return max(0, self.connects - 1)

    @property
    def success_rate(self) -> float:
        total = self.reads_ok + self.reads_failed
        return 1.0 if total == 0 else self.reads_ok / total


def next_backoff(current: float, maximum: float) -> float:
    """재접속 대기 시간을 두 배로 늘린다 (최대값까지)."""
    return min(current * 2, maximum)


def next_schedule(scheduled: float, interval: float, now: float) -> tuple[float, float]:
    """고정 주기의 다음 일정. scheduled = 방금 읽은 일정 시각.
    반환: (다음 일정 시각, 지금부터 기다릴 시간).
    이미 다음 일정을 넘겼으면(응답이 interval보다 길었을 때) 밀린 만큼 몰아서 읽지 않고, 지금을 새 기준으로 삼아 바로 다음으로 간다."""
    nxt = scheduled + interval
    if nxt < now:
        return now, 0.0
    return nxt, nxt - now


def stagger_delay(index: int, count: int, interval: float) -> float:
    """설비 count대가 같은 순간에 읽지 않도록, index번째 설비의 시작 지연(초). 한 주기 안에 고르게 펼친다."""
    return 0.0 if count <= 1 else (index % count) * interval / count


class DeviceWorker:
    def __init__(
        self,
        name: str,
        host: str,
        port: int,
        *,
        interval: float = 0.5,
        timeout: float = 1.0,
        backoff_initial: float = 1.0,
        backoff_max: float = 4.0,
        unit_id: int = 1,
        start_delay: float = 0.0,
        on_snapshot: Callable[[str, Snapshot], None] | None = None,
        on_event: Callable[[Event], None] | None = None,
    ) -> None:
        self.name = name
        self.interval = interval
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.start_delay = start_delay
        self._next_at: float | None = None   # 다음에 읽을 시각 (time.monotonic 기준). None이면 지금 바로
        self.stats = DeviceStats()
        self._client = ModbusTcpClient(host, port, timeout=timeout, unit_id=unit_id)
        self._on_snapshot = on_snapshot
        self._on_event = on_event
        self._stop = asyncio.Event()
        self._failing = False       # 마지막 시도가 실패였는가 (복구 이벤트용)
        self._was_connected = False  # 접속해 있던 중인가 (폴링 사이의 끊김을 알아채기 위함)

    def stop(self) -> None:
        self._stop.set()

    @property
    def last_exchange(self):
        """가장 최근에 주고받은 Modbus 바이트 기록 (패킷 뷰어용)."""
        return self._client.last_exchange

    @property
    def last_failed_exchange(self):
        """가장 최근에 실패한 기록. 이후에 성공해도 남아 있어서 '무슨 일이 있었는지' 볼 수 있다."""
        return self._client.last_failed_exchange

    async def run(self) -> None:
        backoff = self.backoff_initial
        try:
            if self.start_delay > 0:
                await self._sleep(self.start_delay)             # 설비들이 같은 순간에 읽지 않도록 시작을 엇갈린다
            while not self._stop.is_set():
                if self._was_connected and not self._client.connected:
                    # 폴링 사이에 상대가 연결을 닫았다 (설비 전원 꺼짐 등)
                    self._was_connected = False
                    self._next_at = None
                    self._record_failure(EV_DISCONNECTED,
                                         ModbusConnectionError("상대가 연결을 닫았습니다"))
                    await self._client.close()
                    backoff = await self._wait_backoff(backoff)
                    continue

                if not self._client.connected:
                    self._set_state(ConnState.CONNECTING)
                    try:
                        await self._client.connect()
                    except ModbusError as e:
                        self._record_failure(EV_CONNECT_FAILED, e)
                        backoff = await self._wait_backoff(backoff)
                        continue
                    self.stats.connects += 1
                    self._was_connected = True
                    self._emit(EV_CONNECTED, "접속됨" if self.stats.connects == 1
                               else f"다시 접속됨 ({self.stats.reconnects}번째)")

                try:
                    started = time.monotonic()
                    if self._next_at is None:
                        self._next_at = started                     # 첫 읽기(또는 재접속 뒤 첫 읽기)는 바로, 그 시각이 일정의 기준
                    regs = await self._client.read_holding_registers(0, REGISTER_COUNT)
                    snapshot = parse_registers(regs, time.time())
                    latency_ms = (time.monotonic() - started) * 1000
                except ModbusExceptionResponse as e:
                    # 설비가 거절했을 뿐 연결은 정상: 재접속 없이 다음 주기에 다시 읽는다
                    self._record_failure(EV_READ_FAILED, e)
                    await self._sleep_until_next()
                    continue
                except (ModbusError, ValueError) as e:
                    # 어떤 오류든 연결 상태를 믿을 수 없으니 닫고 처음부터 다시 접속한다
                    self._record_failure(EV_READ_FAILED, e)
                    self._was_connected = False
                    self._next_at = None                            # 재접속하면 일정을 새로 시작한다
                    await self._client.close()
                    backoff = await self._wait_backoff(backoff)
                    continue

                self._record_success(snapshot, latency_ms)
                backoff = self.backoff_initial
                await self._sleep_until_next()
        finally:
            await self._client.close()
            self._set_state(ConnState.STOPPED)
            self._emit(EV_STOPPED, "수집 중지")

    # ------------------------------------------------------------------
    async def _wait_backoff(self, backoff: float) -> float:
        self._set_state(ConnState.BACKOFF)
        await self._sleep(backoff)
        return next_backoff(backoff, self.backoff_max)

    async def _sleep_until_next(self) -> None:
        """다음에 읽을 시각까지 쉰다 (고정 주기). 이미 늦었으면 몰아서 읽지 않고 지금부터 다시 시작한다."""
        assert self._next_at is not None
        self._next_at, wait = next_schedule(self._next_at, self.interval, time.monotonic())
        if wait > 0:
            await self._sleep(wait)
        else:
            await asyncio.sleep(0)                                  # 쉴 틈이 없어도 다른 설비에게 차례를 넘긴다

    async def _sleep(self, seconds: float) -> None:
        """중지 요청이 오면 바로 깨어나는 sleep."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    def _set_state(self, state: ConnState) -> None:
        self.stats.state = state

    def _record_success(self, snapshot: Snapshot, latency_ms: float) -> None:
        s = self.stats
        s.reads_ok += 1
        s.consecutive_failures = 0
        s.last_latency_ms = latency_ms
        s.avg_latency_ms = latency_ms if s.reads_ok == 1 else s.avg_latency_ms * 0.9 + latency_ms * 0.1
        s.last_snapshot = snapshot
        s.last_ok_at = snapshot.timestamp
        s.state = ConnState.ONLINE
        if self._failing:
            self._failing = False
            self._emit(EV_RECOVERED, "수집 복구")
        if self._on_snapshot:
            try:
                self._on_snapshot(self.name, snapshot)
            except Exception:   # 콜백 오류가 수집을 멈추면 안 된다
                log.exception("on_snapshot 콜백 오류")

    def _record_failure(self, kind: str, error: Exception) -> None:
        s = self.stats
        s.reads_failed += 1
        s.consecutive_failures += 1
        s.last_error = f"{type(error).__name__}: {error}"
        self._failing = True
        self._emit(kind, s.last_error)

    def _emit(self, kind: str, message: str) -> None:
        if self._on_event:
            try:
                self._on_event(Event(time.time(), self.name, kind, message))
            except Exception:
                log.exception("on_event 콜백 오류")
