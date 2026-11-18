"""가상 설비(시뮬레이터) 시험.

    python -m unittest discover -s tests -v
"""
import socket
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "simulator"))

import robot_sim as rs  # noqa: E402
from modbus_sim import Faults, SimServer  # noqa: E402


def run(sim, seconds, dt=0.1, on_step=None):
    for _ in range(int(round(seconds / dt))):
        sim.step(dt)
        if on_step:
            on_step(sim)


class RobotSimTest(unittest.TestCase):
    def test_registers_shape_and_initial_values(self):
        sim = rs.RobotSim("T", seed=1)
        regs = sim.registers()
        self.assertEqual(len(regs), rs.REGISTER_COUNT)
        self.assertTrue(all(0 <= v <= 0xFFFF for v in regs))
        self.assertEqual(regs[rs.REG_JOINT1], 900)    # 90.0도
        self.assertEqual(regs[rs.REG_JOINT2], 200)    # 20.0도
        self.assertEqual(regs[rs.REG_TEMP], 400)      # 40.0도
        self.assertEqual(regs[rs.REG_STATUS], rs.STATUS_NORMAL)

    def test_cycle_phases_run_in_order(self):
        sim = rs.RobotSim("T", seed=1, box_interval=(0.5, 0.5))
        seen = []

        def record(s):
            if not seen or seen[-1] != s.phase:
                seen.append(s.phase)

        run(sim, 20, on_step=record)
        self.assertEqual(
            seen[:7],
            [rs.PHASE_IDLE, rs.PHASE_APPROACH, rs.PHASE_GRAB, rs.PHASE_MOVE,
             rs.PHASE_RELEASE, rs.PHASE_RETURN, rs.PHASE_IDLE],
        )

    def test_production_count_and_cycle_time(self):
        sim = rs.RobotSim("T", seed=1, box_interval=(0.5, 0.5))
        run(sim, 20)
        self.assertGreaterEqual(sim.count, 2)
        # 접근 1.5 + 집기 0.8 + 이동 2.0 + 놓기 0.8 + 복귀 1.5 = 6.6초
        self.assertAlmostEqual(sim.last_cycle, 6.6, delta=0.3)

    def test_gripper_follows_phase(self):
        sim = rs.RobotSim("T", seed=1, box_interval=(0.5, 0.5))

        def check(s):
            if s.phase in (rs.PHASE_GRAB, rs.PHASE_MOVE):
                self.assertEqual(s.gripper, 1, f"단계 {s.phase}에서 집게가 닫혀 있어야 한다")
            if s.phase in (rs.PHASE_APPROACH, rs.PHASE_RELEASE, rs.PHASE_RETURN):
                self.assertEqual(s.gripper, 0, f"단계 {s.phase}에서 집게가 열려 있어야 한다")

        run(sim, 20, on_step=check)

    def test_joint_angles_stay_in_range(self):
        sim = rs.RobotSim("T", seed=1, box_interval=(0.5, 0.5))

        def check(s):
            self.assertTrue(50.0 - 1e-6 <= s.j1 <= 130.0 + 1e-6, s.j1)
            self.assertTrue(20.0 - 1e-6 <= s.j2 <= 70.0 + 1e-6, s.j2)

        run(sim, 30, on_step=check)

    def test_box_sensor_pulses(self):
        sim = rs.RobotSim("T", seed=1, box_interval=(0.5, 0.5))
        edges = 0
        prev = 0
        for _ in range(200):
            sim.step(0.1)
            cur = sim.registers()[rs.REG_BOX_SENSOR]
            if cur == 1 and prev == 0:
                edges += 1
            prev = cur
        self.assertGreaterEqual(edges, 2)

    def test_normal_operation_stays_normal(self):
        sim = rs.RobotSim("T", seed=1)
        run(sim, 60)
        self.assertEqual(sim.status, rs.STATUS_NORMAL)
        self.assertLess(sim.temp, rs.WARN_TEMP)

    def test_overheat_stops_robot_then_recovers(self):
        sim = rs.RobotSim("T", seed=1, heat_rate=5.0, cool_rate=5.0, box_interval=(0.2, 0.2))
        # 이상 상태가 될 때까지 돌린다
        for _ in range(400):
            sim.step(0.1)
            if sim.status == rs.STATUS_FAULT:
                break
        self.assertEqual(sim.status, rs.STATUS_FAULT)
        self.assertEqual(sim.registers()[rs.REG_CONVEYOR], 0)

        # 이상인 동안에는 멈춰 있고 생산 카운트가 늘지 않는다
        count_at_fault = sim.count
        for _ in range(400):
            sim.step(0.1)
            if sim.status != rs.STATUS_FAULT:
                break
            self.assertEqual(sim.phase, rs.PHASE_IDLE)
            self.assertEqual(sim.count, count_at_fault)
        # 식으면 복구한다
        self.assertNotEqual(sim.status, rs.STATUS_FAULT)

    def test_warning_between_thresholds(self):
        sim = rs.RobotSim("T", seed=1)
        sim.temp = 66.0
        sim.step(0.1)
        self.assertEqual(sim.status, rs.STATUS_WARNING)


