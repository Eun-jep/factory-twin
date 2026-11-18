"""전송 서버의 조회 주소(HTTP)와 확인 명령(WebSocket) 시험, 그리고 build_state의 패킷·지표 부분.

    python -m unittest discover -s tests -v
"""
import asyncio
import json
import struct
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import snap  # noqa: E402
from test_webfeed import (  # noqa: E402
    FeedServerCase, client_frame, http_request, read_message, read_server_frame, ws_connect)

from collector import webfeed  # noqa: E402
from collector.alarms import AlarmManager  # noqa: E402
from collector.commands import handle_command  # noqa: E402
from collector.device import DeviceWorker  # noqa: E402
from collector.packets import Exchange  # noqa: E402
from collector.stats import compute_kpis  # noqa: E402
from collector.store import Store  # noqa: E402
from collector.webfeed import OP_CLOSE, OP_TEXT, build_state, packet_state  # noqa: E402

OP_BINARY = 0x2


def ok_handler(query):
    return 200, {"echo": query}


def boom_handler(query):
    raise RuntimeError("비밀 경로 C:\\secret\\db.sqlite 를 열 수 없음")


class ApiOverHttpTest(FeedServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.start_server(api={"/api/ok": ok_handler, "/api/boom": boom_handler,
                                     "/api/missing": lambda q: (404, {"error": "없음"})})

    async def test_json_response_and_query(self):
        status, headers, body = await http_request(self.port, "/api/ok?a=1&b=%ED%99%8D&a=2")
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("application/json"))
        self.assertEqual(json.loads(body), {"echo": {"a": "1", "b": "홍"}})       # 같은 이름은 첫 값만
        self.assertEqual(int(headers["content-length"]), len(body))

    async def test_handler_status_is_passed_through(self):
        status, _, body = await http_request(self.port, "/api/missing")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "없음"})

    async def test_unknown_api_path_is_404_json(self):
        status, headers, body = await http_request(self.port, "/api/nothing")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))
        self.assertTrue(headers["content-type"].startswith("application/json"))

    async def test_post_is_405(self):
        status, _, body = await http_request(self.port, "/api/ok", method="POST")
        self.assertEqual(status, 405)
        self.assertIn("error", json.loads(body))

    async def test_head_has_headers_but_no_body(self):
        status, headers, body = await http_request(self.port, "/api/ok", method="HEAD")
        self.assertEqual((status, body), (200, b""))
        self.assertGreater(int(headers["content-length"]), 0)

    async def test_internal_error_is_500_without_leaking_details(self):
        with self.assertLogs("collector.webfeed", level="ERROR"):
            status, _, body = await http_request(self.port, "/api/boom")
        self.assertEqual(status, 500)
        self.assertNotIn(b"secret", body)
        self.assertNotIn(b"RuntimeError", body)
        status, _, _ = await http_request(self.port, "/api/ok")                    # 서버는 계속 살아 있다
        self.assertEqual(status, 200)

    async def test_host_check_applies_to_api(self):
        status, _, _ = await http_request(self.port, "/api/ok", headers={"Host": "evil.example.com"})
        self.assertEqual(status, 403)

    async def test_api_prefix_never_falls_through_to_static_files(self):
        (self.root / "api").mkdir()
        (self.root / "api" / "x.txt").write_text("static", encoding="utf-8")
        status, _, body = await http_request(self.port, "/api/x.txt")
        self.assertEqual(status, 404)
        self.assertNotIn(b"static", body)

    async def test_no_api_routes_means_404(self):
        await self.start_server()                                                   # api 없이
        status, _, _ = await http_request(self.port, "/api/ok")
        self.assertEqual(status, 404)


