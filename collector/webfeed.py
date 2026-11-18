"""브라우저로 실시간 데이터를 보내는 서버. WebSocket을 라이브러리 없이 직접 구현했다 (RFC 6455).

한 포트에서 두 가지를 한다.
- 일반 HTTP GET이면 web/ 폴더의 정적 파일(HTML, JS, CSS)을 보낸다.
- 주소가 /ws 이고 'Upgrade: websocket' 헤더가 있으면 WebSocket으로 전환해 JSON 메시지를 계속 보낸다.

브라우저로 보내는 메시지 (모두 JSON, "type" 필드로 구분)
  state     전체 상태 (설비별 연결 상태·통계·마지막 값 + 열린 알람). 접속할 때와 1초마다 보낸다.
  snapshot  설비 값 하나를 읽을 때마다 (0.5초마다 설비별로)
  alarm     알람 발생·재발생·해제·확인
  event     통신 이벤트 (접속, 끊김, 실패, 복구)

설계에서 신경 쓴 점
- 느린 브라우저가 수집 서버를 붙잡지 않는다: 접속자마다 크기가 정해진 대기열을 두고, 가득 차면 오래된
  메시지부터 버린다. 5초 안에 보내지 못하면 그 접속을 끊는다.
- 로컬 전용: 기본으로 127.0.0.1에만 열고, Host 헤더가 로컬 주소가 아니면 거절한다(DNS 리바인딩 방지).
  WebSocket은 Origin이 같은 주소일 때만 받는다(다른 웹사이트가 몰래 접속해 데이터를 읽는 것 방지).
- 정적 파일은 web/ 폴더 밖으로 나갈 수 없다 (../ 경로 차단).
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import struct
import time
from collections import deque
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from .device import DeviceWorker, Event
from .models import Snapshot
from .packets import exchange_dict
from .store import Store, alarm_state

log = logging.getLogger(__name__)

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"        # RFC 6455에 정해진 고정 문자열
OP_TEXT, OP_CLOSE, OP_PING, OP_PONG = 0x1, 0x8, 0x9, 0xA
MAX_HEADER = 16 * 1024          # HTTP 요청 머리 최대 크기
MAX_FRAME = 1 << 20             # 브라우저가 보내는 프레임 최대 크기 (1MB)
HEAD_TIMEOUT = 10.0             # 요청 머리를 기다리는 최대 시간(초)
SEND_TIMEOUT = 5.0              # 브라우저로 메시지를 보내는 데 허용하는 최대 시간(초)
COMMAND_MAX_BYTES = 4096        # 브라우저가 보내는 명령 하나의 최대 크기
COMMAND_LIMIT = 20              # 한 접속이 COMMAND_WINDOW초 안에 보낼 수 있는 명령 수 (넘으면 접속을 끊는다)
COMMAND_WINDOW = 5.0
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}
REASONS = {200: "OK", 101: "Switching Protocols", 400: "Bad Request", 403: "Forbidden",
           404: "Not Found", 405: "Method Not Allowed", 426: "Upgrade Required",
           431: "Request Header Fields Too Large", 500: "Internal Server Error"}


# ---- WebSocket 프레임 (소켓과 무관한 순수 함수) ---------------------------------
def accept_key(key: str) -> str:
    """Sec-WebSocket-Key로 응답용 Sec-WebSocket-Accept 값을 만든다."""
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")


def encode_frame(opcode: int, payload: bytes) -> bytes:
    """서버가 보내는 프레임. 서버 프레임은 마스킹하지 않는다."""
    header = bytearray([0x80 | opcode])                  # FIN=1 (한 번에 끝나는 메시지)
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    return bytes(header) + payload


class FrameError(Exception):
    """규격에 맞지 않는 프레임."""


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """브라우저가 보낸 프레임 하나를 읽는다. (opcode, 내용)을 돌려준다."""
    b1, b2 = await reader.readexactly(2)
    fin, rsv, opcode = b1 & 0x80, b1 & 0x70, b1 & 0x0F
    masked, length = b2 & 0x80, b2 & 0x7F
    if rsv:
        raise FrameError("RSV 비트는 지원하지 않습니다")
    if not fin or opcode == 0x0:
        raise FrameError("조각난 메시지는 지원하지 않습니다")
    if not masked:
        raise FrameError("클라이언트 프레임은 반드시 마스킹되어야 합니다")
    if length == 126:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await reader.readexactly(8))[0]
    if length > MAX_FRAME:
        raise FrameError("프레임이 너무 큽니다")
    if opcode >= 0x8 and length > 125:
        raise FrameError("제어 프레임은 125바이트 이하여야 합니다")
    mask = await reader.readexactly(4)
    data = bytearray(await reader.readexactly(length))
    for i in range(length):                              # 마스크 키 4바이트를 돌려 가며 XOR
        data[i] ^= mask[i % 4]
    return opcode, bytes(data)


# ---- HTTP 도우미 ------------------------------------------------------------------
class BadRequest(Exception):
    pass


def parse_request(head: bytes) -> tuple[str, str, dict[str, str]]:
    """요청 머리를 (메서드, 경로, 헤더)로 나눈다. 헤더 이름은 소문자로 바꾼다."""
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        raise BadRequest("요청 줄이 올바르지 않습니다")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep:
            raise BadRequest("헤더 형식이 올바르지 않습니다")
        name = name.strip().lower()
        headers[name] = (headers[name] + ", " if name in headers else "") + value.strip()
    return parts[0], parts[1], headers


def http_response(status: int, body: bytes = b"", content_type: str = "text/plain; charset=utf-8",
                  extra: dict[str, str] | None = None, head_only: bool = False) -> bytes:
    lines = [f"HTTP/1.1 {status} {REASONS.get(status, '')}",
             f"Content-Type: {content_type}",
             f"Content-Length: {len(body)}",
             "Cache-Control: no-store",
             "X-Content-Type-Options: nosniff",
             "Referrer-Policy: no-referrer",
             "Connection: close"]
    for k, v in (extra or {}).items():
        lines.append(f"{k}: {v}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (b"" if head_only else body)


def http_json(status: int, payload: dict, head_only: bool = False) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return http_response(status, body, "application/json; charset=utf-8", head_only=head_only)


def host_allowed(host: str | None, bind_host: str) -> bool:
    """Host 헤더가 로컬 주소일 때만 받는다 (DNS 리바인딩 방지)."""
    if not host:
        return False
    host = host.strip().lower()
    if host.startswith("["):                      # IPv6 주소: [::1]:8000
        name = host[1:].split("]", 1)[0]
    else:                                         # 127.0.0.1:8000, localhost
        name = host.split(":", 1)[0]
    return name in LOCAL_HOSTS | {bind_host.lower()}


def origin_allowed(origin: str | None, host: str | None) -> bool:
    """Origin이 없거나(브라우저가 아닌 클라이언트) 접속한 주소와 같을 때만 받는다."""
    if origin is None:
        return True
    if not host:
        return False
    try:
        return urlsplit(origin).netloc.lower() == host.lower()
    except ValueError:
        return False


def resolve_static(root: Path, target: str) -> Path | None:
    """요청 경로를 root 안의 파일로 바꾼다. root 밖이거나 파일이 아니면 None."""
    path = unquote(urlsplit(target).path)
    if path in ("", "/"):
        path = "/index.html"
    root = root.resolve()
    candidate = (root / path.lstrip("/")).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


# ---- 접속자 ---------------------------------------------------------------------
class _Client:
    def __init__(self, max_queue: int) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max_queue)

    def offer(self, text: str) -> bool:
        """메시지를 대기열에 넣는다. 가득 차 있으면 가장 오래된 것을 버리고 True를 돌려준다."""
        dropped = False
        if self.queue.full():
            self.queue.get_nowait()
            dropped = True
        self.queue.put_nowait(text)
        return dropped


class FeedServer:
    def __init__(self, web_root: Path, host: str = "127.0.0.1", port: int = 8000,
                 hello_provider: Callable[[], dict] | None = None, max_queue: int = 200,
                 api: dict[str, Callable[[dict[str, str]], tuple[int, dict]]] | None = None,
                 on_command: Callable[[dict], dict] | None = None) -> None:
        self.web_root = Path(web_root)
        self.host = host
        self.port = port
        self.max_queue = max_queue
        self.dropped_total = 0
        self._hello_provider = hello_provider
        self._api_routes = api or {}              # 주소 -> 처리 함수 (collector/api.py)
        self._on_command = on_command             # 브라우저가 보낸 명령 처리 함수 (collector/commands.py)
        self._clients: set[_Client] = set()
        self._sessions: set[asyncio.Task] = set()
        self._server: asyncio.AbstractServer | None = None

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port, limit=MAX_HEADER)
        self.port = self._server.sockets[0].getsockname()[1]      # port=0이면 실제 배정된 포트

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        for task in list(self._sessions):
            task.cancel()
        await asyncio.gather(*self._sessions, return_exceptions=True)
        if self._server:
            await self._server.wait_closed()

    def publish(self, message: dict) -> None:
        """모든 접속자에게 메시지를 보낸다. 기다리지 않는다 (느린 접속자가 수집을 막지 못한다)."""
        text = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        for client in list(self._clients):
            if client.offer(text):
                self.dropped_total += 1

    # ---- 요청 처리 ----------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task:
            self._sessions.add(task)
        try:
            await self._serve(reader, writer)
        except (asyncio.CancelledError, ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            if task:
                self._sessions.discard(task)
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1.0)
            except (asyncio.TimeoutError, OSError, ConnectionError):
                pass

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
        except asyncio.LimitOverrunError:
            writer.write(http_response(431, "요청 머리가 너무 큽니다".encode()))
            return
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            return
        try:
            method, target, headers = parse_request(head)
        except BadRequest as e:
            writer.write(http_response(400, str(e).encode()))
            return

        if not host_allowed(headers.get("host"), self.host):
            writer.write(http_response(403, "허용되지 않는 Host입니다".encode()))
            return

        path = urlsplit(target).path
        if path == "/ws":
            await self._upgrade(reader, writer, method, headers)
        elif path.startswith("/api/"):
            self._api(writer, method, target)
        else:
            self._static(writer, method, target)

    def _api(self, writer: asyncio.StreamWriter, method: str, target: str) -> None:
        """조회 주소: 처리 함수를 불러 JSON으로 답한다. 오류 내용은 밖으로 새지 않게 한다."""
        if method not in ("GET", "HEAD"):
            writer.write(http_json(405, {"error": "GET만 지원합니다"}))
            return
        parts = urlsplit(target)
        handler = self._api_routes.get(parts.path)
        if handler is None:
            writer.write(http_json(404, {"error": "없는 주소입니다"}))
            return
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}      # 같은 이름이 여러 번이면 첫 값만
        try:
            status, payload = handler(query)
        except Exception:                                                  # 내부 오류를 화면에 그대로 보여 주지 않는다
            log.exception("API 처리 오류: %s", parts.path)
            writer.write(http_json(500, {"error": "서버에서 처리하지 못했습니다"}))
            return
        writer.write(http_json(status, payload, head_only=(method == "HEAD")))

    def _static(self, writer: asyncio.StreamWriter, method: str, target: str) -> None:
        if method not in ("GET", "HEAD"):
            writer.write(http_response(405, b"GET only", extra={"Allow": "GET, HEAD"}))
            return
        path = resolve_static(self.web_root, target)
        if path is None:
            writer.write(http_response(404, "찾을 수 없습니다".encode()))
            return
        ctype = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        writer.write(http_response(200, path.read_bytes(), ctype, head_only=(method == "HEAD")))

    # ---- WebSocket ----------------------------------------------------------
    async def _upgrade(self, reader, writer, method: str, headers: dict[str, str]) -> None:
        if method != "GET":
            writer.write(http_response(405, b"GET only", extra={"Allow": "GET"}))
            return
        wants_upgrade = (headers.get("upgrade", "").lower() == "websocket"
                         and "upgrade" in headers.get("connection", "").lower())
        key = headers.get("sec-websocket-key")
        if not wants_upgrade or not key:
            writer.write(http_response(426, "WebSocket 연결이 필요합니다".encode(),
                                       extra={"Upgrade": "websocket"}))
            return
        if headers.get("sec-websocket-version") != "13":
            writer.write(http_response(400, b"Sec-WebSocket-Version must be 13",
                                       extra={"Sec-WebSocket-Version": "13"}))
            return
        if not origin_allowed(headers.get("origin"), headers.get("host")):
            writer.write(http_response(403, "허용되지 않는 Origin입니다".encode()))
            return

        writer.write(("HTTP/1.1 101 Switching Protocols\r\n"
                      "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept_key(key)}\r\n\r\n").encode("ascii"))
        await writer.drain()
        await self._session(reader, writer)

    async def _session(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = _Client(self.max_queue)
        if self._hello_provider is not None:                      # 접속하자마자 전체 상태를 먼저 보낸다
            client.offer(json.dumps(self._hello_provider(), ensure_ascii=False, separators=(",", ":")))
        self._clients.add(client)
        sender = asyncio.create_task(self._send_loop(client, writer))
        receiver = asyncio.create_task(self._recv_loop(reader, writer, client))
        try:
            await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._clients.discard(client)
            for t in (sender, receiver):
                t.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)

    async def _send_loop(self, client: _Client, writer: asyncio.StreamWriter) -> None:
        while True:
            text = await client.queue.get()
            writer.write(encode_frame(OP_TEXT, text.encode("utf-8")))
            # 5초 안에 못 보내는 접속자는 끊는다 (TimeoutError가 나면 세션이 끝난다)
            await asyncio.wait_for(writer.drain(), SEND_TIMEOUT)

    async def _recv_loop(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                         client: _Client) -> None:
        recent: deque[float] = deque()                        # 최근에 명령을 보낸 시각들 (속도 제한용)
        while True:
            try:
                opcode, payload = await read_frame(reader)
            except FrameError:
                writer.write(encode_frame(OP_CLOSE, struct.pack(">H", 1002)))   # 1002 = 규격 위반
                return
            if opcode == OP_PING:
                writer.write(encode_frame(OP_PONG, payload))
            elif opcode == OP_CLOSE:
                writer.write(encode_frame(OP_CLOSE, payload[:2]))
                return
            elif opcode == OP_TEXT and self._on_command is not None:
                now = time.monotonic()
                while recent and now - recent[0] > COMMAND_WINDOW:
                    recent.popleft()
                if len(recent) >= COMMAND_LIMIT:               # 너무 자주 보내면 접속을 끊는다
                    writer.write(encode_frame(OP_CLOSE, struct.pack(">H", 1008)))   # 1008 = 정책 위반
                    return
                recent.append(now)
                client.offer(json.dumps(self._run_command(payload), ensure_ascii=False,
                                        separators=(",", ":")))
            # 그 밖의 메시지(바이너리, PONG 등)는 쓰지 않으니 무시한다

    def _run_command(self, payload: bytes) -> dict:
        """명령 하나를 검사하고 처리한다. 어떤 입력이 와도 답(딕셔너리)을 돌려주고 예외를 내지 않는다."""
        if len(payload) > COMMAND_MAX_BYTES:
            return {"type": "error", "message": "명령이 너무 깁니다"}
        try:
            msg = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return {"type": "error", "message": "JSON 형식이 아닙니다"}
        if not isinstance(msg, dict):
            return {"type": "error", "message": "명령은 JSON 객체여야 합니다"}
        try:
            return self._on_command(msg)
        except Exception:
            log.exception("명령 처리 오류")
            return {"type": "error", "message": "명령을 처리하지 못했습니다"}


# ---- 브라우저로 보내는 메시지 만들기 --------------------------------------------
def snapshot_dict(s: Snapshot) -> dict:
    return {
        "ts": s.timestamp, "joint1": s.joint1_deg, "joint2": s.joint2_deg,
        "gripper": s.gripper_closed, "phase": s.phase, "phase_name": s.phase_name,
        "box": s.box_detected, "count": s.count, "temp": s.temp_c,
        "status": s.status, "status_name": s.status_name,
        "cycle": s.cycle_s, "conveyor": s.conveyor_mm_s,
    }


def snapshot_message(device: str, s: Snapshot) -> dict:
    return {"type": "snapshot", "device": device, **snapshot_dict(s)}


def alarm_dict(row) -> dict:
    keys = ("id", "device", "kind", "severity", "message", "value", "last_value", "raised_at",
            "last_raised_at", "acked_at", "acked_by", "cleared_at", "repeat_count")
    out = {k: row[k] for k in keys}
    out["state"] = alarm_state(row)
    return out


def alarm_message(action: str, row) -> dict:
    return {"type": "alarm", "action": action, "alarm": alarm_dict(row)}


def event_message(e: Event) -> dict:
    return {"type": "event", "ts": e.time, "device": e.device, "kind": e.kind, "message": e.message}


def packet_state(w: DeviceWorker) -> dict:
    """패킷 뷰어용: 가장 최근에 주고받은 바이트와, 가장 최근에 실패한 바이트 (필드별 해석 포함). /api/packet이 쓴다."""
    return {"last": exchange_dict(w.last_exchange), "last_failed": exchange_dict(w.last_failed_exchange)}


def build_state(workers: list[DeviceWorker], store: Store, now: float | None = None,
                kpi: dict | None = None) -> dict:
    """지금 상태 전체. 끊긴 설비의 마지막 값은 data_age(몇 초 전 값인지)로 구분할 수 있다."""
    now = time.time() if now is None else now
    devices = []
    for w in workers:
        s = w.stats
        age = s.data_age(now)
        devices.append({
            "name": w.name, "state": s.state.name, "state_label": s.state.value,
            "reads_ok": s.reads_ok, "reads_failed": s.reads_failed, "reconnects": s.reconnects,
            "consecutive_failures": s.consecutive_failures,
            "avg_latency_ms": round(s.avg_latency_ms, 1), "last_error": s.last_error,
            "data_age": None if age is None else round(age, 1),
            "snapshot": snapshot_dict(s.last_snapshot) if s.last_snapshot else None,
            # 패킷 기록(바이트와 필드별 해석)은 설비당 2~3KB라서 여기에 넣지 않는다. 1초마다 모든 접속자에게 가는
            # 이 메시지가 설비 수에 비례해 커지기 때문이다. 화면에서 고른 설비 하나만 /api/packet으로 가져간다.
        })
    state = {"type": "state", "time": now, "devices": devices,
             "alarms": [alarm_dict(r) for r in store.open_alarms()[:50]]}
    if kpi is not None:
        state["kpi"] = kpi
    return state
