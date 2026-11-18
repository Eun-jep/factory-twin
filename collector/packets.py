"""Modbus 요청·응답 바이트를 화면에서 보여 주기 위해 필드별로 나눠 해석한다 (패킷 뷰어의 재료).

수집 클라이언트가 한 번 주고받을 때마다 Exchange에 요청과 응답 바이트를 그대로 남긴다.
이 모듈은 그 바이트를 "어느 바이트가 무슨 뜻인지" 필드 목록으로 바꾼다.
해석은 서버에서 한 번만 하고(시험이 있다), 화면은 받은 필드를 색으로 나눠 그리기만 한다.

각 필드는 {name, hex, value, group}이다. group이 같으면 화면에서 같은 색으로 칠한다.
  header    MBAP 헤더 7바이트 (트랜잭션 번호, 프로토콜 ID, 길이, 유닛 ID)
  function  기능 코드
  address   시작 주소
  count     개수, 바이트 수
  data      레지스터 값
  error     예외 응답이나 규격에 맞지 않는 바이트
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .models import REGISTER_LABELS, describe_register

EXCEPTION_NAMES = {
    1: "지원하지 않는 기능",
    2: "잘못된 주소",
    3: "잘못된 값",
    4: "설비 오류",
}


@dataclass(frozen=True)
class Exchange:
    """수집 클라이언트가 한 번 주고받은 기록."""
    ts: float                  # 요청을 보낸 시각 (epoch 초)
    tid: int                   # 트랜잭션 번호
    request: bytes             # 보낸 바이트
    response: bytes | None     # 받은 바이트. 깨졌거나 일부만 받았으면 받은 만큼. 아무것도 못 받았으면 None
    error: str | None          # 실패했다면 이유. 성공이면 None


def hex_bytes(data: bytes) -> str:
    return data.hex(" ")


def _field(name: str, raw: bytes, value: str, group: str) -> dict:
    return {"name": name, "hex": hex_bytes(raw), "value": value, "group": group}


def _header_fields(data: bytes) -> list[dict]:
    tid, protocol, length, unit = struct.unpack(">HHHB", data[:7])
    protocol_text = f"{protocol} (정상)" if protocol == 0 else f"{protocol} (비정상: 0이어야 함)"
    return [
        _field("트랜잭션 번호", data[0:2], str(tid), "header"),
        _field("프로토콜 ID", data[2:4], protocol_text, "header"),
        _field("길이", data[4:6], f"{length} (뒤따르는 바이트 수)", "header"),
        _field("유닛 ID", data[6:7], str(unit), "header"),
    ]


def annotate_request(data: bytes) -> list[dict]:
    """우리가 보내는 요청(기능 0x03, 12바이트)을 필드로 나눈다."""
    if len(data) != 12 or data[7] != 0x03:
        return [_field("알 수 없는 요청", data, "해석할 수 없습니다", "error")]
    address, quantity = struct.unpack(">HH", data[8:12])
    return _header_fields(data) + [
        _field("기능 코드", data[7:8], "3 (Holding Register 읽기)", "function"),
        _field("시작 주소", data[8:10], str(address), "address"),
        _field("개수", data[10:12], f"{quantity} (레지스터 {quantity}개)", "count"),
    ]


def annotate_response(data: bytes, start_address: int = 0) -> list[dict]:
    """설비가 보낸 응답을 필드로 나눈다. 깨진 응답도 가능한 만큼 해석하고, 못 하는 부분은 error로 표시한다."""
    if len(data) < 7:
        return [_field("받은 바이트", data, f"헤더(7바이트)보다 짧습니다 ({len(data)}바이트)", "error")]
    fields = _header_fields(data)
    pdu = data[7:]
    if not pdu:
        return fields + [_field("PDU", b"", "헤더 뒤에 내용이 없습니다", "error")]

    function = pdu[0]
    if function & 0x80:                                    # 예외 응답: 기능 코드에 0x80이 더해져 온다
        fields.append(_field("기능 코드", pdu[0:1], f"{function} (예외 응답, 원래 기능 {function & 0x7F})", "error"))
        if len(pdu) >= 2:
            code = pdu[1]
            fields.append(_field("예외 코드", pdu[1:2], f"{code} ({EXCEPTION_NAMES.get(code, '알 수 없음')})", "error"))
        if len(pdu) > 2:
            fields.append(_field("남는 바이트", pdu[2:], "예외 응답은 2바이트여야 합니다", "error"))
        return fields
    if function != 0x03:
        fields.append(_field("기능 코드", pdu[0:1], f"{function} (예상하지 못한 기능)", "error"))
        if len(pdu) > 1:
            fields.append(_field("나머지", pdu[1:], "해석하지 않습니다", "error"))
        return fields

    fields.append(_field("기능 코드", pdu[0:1], "3 (Holding Register 읽기)", "function"))
    if len(pdu) < 2:
        return fields + [_field("바이트 수", b"", "응답이 여기서 끝났습니다", "error")]
    byte_count = pdu[1]
    fields.append(_field("바이트 수", pdu[1:2], f"{byte_count} (레지스터 {byte_count // 2}개)", "count"))

    body = pdu[2:]
    for i in range(0, len(body) - 1, 2):
        address = start_address + i // 2                   # 이름과 해석은 응답 안의 순서가 아니라 실제 주소를 기준으로 한다
        raw = struct.unpack(">H", body[i:i + 2])[0]
        label = REGISTER_LABELS[address] if address < len(REGISTER_LABELS) else "값"
        fields.append(_field(f"레지스터 {address}: {label}", body[i:i + 2],
                             f"{raw} -> {describe_register(address, raw)}", "data"))
    if len(body) % 2:
        fields.append(_field("남는 바이트", body[-1:], "레지스터 한 칸(2바이트)이 되지 못했습니다", "error"))
    if len(body) != byte_count:
        fields.append(_field("바이트 수 불일치", b"", f"바이트 수 필드는 {byte_count}인데 실제 데이터는 {len(body)}바이트", "error"))
    return fields


def exchange_dict(ex: Exchange | None) -> dict | None:
    """Exchange를 브라우저로 보낼 JSON 형태로 바꾼다."""
    if ex is None:
        return None
    start = struct.unpack(">H", ex.request[8:10])[0] if len(ex.request) == 12 else 0
    return {
        "ts": ex.ts,
        "tid": ex.tid,
        "error": ex.error,
        "request": {"hex": hex_bytes(ex.request), "fields": annotate_request(ex.request)},
        "response": None if not ex.response else {
            "hex": hex_bytes(ex.response), "fields": annotate_response(ex.response, start)},
    }
