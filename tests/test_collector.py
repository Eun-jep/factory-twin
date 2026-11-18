"""수집 서버 시험.

    python -m unittest discover -s tests -v
"""
import asyncio
import socket
import struct
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "simulator"))

import robot_sim as rs  # noqa: E402
from collector import models  # noqa: E402
from collector.device import (  # noqa: E402
    ConnState, DeviceWorker, next_backoff,
    EV_CONNECT_FAILED, EV_DISCONNECTED, EV_READ_FAILED, EV_RECOVERED,
)
from collector.modbus import (  # noqa: E402
    ModbusConnectionError, ModbusExceptionResponse, ModbusProtocolError, ModbusTcpClient,
    ModbusTimeout, build_read_request, parse_header, parse_read_response,
)
from modbus_sim import Faults, SimServer  # noqa: E402


# ---- 프레임 만들기·검사 (소켓 없음) ----------------------------------------------
class FrameTest(unittest.TestCase):
    def test_build_read_request_bytes(self):
        self.assertEqual(build_read_request(1, 1, 0, 6), bytes.fromhex("000100000006010300000006"))
        self.assertEqual(build_read_request(0x1234, 7, 10, 2), bytes.fromhex("12340000000607030 00a0002".replace(" ", "")))

    def test_transaction_id_wraps(self):
        self.assertEqual(build_read_request(0x10000, 1, 0, 1)[:2], b"\x00\x00")

    def test_parse_header_ok(self):
        header = struct.pack(">HHHB", 5, 0, 15, 1)
        self.assertEqual(parse_header(header, 5, 1), 14)

    def test_parse_header_rejects_bad_frames(self):
        cases = {
            "protocol": struct.pack(">HHHB", 5, 0x1234, 15, 1),
            "length too small": struct.pack(">HHHB", 5, 0, 1, 1),
            "length too large": struct.pack(">HHHB", 5, 0, 254, 1),
            "transaction id": struct.pack(">HHHB", 6, 0, 15, 1),
            "unit id": struct.pack(">HHHB", 5, 0, 15, 2),
        }
        for label, header in cases.items():
            with self.subTest(label), self.assertRaises(ModbusProtocolError):
                parse_header(header, 5, 1)

    def test_parse_read_response_ok(self):
        pdu = bytes([0x03, 4]) + struct.pack(">HH", 452, 300)
        self.assertEqual(parse_read_response(pdu, 2), [452, 300])

    def test_parse_read_response_errors(self):
        with self.assertRaises(ModbusProtocolError):                 # 너무 짧음
            parse_read_response(b"\x03", 1)
        with self.assertRaises(ModbusProtocolError):                 # 바이트 수 불일치
            parse_read_response(bytes([0x03, 2, 0, 1]), 2)
        with self.assertRaises(ModbusProtocolError):                 # 길이와 바이트 수 필드가 다름
            parse_read_response(bytes([0x03, 4, 0, 1]), 2)
        with self.assertRaises(ModbusProtocolError):                 # 다른 기능 코드
            parse_read_response(bytes([0x04, 2, 0, 1]), 1)
        with self.assertRaises(ModbusExceptionResponse) as ctx:      # 예외 응답
            parse_read_response(bytes([0x83, 0x02]), 1)
        self.assertEqual(ctx.exception.code, 2)


