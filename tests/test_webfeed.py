"""브라우저 전송 서버(직접 구현한 WebSocket + 정적 파일) 시험.

    python -m unittest discover -s tests -v
"""
import asyncio
import json
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import snap  # noqa: E402

import robot_sim as rs  # noqa: E402
from collector import webfeed  # noqa: E402
from collector.alarms import AlarmManager  # noqa: E402
from collector.device import DeviceWorker  # noqa: E402
from collector.store import Store  # noqa: E402
from collector.webfeed import (  # noqa: E402
    FeedServer, FrameError, OP_CLOSE, OP_PING, OP_PONG, OP_TEXT, accept_key, build_state,
    encode_frame, host_allowed, origin_allowed, parse_request, read_frame, resolve_static,
    snapshot_message,
)
from modbus_sim import SimServer  # noqa: E402

MASK = b"\x01\x02\x03\x04"


# ---- 시험용 클라이언트 쪽 도우미 -------------------------------------------------
def client_frame(opcode, payload=b"", *, fin=True, masked=True, rsv=0, mask=MASK):
    """브라우저가 보내는 프레임을 만든다 (클라이언트 프레임은 마스킹해야 한다)."""
    b1 = (0x80 if fin else 0) | rsv | opcode
    flag = 0x80 if masked else 0
    n = len(payload)
    if n < 126:
        head = bytes([b1, flag | n])
    elif n < 65536:
        head = bytes([b1, flag | 126]) + struct.pack(">H", n)
    else:
        head = bytes([b1, flag | 127]) + struct.pack(">Q", n)
    if masked:
        return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return head + payload


async def read_server_frame(reader, timeout=3.0):
    """서버가 보낸 프레임 하나를 읽는다 (서버 프레임은 마스킹되지 않는다)."""
    async def inner():
        b1, b2 = await reader.readexactly(2)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack(">H", await reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", await reader.readexactly(8))[0]
        return b1 & 0x0F, await reader.readexactly(length)
    return await asyncio.wait_for(inner(), timeout)


async def read_message(reader):
    opcode, payload = await read_server_frame(reader)
    assert opcode == OP_TEXT, f"텍스트 프레임이 아닙니다: {opcode}"
    return json.loads(payload.decode("utf-8"))


async def http_request(port, path="/", method="GET", headers=None, raw=None):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    if raw is None:
        h = {"Host": f"127.0.0.1:{port}", **(headers or {})}
        raw = (f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items())
               + "\r\n").encode()
    writer.write(raw)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 5.0)
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    hdrs = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    return status, hdrs, body


async def ws_connect(port, extra=None, key="dGhlIHNhbXBsZSBub25jZQ==", drop=()):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    h = {"Host": f"127.0.0.1:{port}", "Upgrade": "websocket", "Connection": "Upgrade",
         "Sec-WebSocket-Key": key, "Sec-WebSocket-Version": "13", **(extra or {})}
    for name in drop:
        h.pop(name, None)
    writer.write((f"GET /ws HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items())
                  + "\r\n").encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3.0)
    return reader, writer, head