# ---- Modbus TCP 통신 ---------------------------------------------------------
def recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


def request(sock, tid, pdu):
    sock.sendall(struct.pack(">HHHB", tid, 0, len(pdu) + 1, 1) + pdu)


def read_response(sock):
    header = recv_exact(sock, 7)
    tid, proto, length, unit = struct.unpack(">HHHB", header)
    return tid, proto, recv_exact(sock, length - 1)


class ModbusServerTest(unittest.TestCase):
    def start(self, faults=None):
        sim = rs.RobotSim("T", seed=1)
        self.server = SimServer(sim, "127.0.0.1", 0, faults)
        self.server.start()
        self.sock = socket.create_connection(("127.0.0.1", self.server.port), timeout=3)
        self.addCleanup(self.stop)

    def stop(self):
        self.sock.close()
        self.server.stop()

    def test_read_all_registers(self):
        self.start()
        request(self.sock, 42, struct.pack(">BHH", 3, 0, rs.REGISTER_COUNT))
        tid, proto, pdu = read_response(self.sock)
        self.assertEqual(tid, 42)                       # 트랜잭션 번호를 그대로 돌려준다
        self.assertEqual(proto, 0)
        self.assertEqual(pdu[0], 0x03)
        self.assertEqual(pdu[1], rs.REGISTER_COUNT * 2)  # 바이트 수
        values = struct.unpack(">" + "H" * rs.REGISTER_COUNT, pdu[2:])
        self.assertEqual(values[rs.REG_JOINT1], 900)
        self.assertEqual(values[rs.REG_TEMP], 400)

    def test_out_of_range_address_returns_exception_02(self):
        self.start()
        request(self.sock, 1, struct.pack(">BHH", 3, 5, 10))
        _, _, pdu = read_response(self.sock)
        self.assertEqual(pdu, bytes([0x83, 0x02]))

    def test_port_already_in_use_is_refused(self):
        self.start()
        other = SimServer(rs.RobotSim("T2", seed=2), "127.0.0.1", self.server.port)
        with self.assertRaises(OSError):                 # 같은 포트에 두 번째 로봇이 붙으면 안 된다
            other.start()
        # 기존 서버는 영향받지 않고 계속 응답한다
        request(self.sock, 7, struct.pack(">BHH", 3, 0, 1))
        tid, _, pdu = read_response(self.sock)
        self.assertEqual((tid, pdu[0]), (7, 0x03))

    def test_unsupported_function_returns_exception_01(self):
        self.start()
        request(self.sock, 1, struct.pack(">BHH", 0x06, 0, 1))
        _, _, pdu = read_response(self.sock)
        self.assertEqual(pdu, bytes([0x86, 0x01]))

    def test_garbage_fault_injection(self):
        self.start(Faults(garbage_every=2))
        request(self.sock, 1, struct.pack(">BHH", 3, 0, 1))
        read_response(self.sock)                         # 1번째는 정상
        request(self.sock, 2, struct.pack(">BHH", 3, 0, 1))
        # 2번째는 깨짐: 헤더 7바이트 + 2바이트, 프로토콜 ID가 0x1234
        broken = recv_exact(self.sock, 9)
        tid, proto, length, unit = struct.unpack(">HHHB", broken[:7])
        self.assertEqual((tid, proto), (2, 0x1234))

    def test_drop_fault_injection_closes_connection(self):
        self.start(Faults(drop_every=2))
        request(self.sock, 1, struct.pack(">BHH", 3, 0, 1))
        read_response(self.sock)
        request(self.sock, 2, struct.pack(">BHH", 3, 0, 1))
        with self.assertRaises((ConnectionError, OSError)):
            recv_exact(self.sock, 7)


if __name__ == "__main__":
    unittest.main()