class ModelTest(unittest.TestCase):
    def test_register_map_matches_simulator(self):
        pairs = [
            (models.REG_JOINT1, rs.REG_JOINT1), (models.REG_JOINT2, rs.REG_JOINT2),
            (models.REG_GRIPPER, rs.REG_GRIPPER), (models.REG_PHASE, rs.REG_PHASE),
            (models.REG_BOX_SENSOR, rs.REG_BOX_SENSOR), (models.REG_COUNT, rs.REG_COUNT),
            (models.REG_TEMP, rs.REG_TEMP), (models.REG_STATUS, rs.REG_STATUS),
            (models.REG_CYCLE, rs.REG_CYCLE), (models.REG_CONVEYOR, rs.REG_CONVEYOR),
            (models.REGISTER_COUNT, rs.REGISTER_COUNT),
        ]
        for mine, theirs in pairs:
            self.assertEqual(mine, theirs)
        self.assertEqual(models.PHASE_NAMES.__len__(), len(rs.PHASE_NAMES))

    def test_parse_simulator_registers(self):
        sim = rs.RobotSim("T", seed=1)
        snap = models.parse_registers(sim.registers(), 123.0)
        self.assertEqual(snap.joint1_deg, 90.0)
        self.assertEqual(snap.joint2_deg, 20.0)
        self.assertEqual(snap.temp_c, 40.0)
        self.assertEqual(snap.phase_name, "대기")
        self.assertEqual(snap.status_name, "정상")
        self.assertFalse(snap.gripper_closed)

    def test_parse_registers_rejects_bad_values(self):
        good = rs.RobotSim("T", seed=1).registers()
        for label, index, value in [("phase", rs.REG_PHASE, 9), ("status", rs.REG_STATUS, 7),
                                    ("gripper", rs.REG_GRIPPER, 2), ("sensor", rs.REG_BOX_SENSOR, 5)]:
            bad = list(good)
            bad[index] = value
            with self.subTest(label), self.assertRaises(ValueError):
                models.parse_registers(bad, 0.0)
        with self.assertRaises(ValueError):
            models.parse_registers(good[:-1], 0.0)


class BackoffTest(unittest.TestCase):
    def test_backoff_doubles_up_to_maximum(self):
        value, seen = 1.0, []
        for _ in range(6):
            seen.append(value)
            value = next_backoff(value, 8.0)
        self.assertEqual(seen, [1.0, 2.0, 4.0, 8.0, 8.0, 8.0])