# ---- 순수 함수 ------------------------------------------------------------------
class PureFunctionTest(unittest.TestCase):
    def test_accept_key_matches_rfc6455_example(self):
        # RFC 6455 1.3절의 공식 예제
        self.assertEqual(accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_encode_frame_lengths(self):
        self.assertEqual(encode_frame(OP_TEXT, b"hi"), b"\x81\x02hi")
        mid = encode_frame(OP_TEXT, b"x" * 200)
        self.assertEqual(mid[:4], b"\x81\x7e\x00\xc8")                  # 126 + 16비트 길이
        big = encode_frame(OP_TEXT, b"x" * 70000)
        self.assertEqual(big[:10], b"\x81\x7f" + struct.pack(">Q", 70000))   # 127 + 64비트 길이
        self.assertEqual(encode_frame(OP_PONG, b"")[:2], b"\x8a\x00")

    def test_encode_frame_length_boundaries(self):
        # 규격: 가장 짧은 길이 표현을 써야 한다 (125 이하 1바이트, 65535 이하 3바이트, 그 이상 9바이트)
        cases = [(0, 2), (125, 2), (126, 4), (65535, 4), (65536, 10)]
        for size, header_len in cases:
            with self.subTest(size):
                frame = encode_frame(OP_TEXT, b"x" * size)
                self.assertEqual(len(frame), header_len + size)
        self.assertEqual(encode_frame(OP_TEXT, b"x" * 125)[1], 125)
        self.assertEqual(encode_frame(OP_TEXT, b"x" * 126)[1], 126)
        self.assertEqual(encode_frame(OP_TEXT, b"x" * 65535)[1], 126)
        self.assertEqual(encode_frame(OP_TEXT, b"x" * 65536)[1], 127)

    def test_host_allowed(self):
        cases = {
            "127.0.0.1:8000": True, "localhost": True, "localhost:8000": True, "LOCALHOST:8000": True,
            "[::1]:8000": True, "evil.example:8000": False, "evil.example": False,
            "127.0.0.1.evil.example": False, "": False, None: False,
        }
        for host, expected in cases.items():
            with self.subTest(host):
                self.assertEqual(host_allowed(host, "127.0.0.1"), expected)

    def test_host_allowed_includes_bind_host(self):
        self.assertTrue(host_allowed("0.0.0.0:8000", "0.0.0.0"))
        self.assertFalse(host_allowed("192.168.0.5:8000", "127.0.0.1"))

    def test_origin_allowed(self):
        cases = [
            (None, "127.0.0.1:8000", True),                        # 브라우저가 아닌 클라이언트
            ("http://127.0.0.1:8000", "127.0.0.1:8000", True),
            ("http://localhost:8000", "localhost:8000", True),
            ("http://evil.example", "127.0.0.1:8000", False),      # 다른 웹사이트
            ("http://127.0.0.1:9999", "127.0.0.1:8000", False),    # 포트가 다름
            ("null", "127.0.0.1:8000", False),
            ("http://127.0.0.1:8000", None, False),
        ]
        for origin, host, expected in cases:
            with self.subTest(origin=origin, host=host):
                self.assertEqual(origin_allowed(origin, host), expected)

    def test_parse_request(self):
        method, target, headers = parse_request(b"GET /ws HTTP/1.1\r\nHost: a\r\nX-Foo: 1\r\nX-Foo: 2\r\n\r\n")
        self.assertEqual((method, target), ("GET", "/ws"))
        self.assertEqual(headers["host"], "a")
        self.assertEqual(headers["x-foo"], "1, 2")                  # 같은 헤더는 합친다
        for bad in (b"GET /\r\n\r\n", b"GET / FTP/1.0\r\n\r\n", b"GET / HTTP/1.1\r\nbroken\r\n\r\n"):
            with self.subTest(bad), self.assertRaises(webfeed.BadRequest):
                parse_request(bad)

    def test_resolve_static_stays_inside_root(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            root = Path(d) / "web"
            (root / "sub").mkdir(parents=True)
            (root / "index.html").write_text("home", encoding="utf-8")
            (root / "sub" / "x.js").write_text("js", encoding="utf-8")
            (Path(d) / "secret.txt").write_text("secret", encoding="utf-8")
            self.assertEqual(resolve_static(root, "/").name, "index.html")
            self.assertEqual(resolve_static(root, "/sub/x.js").name, "x.js")
            self.assertEqual(resolve_static(root, "/index.html?v=1").name, "index.html")
            for target in ("/../secret.txt", "/%2e%2e/secret.txt", "/sub/../../secret.txt",
                           "/..%2fsecret.txt", "/missing.html", "/sub"):
                with self.subTest(target):
                    self.assertIsNone(resolve_static(root, target))


# ---- 프레임 읽기 ----------------------------------------------------------------
class ReadFrameTest(unittest.IsolatedAsyncioTestCase):
    def reader_with(self, data):
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        return reader

    async def test_masked_text(self):
        op, payload = await read_frame(self.reader_with(client_frame(OP_TEXT, "안녕".encode())))
        self.assertEqual((op, payload.decode()), (OP_TEXT, "안녕"))

    async def test_length_boundaries(self):
        for size in (0, 125, 126, 300, 65535, 65536):
            with self.subTest(size):
                _, payload = await read_frame(self.reader_with(client_frame(OP_TEXT, b"a" * size)))
                self.assertEqual(len(payload), size)

    async def test_ping_payload_is_kept(self):
        op, payload = await read_frame(self.reader_with(client_frame(OP_PING, b"abc")))
        self.assertEqual((op, payload), (OP_PING, b"abc"))

    async def test_rejects_bad_frames(self):
        cases = {
            "마스킹 없음": client_frame(OP_TEXT, b"x", masked=False),
            "조각난 메시지": client_frame(OP_TEXT, b"x", fin=False),
            "RSV 비트": client_frame(OP_TEXT, b"x", rsv=0x40),
            "너무 긴 제어 프레임": client_frame(OP_PING, b"x" * 126),
            "너무 큰 프레임": bytes([0x81, 0x80 | 127]) + struct.pack(">Q", 1 << 21) + MASK,
        }
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(FrameError):
                await read_frame(self.reader_with(data))

    async def test_truncated_frame_raises_incomplete_read(self):
        data = client_frame(OP_TEXT, b"hello")[:-2]
        with self.assertRaises(asyncio.IncompleteReadError):
            await read_frame(self.reader_with(data))


# ---- 서버 ------------------------------------------------------------------------
class FeedServerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.root = base / "web"
        self.root.mkdir()
        self.index = "<!doctype html><title>시험</title>".encode("utf-8")
        (self.root / "index.html").write_bytes(self.index)
        (self.root / "app.js").write_text("console.log(1)", encoding="utf-8")
        (base / "secret.txt").write_text("secret", encoding="utf-8")
        await self.start_server()

    async def start_server(self, **kwargs):
        kwargs.setdefault("hello_provider", lambda: {"type": "hello", "n": 1})
        self.server = FeedServer(self.root, "127.0.0.1", 0, **kwargs)
        await self.server.start()
        self.addAsyncCleanup(self.server.stop)
        self.port = self.server.port

    async def wait_until(self, condition, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                return
            await asyncio.sleep(0.02)
        self.fail("조건이 시간 안에 충족되지 않았습니다.")

    async def connect_ws(self, **kwargs):
        reader, writer, head = await ws_connect(self.port, **kwargs)
        self.assertIn(b"101", head.split(b"\r\n")[0])
        self.addCleanup(writer.close)
        return reader, writer


class StaticFileTest(FeedServerCase):
    async def test_index_and_content_types(self):
        status, headers, body = await http_request(self.port, "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, self.index)
        self.assertTrue(headers["content-type"].startswith("text/html"))
        _, headers, _ = await http_request(self.port, "/app.js")
        self.assertTrue(headers["content-type"].startswith("text/javascript"))     # 윈도우 설정과 무관하게
        self.assertEqual(headers["x-content-type-options"], "nosniff")

    async def test_head_has_no_body(self):
        status, headers, body = await http_request(self.port, "/", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(int(headers["content-length"]), len(self.index))

    async def test_path_traversal_is_blocked(self):
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/..%2fsecret.txt"):
            with self.subTest(path):
                status, _, body = await http_request(self.port, path)
                self.assertEqual(status, 404)
                self.assertNotIn(b"secret", body)

    async def test_unknown_path_and_methods(self):
        self.assertEqual((await http_request(self.port, "/nope.html"))[0], 404)
        status, headers, _ = await http_request(self.port, "/", method="POST")
        self.assertEqual(status, 405)
        self.assertEqual(headers["allow"], "GET, HEAD")

    async def test_bad_requests(self):
        self.assertEqual((await http_request(self.port, raw=b"NONSENSE\r\n\r\n"))[0], 400)
        huge = b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Big: " + b"a" * 20000 + b"\r\n\r\n"
        self.assertEqual((await http_request(self.port, raw=huge))[0], 431)

    async def test_foreign_host_header_is_rejected(self):
        status, _, _ = await http_request(self.port, "/", headers={"Host": "evil.example:8000"})
        self.assertEqual(status, 403)                                  # DNS 리바인딩 방지
        status, _, _ = await http_request(self.port, "/", headers={"Host": f"localhost:{self.port}"})
        self.assertEqual(status, 200)


class HandshakeTest(FeedServerCase):
    async def test_upgrade_returns_correct_accept_key(self):
        reader, writer, head = await ws_connect(self.port)
        writer.close()
        self.assertIn(b"101 Switching Protocols", head)
        self.assertIn(b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", head)

    async def test_rejects_incomplete_handshakes(self):
        cases = {
            "키 없음": ({}, ("Sec-WebSocket-Key",), 426),
            "Upgrade 헤더 없음": ({}, ("Upgrade",), 426),
            "버전이 다름": ({"Sec-WebSocket-Version": "8"}, (), 400),
        }
        for label, (extra, drop, expected) in cases.items():
            with self.subTest(label):
                reader, writer, head = await ws_connect(self.port, extra=extra, drop=drop)
                writer.close()
                self.assertIn(str(expected).encode(), head.split(b"\r\n")[0])

    async def test_origin_rules(self):
        for origin, expected in ((f"http://127.0.0.1:{self.port}", b"101"),
                                 ("http://evil.example", b"403")):
            with self.subTest(origin):
                reader, writer, head = await ws_connect(self.port, extra={"Origin": origin})
                writer.close()
                self.assertIn(expected, head.split(b"\r\n")[0])

    async def test_foreign_host_cannot_upgrade(self):
        reader, writer, head = await ws_connect(self.port, extra={"Host": "evil.example"})
        writer.close()
        self.assertIn(b"403", head.split(b"\r\n")[0])


class MessagingTest(FeedServerCase):
    async def test_hello_is_first_message(self):
        reader, _ = await self.connect_ws()
        self.assertEqual(await read_message(reader), {"type": "hello", "n": 1})

    async def test_publish_reaches_all_clients_with_korean_text(self):
        r1, _ = await self.connect_ws()
        r2, _ = await self.connect_ws()
        await read_message(r1)
        await read_message(r2)
        self.assertEqual(self.server.client_count, 2)
        self.server.publish({"type": "event", "message": "연결 끊김"})
        for reader in (r1, r2):
            self.assertEqual(await read_message(reader), {"type": "event", "message": "연결 끊김"})

    async def test_ping_gets_pong_with_same_payload(self):
        reader, writer = await self.connect_ws()
        await read_message(reader)
        writer.write(client_frame(OP_PING, b"hello"))
        await writer.drain()
        self.assertEqual(await read_server_frame(reader), (OP_PONG, b"hello"))

    async def test_close_handshake(self):
        reader, writer = await self.connect_ws()
        await read_message(reader)
        writer.write(client_frame(OP_CLOSE, struct.pack(">H", 1000)))
        await writer.drain()
        op, payload = await read_server_frame(reader)
        self.assertEqual((op, payload), (OP_CLOSE, struct.pack(">H", 1000)))
        self.assertEqual(await asyncio.wait_for(reader.read(), 3.0), b"")        # 서버가 연결을 닫는다
        await self.wait_until(lambda: self.server.client_count == 0)

    async def test_unmasked_client_frame_closes_with_1002(self):
        reader, writer = await self.connect_ws()
        await read_message(reader)
        writer.write(client_frame(OP_TEXT, b"x", masked=False))
        await writer.drain()
        op, payload = await read_server_frame(reader)
        self.assertEqual((op, struct.unpack(">H", payload)[0]), (OP_CLOSE, 1002))
        await self.wait_until(lambda: self.server.client_count == 0)

    async def test_abrupt_disconnect_is_cleaned_up(self):
        reader, writer = await self.connect_ws()
        await read_message(reader)
        self.assertEqual(self.server.client_count, 1)
        writer.close()
        await self.wait_until(lambda: self.server.client_count == 0)

    async def test_stop_closes_clients(self):
        reader, _ = await self.connect_ws()
        await read_message(reader)
        await self.server.stop()
        self.assertEqual(self.server.client_count, 0)
        rest = await asyncio.wait_for(reader.read(), 3.0)                         # 연결이 끊긴다
        self.assertIsInstance(rest, bytes)

    async def test_publish_without_clients_is_fine(self):
        self.server.publish({"type": "x"})
        self.assertEqual(self.server.dropped_total, 0)


class SlowClientTest(FeedServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.server.stop()
        await self.start_server(max_queue=20)

    async def test_slow_client_does_not_block_publish(self):
        slow_reader, slow_writer = await self.connect_ws()        # 연결만 하고 읽지 않는다
        fast_reader, _ = await self.connect_ws()
        await read_message(fast_reader)

        big = "x" * 20000
        started = time.monotonic()
        for i in range(2000):                                     # 약 40MB
            self.server.publish({"type": "snapshot", "i": i, "pad": big})
            if i % 50 == 0:
                await asyncio.sleep(0)                            # 전송 작업에 차례를 준다
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3.0)                             # 느린 접속자를 기다리지 않았다
        self.assertGreater(self.server.dropped_total, 0)          # 대신 오래된 메시지를 버렸다
        slow_writer.close()


class MessageBuilderTest(unittest.TestCase):
    def test_snapshot_message(self):
        msg = snapshot_message("ARM-01", snap(100.0, count=7, temp=66.4, status=1))
        self.assertEqual(msg["type"], "snapshot")
        self.assertEqual(msg["device"], "ARM-01")
        self.assertEqual((msg["count"], msg["temp"], msg["status"], msg["status_name"]), (7, 66.4, 1, "주의"))
        self.assertEqual((msg["joint1"], msg["gripper"], msg["phase_name"]), (90.0, False, "대기"))
        json.dumps(msg)                                           # JSON으로 바꿀 수 있어야 한다

    def test_build_state_before_any_data(self):
        store = Store()
        self.addCleanup(store.close)
        state = build_state([DeviceWorker("ARM-01", "127.0.0.1", 1)], store, now=500.0)
        dev = state["devices"][0]
        self.assertEqual((state["type"], dev["name"], dev["state"]), ("state", "ARM-01", "CONNECTING"))
        self.assertIsNone(dev["snapshot"])
        self.assertIsNone(dev["data_age"])
        self.assertEqual(state["alarms"], [])

    def test_build_state_includes_alarms_with_state_text(self):
        store = Store()
        self.addCleanup(store.close)
        AlarmManager(store).on_snapshot("ARM-01", snap(10.0, temp=66.0, status=1))
        alarms = build_state([], store)["alarms"]
        self.assertEqual(len(alarms), 1)
        self.assertEqual((alarms[0]["kind"], alarms[0]["state"], alarms[0]["repeat_count"]),
                         ("TEMP_WARNING", "활성·미확인", 1))
        json.dumps(alarms)


class EndToEndTest(FeedServerCase):
    async def test_device_values_reach_the_browser(self):
        """가상 로봇 -> 수집 워커 -> 브라우저 전송 서버 -> WebSocket 클라이언트"""
        sim = SimServer(rs.RobotSim("ARM-X", seed=1), "127.0.0.1", 0)
        sim.start()
        self.addCleanup(sim.stop)
        worker = DeviceWorker("ARM-X", "127.0.0.1", sim.port, interval=0.05, timeout=0.3,
                              on_snapshot=lambda name, s: self.server.publish(snapshot_message(name, s)))
        task = asyncio.create_task(worker.run())

        async def cleanup():
            worker.stop()
            await task
        self.addAsyncCleanup(cleanup)

        reader, _ = await self.connect_ws()
        await read_message(reader)                                # hello
        msg = await read_message(reader)                          # 첫 snapshot
        self.assertEqual(msg["type"], "snapshot")
        self.assertEqual(msg["device"], "ARM-X")
        self.assertEqual(msg["joint1"], 90.0)                     # 가상 로봇의 초기 자세
        self.assertEqual(msg["phase_name"], "대기")


if __name__ == "__main__":
    unittest.main()
