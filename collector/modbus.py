"""Modbus TCP 클라이언트 (라이브러리 없이 소켓으로 직접 구현).

요청/응답 프레임(MBAP 헤더 + PDU)을 직접 만들고 검증한다.

오류는 네 종류로 나눈다. 수집 서버는 종류에 따라 다르게 대응한다.
- ModbusTimeout            응답이 제한 시간 안에 오지 않음      -> 연결을 닫고 재접속
- ModbusConnectionError    접속 거부, 연결 끊김                  -> 연결을 닫고 재접속
- ModbusProtocolError      규격에 맞지 않는 응답(깨진 패킷 등)   -> 연결을 닫고 재접속
- ModbusExceptionResponse  설비가 예외 코드로 응답(잘못된 주소 등) -> 연결은 정상, 요청만 실패

시간 초과나 규격 위반 뒤에는 늦게 도착한 응답이 다음 요청과 뒤섞일 수 있으므로
클라이언트가 스스로 연결을 닫는다(connected가 False가 된다).

주고받은 바이트는 last_exchange(가장 최근)와 last_failed_exchange(가장 최근 실패)에 그대로 남긴다.
화면의 패킷 뷰어가 이 기록을 보여 준다 (collector/packets.py가 필드별로 풀어 준다).
"""
from __future__ import annotations

import asyncio
import struct
import time

from .packets import Exchange

MBAP_LENGTH = 7
FUNC_READ_HOLDING = 0x03


class ModbusError(Exception):
    """수집 중 발생하는 모든 통신 오류의 부모."""


class ModbusTimeout(ModbusError):
    pass


class ModbusConnectionError(ModbusError):
    pass


class ModbusProtocolError(ModbusError):
    pass


class ModbusExceptionResponse(ModbusError):
    def __init__(self, function: int, code: int) -> None:
        super().__init__(f"예외 응답: 기능 0x{function:02X}, 코드 0x{code:02X}")
        self.function = function
        self.code = code


# ---- 프레임 만들기와 검사 (소켓과 무관한 순수 함수) ---------------------------
def build_read_request(tid: int, unit: int, address: int, quantity: int) -> bytes:
    """기능 0x03 요청 12바이트: MBAP 헤더 7 + PDU 5."""
    pdu = struct.pack(">BHH", FUNC_READ_HOLDING, address, quantity)
    return struct.pack(">HHHB", tid & 0xFFFF, 0, len(pdu) + 1, unit) + pdu


def parse_header(header: bytes, expected_tid: int, expected_unit: int) -> int:
    """MBAP 헤더를 검사하고, 뒤따라올 PDU의 바이트 수를 돌려준다."""
    tid, protocol, length, unit = struct.unpack(">HHHB", header)
    if protocol != 0:
        raise ModbusProtocolError(f"프로토콜 ID가 0이 아닙니다: 0x{protocol:04X}")
    if not 2 <= length <= 253:
        raise ModbusProtocolError(f"길이 필드가 비정상입니다: {length}")
    if tid != expected_tid:
        raise ModbusProtocolError(f"트랜잭션 번호 불일치: 요청 {expected_tid}, 응답 {tid}")
    if unit != expected_unit:
        raise ModbusProtocolError(f"유닛 ID 불일치: 요청 {expected_unit}, 응답 {unit}")
    return length - 1   # 길이 필드는 유닛 ID 1바이트를 포함한다


def parse_read_response(pdu: bytes, quantity: int) -> list[int]:
    """기능 0x03 응답 PDU를 레지스터 값 목록으로 바꾼다."""
    if len(pdu) < 2:
        raise ModbusProtocolError("PDU가 너무 짧습니다.")
    func = pdu[0]
    if func == (FUNC_READ_HOLDING | 0x80):
        raise ModbusExceptionResponse(FUNC_READ_HOLDING, pdu[1])
    if func != FUNC_READ_HOLDING:
        raise ModbusProtocolError(f"예상하지 못한 기능 코드: 0x{func:02X}")
    byte_count = pdu[1]
    if byte_count != quantity * 2 or len(pdu) != 2 + byte_count:
        raise ModbusProtocolError(
            f"바이트 수 불일치: 필드 {byte_count}, 기대 {quantity * 2}, 실제 PDU {len(pdu)}"
        )
    return list(struct.unpack(f">{quantity}H", pdu[2:]))


# ---- 클라이언트 ----------------------------------------------------------------
class ModbusTcpClient:
    def __init__(self, host: str, port: int, timeout: float = 1.0, unit_id: int = 1) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.unit_id = unit_id
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._tid = 0
        self.last_exchange: Exchange | None = None           # 가장 최근에 주고받은 기록
        self.last_failed_exchange: Exchange | None = None    # 가장 최근에 실패한 기록 (이후 성공해도 남는다)

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        await self.close()
        try:
            async with asyncio.timeout(self.timeout):
                self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        except TimeoutError:
            raise ModbusTimeout(f"연결 시간 초과 ({self.timeout * 1000:.0f} ms)") from None
        except OSError as e:
            raise ModbusConnectionError(f"연결 실패: {e}") from e

    async def close(self) -> None:
        writer, self._reader, self._writer = self._writer, None, None
        if writer is None:
            return
        writer.close()
        try:
            async with asyncio.timeout(1.0):
                await writer.wait_closed()
        except (TimeoutError, OSError):
            pass

    async def read_holding_registers(self, address: int, quantity: int) -> list[int]:
        """기능 0x03: Holding Register를 quantity개 읽는다."""
        if not 1 <= quantity <= 125:
            raise ValueError("quantity는 1~125여야 합니다.")
        if not self.connected:
            raise ModbusConnectionError("연결되어 있지 않습니다.")
        assert self._reader is not None and self._writer is not None

        self._tid = (self._tid + 1) & 0xFFFF
        tid = self._tid
        request = build_read_request(tid, self.unit_id, address, quantity)
        received = bytearray()           # 설비에서 받은 바이트 (실패하면 받은 만큼만)
        started = time.time()
        error: str | None = None
        try:
            try:
                async with asyncio.timeout(self.timeout):
                    self._writer.write(request)
                    await self._writer.drain()
                    header = await self._reader.readexactly(MBAP_LENGTH)        # 1) 헤더 7바이트
                    received += header
                    pdu_len = parse_header(header, tid, self.unit_id)
                    pdu = await self._reader.readexactly(pdu_len)               # 2) PDU
                    received += pdu
            except TimeoutError:
                await self.close()
                raise ModbusTimeout(f"응답 시간 초과 ({self.timeout * 1000:.0f} ms)") from None
            except asyncio.IncompleteReadError as e:
                received += e.partial                                            # 끊기기 전에 받은 바이트도 남긴다
                await self.close()
                raise ModbusConnectionError(f"연결이 끊겼습니다: {type(e).__name__}") from e
            except OSError as e:
                await self.close()
                raise ModbusConnectionError(f"연결이 끊겼습니다: {type(e).__name__}") from e
            except ModbusProtocolError:
                await self.close()
                raise

            try:
                return parse_read_response(pdu, quantity)
            except ModbusProtocolError:
                await self.close()
                raise
        except ModbusError as e:
            error = str(e)
            raise
        except asyncio.CancelledError:
            error = "취소됨"
            raise
        finally:
            exchange = Exchange(started, tid, request, bytes(received) or None, error)
            self.last_exchange = exchange
            if error is not None:
                self.last_failed_exchange = exchange