# ---- 실제 시뮬레이터와 통신 ------------------------------------------------------
class SimTestCase(unittest.IsolatedAsyncioTestCase):
    def make_server(self, faults=None, port=0, seed=1):
        server = SimServer(rs.RobotSim("T", seed=seed), "127.0.0.1", port, faults)
        server.start()
        self.addCleanup(server.stop)
        return server

    async def wait_until(self, condition, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                return
            await asyncio.sleep(0.02)
        self.fail("조건이 시간 안에 충족되지 않았습니다.")

    def make_worker(self, port, name="T", **kwargs):
        kwargs.setdefault("interval", 0.05)
        kwargs.setdefault("timeout", 0.3)
        kwargs.setdefault("backoff_initial", 0.05)
        kwargs.setdefault("backoff_max", 0.2)
        return DeviceWorker(name, "127.0.0.1", port, **kwargs)

    async def run_worker(self, worker):
        task = asyncio.create_task(worker.run())

        async def cleanup():
            worker.stop()
            await task
        self.addAsyncCleanup(cleanup)
        return task


class ClientTest(SimTestCase):
    async def test_reads_all_registers(self):
        server = self.make_server()
        client = ModbusTcpClient("127.0.0.1", server.port, timeout=1.0)
        await client.connect()
        self.addAsyncCleanup(client.close)
        regs = await client.read_holding_registers(0, rs.REGISTER_COUNT)
        self.assertEqual(len(regs), rs.REGISTER_COUNT)
        self.assertEqual(regs[rs.REG_JOINT1], 900)

    async def test_exception_response_keeps_connection(self):
        server = self.make_server()
        client = ModbusTcpClient("127.0.0.1", server.port, timeout=1.0)
        await client.connect()
        self.addAsyncCleanup(client.close)
        with self.assertRaises(ModbusExceptionResponse) as ctx:
            await client.read_holding_registers(5, 10)           # 범위 밖
        self.assertEqual(ctx.exception.code, 2)
        self.assertTrue(client.connected)                        # 연결은 그대로
        self.assertEqual(len(await client.read_holding_registers(0, 3)), 3)

    async def test_timeout_closes_connection(self):
        server = self.make_server(Faults(delay_ms=600))
        client = ModbusTcpClient("127.0.0.1", server.port, timeout=0.2)
        await client.connect()
        with self.assertRaises(ModbusTimeout):
            await client.read_holding_registers(0, 1)
        self.assertFalse(client.connected)

    async def test_garbage_response_is_protocol_error(self):
        server = self.make_server(Faults(garbage_every=1))
        client = ModbusTcpClient("127.0.0.1", server.port, timeout=0.5)
        await client.connect()
        with self.assertRaises(ModbusProtocolError):
            await client.read_holding_registers(0, 1)
        self.assertFalse(client.connected)

    async def test_dropped_connection_is_connection_error(self):
        server = self.make_server(Faults(drop_every=1))
        client = ModbusTcpClient("127.0.0.1", server.port, timeout=0.5)
        await client.connect()
        with self.assertRaises(ModbusConnectionError):
            await client.read_holding_registers(0, 1)
        self.assertFalse(client.connected)

    async def test_connect_refused_is_connection_error(self):
        server = self.make_server()
        port = server.port
        server.stop()
        client = ModbusTcpClient("127.0.0.1", port, timeout=0.5)
        # 닫힌 포트에 접속할 때 Linux는 바로 거부(ConnectionError)하지만, Windows는 재시도하느라
        # 제한 시간 안에는 시간 초과로 나타날 수 있다. 수집 서버는 둘을 같게 처리한다.
        with self.assertRaises((ModbusConnectionError, ModbusTimeout)):
            await client.connect()
        self.assertFalse(client.connected)


class WorkerTest(SimTestCase):
    async def test_collects_snapshots(self):
        server = self.make_server()
        got = []
        worker = self.make_worker(server.port, on_snapshot=lambda name, snap: got.append(snap))
        await self.run_worker(worker)
        await self.wait_until(lambda: worker.stats.reads_ok >= 5)
        self.assertEqual(worker.stats.state, ConnState.ONLINE)
        self.assertEqual(worker.stats.reads_failed, 0)
        self.assertEqual(worker.stats.connects, 1)
        self.assertEqual(got[0].joint1_deg, 90.0)

    async def test_reconnects_after_server_restart(self):
        server = self.make_server()
        port = server.port
        events = []
        worker = self.make_worker(port, on_event=events.append)
        await self.run_worker(worker)
        await self.wait_until(lambda: worker.stats.reads_ok >= 2)

        server.stop()                                            # 설비 전원 꺼짐
        await self.wait_until(lambda: worker.stats.state == ConnState.BACKOFF
                              and worker.stats.consecutive_failures >= 2)
        ok_before = worker.stats.reads_ok

        self.make_server(port=port, seed=2)                      # 설비 전원 켜짐
        await self.wait_until(lambda: worker.stats.reads_ok >= ok_before + 3)
        self.assertEqual(worker.stats.state, ConnState.ONLINE)
        self.assertGreaterEqual(worker.stats.reconnects, 1)
        kinds = [e.kind for e in events]
        # 끊긴 순간이 기록된다: 읽는 도중이면 read_failed, 폴링 사이면 disconnected
        failures = [kinds.index(k) for k in (EV_DISCONNECTED, EV_READ_FAILED) if k in kinds]
        self.assertTrue(failures, f"끊김 이벤트가 없습니다: {kinds}")
        self.assertIn(EV_CONNECT_FAILED, kinds)
        self.assertIn(EV_RECOVERED, kinds)
        self.assertLess(min(failures), kinds.index(EV_RECOVERED))

    async def test_reset_between_polls_is_reported_as_disconnected(self):
        server = self.make_server()
        events = []
        worker = self.make_worker(server.port, interval=0.5, on_event=events.append)
        await self.run_worker(worker)
        await self.wait_until(lambda: worker.stats.reads_ok >= 1)

        # 폴링 사이에 설비가 연결을 강제로 리셋한다(RST). 읽기 중이 아니므로 disconnected로 잡혀야 한다.
        with server._lock:
            conns = list(server._conns)
        for c in conns:
            c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            c.close()
        await self.wait_until(lambda: any(e.kind == EV_DISCONNECTED for e in events))
        self.assertNotIn(EV_READ_FAILED, [e.kind for e in events])
        # 서버는 살아 있으므로 대기 후 다시 접속해 복구한다
        await self.wait_until(lambda: any(e.kind == EV_RECOVERED for e in events), timeout=8)

    async def test_data_age_grows_while_offline(self):
        server = self.make_server()
        worker = self.make_worker(server.port)
        await self.run_worker(worker)
        await self.wait_until(lambda: worker.stats.reads_ok >= 2)
        self.assertLess(worker.stats.data_age(time.time()), 0.5)
        server.stop()
        await self.wait_until(lambda: worker.stats.state != ConnState.ONLINE)
        await asyncio.sleep(0.4)
        self.assertGreaterEqual(worker.stats.data_age(time.time()), 0.3)   # 시간이 흐르면 값이 오래됨

    async def test_data_age_is_none_before_first_read(self):
        worker = DeviceWorker("T", "127.0.0.1", 1)
        self.assertIsNone(worker.stats.data_age(time.time()))

    async def test_recovers_from_garbage_and_drop(self):
        server = self.make_server(Faults(garbage_every=4, drop_every=7))
        worker = self.make_worker(server.port)
        await self.run_worker(worker)
        await self.wait_until(lambda: worker.stats.reads_ok >= 12, timeout=10)
        self.assertGreater(worker.stats.reads_failed, 0)         # 장애가 실제로 있었고
        self.assertGreaterEqual(worker.stats.reconnects, 1)      # 재접속으로 이어 갔다
        self.assertEqual(worker.stats.state, ConnState.ONLINE)

    async def test_survives_timeout(self):
        server = self.make_server(Faults(delay_ms=500))
        worker = self.make_worker(server.port, timeout=0.15)
        await self.run_worker(worker)
        await self.wait_until(lambda: worker.stats.consecutive_failures >= 3, timeout=10)
        self.assertEqual(worker.stats.reads_ok, 0)
        self.assertIn("ModbusTimeout", worker.stats.last_error)

    async def test_stop_returns_promptly_even_while_backing_off(self):
        server = self.make_server()
        port = server.port
        server.stop()
        worker = self.make_worker(port, backoff_initial=30.0, backoff_max=30.0)
        task = asyncio.create_task(worker.run())
        await self.wait_until(lambda: worker.stats.state == ConnState.BACKOFF)
        worker.stop()
        await asyncio.wait_for(task, timeout=1.0)                # 30초를 기다리지 않는다
        self.assertEqual(worker.stats.state, ConnState.STOPPED)


class IsolationTest(SimTestCase):
    async def test_one_device_down_does_not_affect_others(self):
        servers = [self.make_server(seed=s) for s in (1, 5, 15)]
        workers = [self.make_worker(srv.port, name=f"ARM-0{i + 1}")
                   for i, srv in enumerate(servers)]
        for w in workers:
            await self.run_worker(w)
        await self.wait_until(lambda: all(w.stats.reads_ok >= 3 for w in workers))

        servers[1].stop()                                        # ARM-02만 끊는다
        await self.wait_until(lambda: workers[1].stats.consecutive_failures >= 2)
        ok_1, ok_3 = workers[0].stats.reads_ok, workers[2].stats.reads_ok
        ok_2 = workers[1].stats.reads_ok

        await asyncio.sleep(0.6)
        self.assertGreaterEqual(workers[0].stats.reads_ok - ok_1, 5)     # ARM-01은 계속 읽는다
        self.assertGreaterEqual(workers[2].stats.reads_ok - ok_3, 5)     # ARM-03도 계속 읽는다
        self.assertEqual(workers[1].stats.reads_ok, ok_2)                # ARM-02만 멈춘다
        self.assertEqual(workers[0].stats.state, ConnState.ONLINE)
        self.assertEqual(workers[2].stats.state, ConnState.ONLINE)
        self.assertEqual(workers[0].stats.reads_failed, 0)
        self.assertEqual(workers[2].stats.reads_failed, 0)


if __name__ == "__main__":
    unittest.main()