class CommandOverWebSocketTest(FeedServerCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.store = Store()
        self.addCleanup(self.store.close)
        self.manager = AlarmManager(self.store)
        self.manager.on_snapshot("ARM-01", snap(10.0, temp=66.0, status=1))
        self.alarm_id = self.store.open_alarms()[0]["id"]
        self.calls = []
        await self.start_server(on_command=self.on_command)

    def on_command(self, msg):
        self.calls.append(msg)
        if msg.get("type") == "explode":
            raise RuntimeError("내부 오류 내용")
        return handle_command(msg, self.manager, self.store)

    async def open(self):
        reader, writer = await self.connect_ws()
        self.assertEqual((await read_message(reader))["type"], "hello")
        return reader, writer

    async def send(self, writer, payload, opcode=OP_TEXT):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        writer.write(client_frame(opcode, data))
        await writer.drain()

    async def test_ack_roundtrip_and_second_ack_is_refused(self):
        reader, writer = await self.open()
        await self.send(writer, {"type": "ack", "id": self.alarm_id, "by": "홍길동"})
        reply = await read_message(reader)
        self.assertEqual(reply, {"type": "ack_result", "id": self.alarm_id, "ok": True, "reason": None})
        self.assertEqual(self.store.get_alarm(self.alarm_id)["acked_by"], "홍길동")
        await self.send(writer, {"type": "ack", "id": self.alarm_id, "by": "다른사람"})
        again = await read_message(reader)
        self.assertFalse(again["ok"])

    async def test_reply_goes_only_to_the_sender(self):
        r1, w1 = await self.open()
        r2, w2 = await self.open()
        await self.send(w1, {"type": "ack", "id": self.alarm_id, "by": "홍길동"})
        self.assertEqual((await read_message(r1))["type"], "ack_result")
        with self.assertRaises(asyncio.TimeoutError):
            await read_server_frame(r2, timeout=0.3)

    async def test_bad_input_gets_an_error_reply_and_the_connection_survives(self):
        reader, writer = await self.open()
        cases = [b"this is not json", b"\xff\xfe\xfd", b"[1,2,3]", b"3", b"null", b'"ack"',
                 json.dumps({"type": "x" * 10}).encode(), b"{" * 1000]
        for payload in cases:
            with self.subTest(payload=payload[:20]):
                await self.send(writer, payload)
                self.assertEqual((await read_message(reader))["type"], "error")
        await self.send(writer, {"type": "ack", "id": self.alarm_id, "by": "홍길동"})        # 아직 정상 동작
        self.assertTrue((await read_message(reader))["ok"])

    async def test_oversize_command_is_rejected_before_parsing(self):
        reader, writer = await self.open()
        big = json.dumps({"type": "ack", "id": 1, "by": "x", "pad": "a" * webfeed.COMMAND_MAX_BYTES}).encode()
        await self.send(writer, big)
        reply = await read_message(reader)
        self.assertEqual(reply["type"], "error")
        self.assertIn("너무", reply["message"])
        self.assertEqual(self.calls, [])                                           # 처리 함수까지 가지 않았다

    async def test_exactly_at_the_size_limit_is_still_parsed(self):
        reader, writer = await self.open()
        base = json.dumps({"type": "ping", "pad": ""}).encode()
        payload = json.dumps({"type": "ping", "pad": "a" * (webfeed.COMMAND_MAX_BYTES - len(base))}).encode()
        self.assertEqual(len(payload), webfeed.COMMAND_MAX_BYTES)
        await self.send(writer, payload)
        await read_message(reader)
        self.assertEqual(len(self.calls), 1)

    async def test_handler_exception_is_hidden_and_connection_survives(self):
        reader, writer = await self.open()
        with self.assertLogs("collector.webfeed", level="ERROR"):
            await self.send(writer, {"type": "explode"})
            reply = await read_message(reader)
        self.assertEqual(reply["type"], "error")
        self.assertNotIn("내부 오류 내용", json.dumps(reply, ensure_ascii=False))
        await self.send(writer, {"type": "ack", "id": self.alarm_id, "by": "홍길동"})
        self.assertTrue((await read_message(reader))["ok"])

    async def test_binary_frames_are_ignored(self):
        reader, writer = await self.open()
        await self.send(writer, b"\x00\x01\x02", opcode=OP_BINARY)
        with self.assertRaises(asyncio.TimeoutError):
            await read_server_frame(reader, timeout=0.3)
        self.assertEqual(self.calls, [])

    async def test_too_many_commands_close_the_connection_with_1008(self):
        reader, writer = await self.open()
        for _ in range(webfeed.COMMAND_LIMIT):
            await self.send(writer, {"type": "ping"})
        for _ in range(webfeed.COMMAND_LIMIT):
            self.assertEqual((await read_message(reader))["type"], "error")      # 한도 안의 명령은 모두 답을 받는다
        await self.send(writer, {"type": "ping"})                                  # 한도를 넘는 명령
        opcode, payload = await read_server_frame(reader)
        self.assertEqual(opcode, OP_CLOSE)
        self.assertEqual(struct.unpack(">H", payload[:2])[0], 1008)
        self.assertEqual(len(self.calls), webfeed.COMMAND_LIMIT)                   # 넘은 명령은 처리하지 않았다

    async def test_rate_limit_is_per_connection(self):
        r1, w1 = await self.open()
        for _ in range(webfeed.COMMAND_LIMIT + 1):
            await self.send(w1, {"type": "ping"})
        r2, w2 = await self.open()
        await self.send(w2, {"type": "ack", "id": self.alarm_id, "by": "홍길동"})
        self.assertTrue((await read_message(r2))["ok"])                            # 다른 접속은 영향이 없다

    async def test_rate_limit_window_slides(self):
        old = webfeed.COMMAND_WINDOW
        webfeed.COMMAND_WINDOW = 0.3
        self.addCleanup(setattr, webfeed, "COMMAND_WINDOW", old)
        reader, writer = await self.open()
        for _ in range(webfeed.COMMAND_LIMIT):
            await self.send(writer, {"type": "ping"})
            await read_message(reader)
        await asyncio.sleep(0.4)                                                   # 창이 지나가면 다시 보낼 수 있다
        await self.send(writer, {"type": "ping"})
        self.assertEqual((await read_message(reader))["type"], "error")             # 닫기 프레임이 아니라 일반 답

    async def test_commands_are_ignored_when_no_handler(self):
        await self.start_server()                                                   # on_command 없음
        reader, writer = await self.open()
        await self.send(writer, {"type": "ack", "id": 1, "by": "x"})
        with self.assertRaises(asyncio.TimeoutError):
            await read_server_frame(reader, timeout=0.3)


class BuildStateTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)
        self.worker = DeviceWorker("ARM-01", "127.0.0.1", 1)

    def test_packet_is_none_before_any_exchange(self):
        self.assertEqual(packet_state(self.worker), {"last": None, "last_failed": None})
        self.assertNotIn("kpi", build_state([self.worker], self.store, now=100.0))

    def test_state_does_not_carry_packets(self):
        """패킷 기록은 설비당 2~3KB라 1초 방송에서 뺐다 (/api/packet으로 고른 설비 하나만 가져간다)."""
        request = bytes.fromhex("0001 0000 0006 01 03 0000 000a")
        response = bytes.fromhex("0001 0000 0017 01 03 14") + bytes(20)
        self.worker._client.last_exchange = Exchange(50.0, 1, request, response, None)
        state = build_state([self.worker], self.store, now=100.0)
        self.assertNotIn("packet", state["devices"][0])

    def test_state_size_stays_small_as_devices_grow(self):
        """설비 100대여도 state 메시지는 설비당 600바이트 안쪽이어야 한다 (패킷을 넣으면 설비당 2.6KB였다)."""
        request = bytes.fromhex("0001 0000 0006 01 03 0000 000a")
        response = bytes.fromhex("0001 0000 0017 01 03 14") + bytes(20)
        workers = [DeviceWorker(f"ARM-{i:03d}", "127.0.0.1", 1) for i in range(100)]
        for w in workers:
            w._client.last_exchange = Exchange(50.0, 1, request, response, None)
            w._client.last_failed_exchange = Exchange(40.0, 0, request, None, "시간 초과")
        size = len(json.dumps(build_state(workers, self.store, now=100.0)))
        self.assertLess(size / 100, 600, f"설비당 {size / 100:.0f}바이트")

    def test_packet_contains_annotated_fields(self):
        request = bytes.fromhex("0001 0000 0006 01 03 0000 000a")
        response = bytes.fromhex("0001 0000 0017 01 03 14") + bytes(20)
        self.worker._client.last_exchange = Exchange(50.0, 1, request, response, None)
        self.worker._client.last_failed_exchange = Exchange(40.0, 0, request, None, "시간 초과")
        packet = packet_state(self.worker)
        self.assertEqual(packet["last"]["tid"], 1)
        self.assertIsNone(packet["last"]["error"])
        self.assertEqual(packet["last"]["response"]["fields"][-1]["name"], "레지스터 9: 컨베이어 속도")
        self.assertEqual(packet["last_failed"]["error"], "시간 초과")
        self.assertIsNone(packet["last_failed"]["response"])
        json.dumps(packet)                                                           # JSON으로 보낼 수 있다

    def test_kpi_is_attached_only_when_given(self):
        kpi = compute_kpis(self.store, [self.worker], now=100.0)
        state = build_state([self.worker], self.store, now=100.0, kpi=kpi)
        self.assertIs(state["kpi"], kpi)
        json.dumps(state)


if __name__ == "__main__":
    unittest.main()
