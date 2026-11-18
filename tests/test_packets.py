"""패킷 기록과 해석 시험.

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
import helpers  # noqa: F401,E402  (경로 설정)

import robot_sim as rs  # noqa: E402
from collector import models  # noqa: E402
from collector.device import DeviceWorker  # noqa: E402
from collector.modbus import (  # noqa: E402
    ModbusError, ModbusTcpClient, build_read_request,
)
from collector.packets import (  # noqa: E402
    Exchange, annotate_request, annotate_response, exchange_dict, hex_bytes,
)
from modbus_sim import Faults, SimServer, build_response, handle_pdu  # noqa: E402


def make_response(tid, regs, unit=1):
    pdu = bytes([0x03, len(regs) * 2]) + b"".join(struct.pack(">H", r) for r in regs)
    return build_response(tid, unit, pdu)


# 예: 관절1 45.2도, 관절2 30.0도, 집게 열림, 단계 집기(2), 감지 없음, 카운트 7, 온도 66.4, 상태 주의, 6.6초, 500mm/s
REGS = [452, 300, 0, 2, 0, 7, 664, 1, 66, 500]


def covered_hex(fields):
    """필드들의 hex를 이어 붙인 것 (빈 필드는 건너뜀)."""
    return " ".join(f["hex"] for f in fields if f["hex"])


class AnnotateRequestTest(unittest.TestCase):
    def test_fields_of_a_read_request(self):
        request = build_read_request(42, 1, 0, 10)
        fields = annotate_request(request)
        self.assertEqual(hex_bytes(request), "00 2a 00 00 00 06 01 03 00 00 00 0a")
        self.assertEqual([(f["name"], f["hex"], f["group"]) for f in fields], [
            ("트랜잭션 번호", "00 2a", "header"),
            ("프로토콜 ID", "00 00", "header"),
            ("길이", "00 06", "header"),
            ("유닛 ID", "01", "header"),
            ("기능 코드", "03", "function"),
            ("시작 주소", "00 00", "address"),
            ("개수", "00 0a", "count"),
        ])
        self.assertEqual(fields[0]["value"], "42")
        self.assertIn("정상", fields[1]["value"])
        self.assertEqual(covered_hex(fields), hex_bytes(request))      # 빠지거나 겹치는 바이트가 없다

    def test_other_start_address_and_count(self):
        fields = annotate_request(build_read_request(1, 3, 100, 2))
        by_name = {f["name"]: f for f in fields}
        self.assertEqual(by_name["시작 주소"]["value"], "100")
        self.assertIn("2", by_name["개수"]["value"])
        self.assertEqual(by_name["유닛 ID"]["value"], "3")

    def test_unknown_request_is_marked_as_error(self):
        for data in (b"", b"\x00\x01", bytes(12)):
            with self.subTest(data=data):
                fields = annotate_request(data)
                self.assertEqual([f["group"] for f in fields], ["error"])


class AnnotateResponseTest(unittest.TestCase):
    def setUp(self):
        self.data = make_response(42, REGS)
        self.fields = annotate_response(self.data)

    def test_structure_and_length(self):
        self.assertEqual(len(self.data), 29)                              # 7 + 1 + 1 + 20
        self.assertEqual(len(self.fields), 4 + 1 + 1 + 10)               # 헤더 4, 기능, 바이트 수, 레지스터 10
        self.assertEqual([f["group"] for f in self.fields],
                         ["header"] * 4 + ["function", "count"] + ["data"] * 10)

    def test_every_byte_is_explained_exactly_once(self):
        self.assertEqual(covered_hex(self.fields), hex_bytes(self.data))

    def test_register_values_are_translated(self):
        data_fields = [f for f in self.fields if f["group"] == "data"]
        self.assertEqual(data_fields[0]["name"], "레지스터 0: 관절1")
        self.assertEqual(data_fields[0]["hex"], "01 c4")
        self.assertEqual(data_fields[0]["value"], "452 -> 45.2°")
        by_label = {f["name"].split(": ")[1]: f["value"] for f in data_fields}
        self.assertEqual(by_label["로봇 단계"], "2 -> 집기")
        self.assertEqual(by_label["온도"], "664 -> 66.4℃")
        self.assertEqual(by_label["상태"], "1 -> 주의")
        self.assertEqual(by_label["집게"], "0 -> 열림")
        self.assertEqual(by_label["컨베이어 속도"], "500 -> 500 mm/s")

    def test_names_and_meaning_follow_the_real_address_not_the_position(self):
        """4번부터 읽은 응답의 첫 값은 '관절1'이 아니라 4번 레지스터(상자 감지)다."""
        fields = annotate_response(make_response(1, [1, 12, 664]), start_address=4)
        data = [(f["name"], f["value"]) for f in fields if f["group"] == "data"]
        self.assertEqual(data, [("레지스터 4: 상자 감지", "1 -> 감지"),
                                ("레지스터 5: 생산 카운트", "12 -> 12"),
                                ("레지스터 6: 온도", "664 -> 66.4℃")])

    def test_addresses_without_a_label(self):
        fields = annotate_response(make_response(1, [5, 6]), start_address=20)
        names = [f["name"] for f in fields if f["group"] == "data"]
        self.assertEqual(names, ["레지스터 20: 값", "레지스터 21: 값"])      # 우리 맵에 없는 주소

    def test_byte_count_field_explains_register_count(self):
        count_field = [f for f in self.fields if f["name"] == "바이트 수"][0]
        self.assertEqual(count_field["hex"], "14")
        self.assertIn("10", count_field["value"])


class AnnotateBrokenResponseTest(unittest.TestCase):
    def errors(self, fields):
        return [f for f in fields if f["group"] == "error"]

    def test_exception_response(self):
        data = build_response(42, 1, bytes([0x83, 0x02]))
        fields = annotate_response(data)
        self.assertEqual(covered_hex(fields), hex_bytes(data))
        errors = self.errors(fields)
        self.assertEqual([f["name"] for f in errors], ["기능 코드", "예외 코드"])
        self.assertIn("예외 응답", errors[0]["value"])
        self.assertIn("잘못된 주소", errors[1]["value"])

    def test_known_exception_codes_are_named(self):
        for code, name in {1: "지원하지 않는 기능", 3: "잘못된 값", 4: "설비 오류", 9: "알 수 없음"}.items():
            with self.subTest(code):
                fields = annotate_response(build_response(1, 1, bytes([0x83, code])))
                self.assertIn(name, self.errors(fields)[-1]["value"])

    def test_wrong_protocol_id_is_flagged(self):
        data = struct.pack(">HHHB", 5, 0x1234, 3, 1) + b"\x00\x00"        # 시뮬레이터의 깨진 응답
        fields = annotate_response(data)
        protocol = [f for f in fields if f["name"] == "프로토콜 ID"][0]
        self.assertIn("비정상", protocol["value"])
        self.assertEqual(covered_hex(fields)[:20], hex_bytes(data)[:20])

    def test_truncated_responses_never_crash(self):
        full = make_response(42, REGS)
        for n in range(0, len(full) + 1):
            with self.subTest(bytes=n):
                fields = annotate_response(full[:n])
                json.dumps(fields)                                         # JSON으로 바꿀 수 있어야 한다
                self.assertTrue(fields)
                if n < len(full):
                    self.assertTrue(self.errors(fields), f"{n}바이트에서 오류 표시가 없습니다")
        # 같은 데이터를 앞에서부터 읽어 설명한 바이트가 실제 바이트와 어긋나지 않는다
        for n in (7, 8, 9, 15):
            fields = annotate_response(full[:n])
            explained = covered_hex([f for f in fields if f["group"] != "error"])
            self.assertTrue(hex_bytes(full[:n]).startswith(explained))

    def test_short_inputs_have_specific_messages(self):
        self.assertIn("헤더(7바이트)보다 짧", annotate_response(b"\x00\x01")[0]["value"])
        header_only = make_response(1, [])[:7]
        self.assertIn("내용이 없습니다", annotate_response(header_only)[-1]["value"])
        only_function = header_only + b"\x03"
        self.assertIn("여기서 끝", annotate_response(only_function)[-1]["value"])

    def test_byte_count_mismatch_and_odd_bytes(self):
        data = make_response(1, [452, 300])
        short = data[:-1]                                                   # 데이터 한 바이트가 모자람
        fields = annotate_response(short)
        names = [f["name"] for f in self.errors(fields)]
        self.assertIn("남는 바이트", names)
        self.assertIn("바이트 수 불일치", names)

    def test_unexpected_function_code(self):
        fields = annotate_response(build_response(1, 1, bytes([0x10, 0x00, 0x01])))
        errors = self.errors(fields)
        self.assertIn("예상하지 못한 기능", errors[0]["value"])
        self.assertEqual(errors[1]["name"], "나머지")


class SimulatorResponseTest(unittest.TestCase):
    def test_real_simulator_frames_are_fully_explained(self):
        """시뮬레이터가 실제로 만드는 응답은 모든 바이트가 정확히 한 번씩 설명되고 오류 표시가 없다."""
        sim = rs.RobotSim("T", seed=1)
        for step in range(40):
            sim.step(0.5)
            pdu = handle_pdu(bytes([0x03]) + struct.pack(">HH", 0, 10), sim.registers())
            data = build_response(step + 1, 1, pdu)
            fields = annotate_response(data)
            self.assertEqual(covered_hex(fields), hex_bytes(data))
            self.assertFalse([f for f in fields if f["group"] == "error"])


class ExchangeDictTest(unittest.TestCase):
    def test_none_and_missing_response(self):
        self.assertIsNone(exchange_dict(None))
        ex = Exchange(1.0, 7, build_read_request(7, 1, 0, 10), None, "응답 시간 초과 (1000 ms)")
        d = exchange_dict(ex)
        self.assertIsNone(d["response"])
        self.assertEqual(d["error"], "응답 시간 초과 (1000 ms)")
        self.assertEqual(d["request"]["hex"], "00 07 00 00 00 06 01 03 00 00 00 0a")

    def test_full_exchange_is_json_ready(self):
        ex = Exchange(2.0, 42, build_read_request(42, 1, 4, 2), make_response(42, [10, 20]), None)
        d = exchange_dict(ex)
        json.dumps(d, ensure_ascii=False)
        self.assertEqual(d["tid"], 42)
        names = [f["name"] for f in d["response"]["fields"] if f["group"] == "data"]
        self.assertEqual(names, ["레지스터 4: 상자 감지", "레지스터 5: 생산 카운트"])   # 시작 주소 4가 반영된다


class ModelDescribeTest(unittest.TestCase):
    def test_register_labels_match_register_count_and_simulator(self):
        self.assertEqual(len(models.REGISTER_LABELS), models.REGISTER_COUNT)
        self.assertEqual(models.PHASE_NAMES, rs.PHASE_NAMES)
        self.assertEqual((models.WARN_TEMP, models.FAULT_TEMP), (rs.WARN_TEMP, rs.FAULT_TEMP))

    def test_describe_each_register(self):
        cases = [(0, 900, "90.0°"), (1, 205, "20.5°"), (2, 1, "닫힘"), (2, 0, "열림"), (3, 4, "놓기"),
                 (4, 1, "감지"), (4, 0, "없음"), (5, 12, "12"), (6, 395, "39.5℃"), (7, 2, "이상"),
                 (8, 66, "6.6초"), (9, 500, "500 mm/s")]
        for index, raw, expected in cases:
            with self.subTest(index=index, raw=raw):
                self.assertEqual(models.describe_register(index, raw), expected)

    def test_out_of_range_values_do_not_crash(self):
        self.assertEqual(models.describe_register(3, 9), "알 수 없음(9)")
        self.assertEqual(models.describe_register(7, 9), "알 수 없음(9)")
        self.assertEqual(models.describe_register(42, 5), "5")


# ---- 실제 클라이언트가 바이트를 기록하는지 ---------------------------------------------
class ClientExchangeTest(unittest.IsolatedAsyncioTestCase):
    async def client_for(self, faults=None, timeout=1.0):
        server = SimServer(rs.RobotSim("T", seed=1), "127.0.0.1", 0, faults)
        server.start()
        self.addCleanup(server.stop)
        client = ModbusTcpClient("127.0.0.1", server.port, timeout=timeout)
        await client.connect()
        self.addAsyncCleanup(client.close)
        return client

    async def test_successful_read_is_recorded(self):
        client = await self.client_for()
        self.assertIsNone(client.last_exchange)                              # 아직 아무것도 안 했다
        await client.read_holding_registers(0, 10)
        ex = client.last_exchange
        self.assertIsNone(ex.error)
        self.assertEqual(ex.request, build_read_request(ex.tid, 1, 0, 10))
        self.assertEqual(len(ex.response), 29)
        self.assertEqual(struct.unpack(">H", ex.response[0:2])[0], ex.tid)
        self.assertIsNone(client.last_failed_exchange)
        self.assertAlmostEqual(ex.ts, time.time(), delta=5)

    async def test_transaction_ids_increase(self):
        client = await self.client_for()
        await client.read_holding_registers(0, 1)
        first = client.last_exchange.tid
        await client.read_holding_registers(0, 1)
        self.assertEqual(client.last_exchange.tid, first + 1)

    async def test_exception_response_is_recorded_with_bytes(self):
        client = await self.client_for()
        with self.assertRaises(ModbusError):
            await client.read_holding_registers(5, 10)                       # 범위 밖
        ex = client.last_failed_exchange
        self.assertIn("예외 응답", ex.error)
        self.assertEqual(len(ex.response), 9)                                # 헤더 7 + 예외 2
        self.assertTrue(client.connected)                                    # 연결은 그대로

    async def test_garbage_response_keeps_the_bad_header(self):
        client = await self.client_for(Faults(garbage_every=1))
        with self.assertRaises(ModbusError):
            await client.read_holding_registers(0, 10)
        ex = client.last_failed_exchange
        self.assertIn("프로토콜 ID", ex.error)
        self.assertEqual(len(ex.response), 7)                                # 규격 검사에서 걸린 헤더
        protocol = [f for f in annotate_response(ex.response) if f["name"] == "프로토콜 ID"][0]
        self.assertIn("비정상", protocol["value"])

    async def test_timeout_has_no_response_bytes(self):
        client = await self.client_for(Faults(delay_ms=600), timeout=0.2)
        with self.assertRaises(ModbusError):
            await client.read_holding_registers(0, 10)
        ex = client.last_failed_exchange
        self.assertIsNone(ex.response)
        self.assertIn("시간 초과", ex.error)
        self.assertEqual(ex.request, build_read_request(ex.tid, 1, 0, 10))   # 보낸 요청은 남는다

    async def test_dropped_connection_is_recorded(self):
        client = await self.client_for(Faults(drop_every=1))
        with self.assertRaises(ModbusError):
            await client.read_holding_registers(0, 10)
        ex = client.last_failed_exchange
        self.assertIsNone(ex.response)
        self.assertIn("끊겼", ex.error)

    async def test_last_failed_survives_later_success(self):
        client = await self.client_for(Faults(garbage_every=2))
        await client.read_holding_registers(0, 10)                           # 1번째: 정상
        with self.assertRaises(ModbusError):
            await client.read_holding_registers(0, 10)                       # 2번째: 깨짐
        failed_tid = client.last_failed_exchange.tid
        await client.connect()                                               # 깨진 뒤에는 연결을 닫으므로 다시 접속
        await client.read_holding_registers(0, 10)                           # 3번째: 정상
        self.assertIsNone(client.last_exchange.error)
        self.assertEqual(client.last_failed_exchange.tid, failed_tid)       # 실패 기록은 그대로 남아 있다

    async def test_worker_exposes_exchanges(self):
        server = SimServer(rs.RobotSim("T", seed=1), "127.0.0.1", 0)
        server.start()
        self.addCleanup(server.stop)
        worker = DeviceWorker("T", "127.0.0.1", server.port, interval=0.05, timeout=0.3)
        task = asyncio.create_task(worker.run())

        async def cleanup():
            worker.stop()
            await task
        self.addAsyncCleanup(cleanup)
        for _ in range(100):
            if worker.last_exchange is not None:
                break
            await asyncio.sleep(0.02)
        self.assertIsNotNone(worker.last_exchange)
        self.assertIsNone(worker.last_failed_exchange)
        json.dumps(exchange_dict(worker.last_exchange))


if __name__ == "__main__":
    unittest.main()
