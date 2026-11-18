"""시연 실행기(tools/demo.py)의 명령 해석과 명령줄 만들기 시험.
실제 프로세스는 띄우지 않는다 (사용자가 쓰는 5021~5026, 8000 포트와 겹치지 않도록)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import demo  # noqa: E402


class ParseCommandTest(unittest.TestCase):
    def test_valid_commands(self):
        self.assertEqual(demo.parse_command("k 4", 6), ("kill", 3))
        self.assertEqual(demo.parse_command("r 1", 6), ("normal", 0))
        self.assertEqual(demo.parse_command("f 6", 6), ("fault", 5))
        self.assertEqual(demo.parse_command("d 2", 6), ("slow", 1))
        self.assertEqual(demo.parse_command("s", 6), ("status", None))
        self.assertEqual(demo.parse_command("h", 6), ("help", None))
        self.assertEqual(demo.parse_command("?", 6), ("help", None))
        self.assertEqual(demo.parse_command("q", 6), ("quit", None))

    def test_case_and_spacing_are_forgiven(self):
        self.assertEqual(demo.parse_command("  K   3  ", 6), ("kill", 2))
        self.assertEqual(demo.parse_command("F\t5", 6), ("fault", 4))
        self.assertEqual(demo.parse_command("Q", 6), ("quit", None))

    def test_blank_line_does_nothing(self):
        self.assertEqual(demo.parse_command("", 6), ("noop", None))
        self.assertEqual(demo.parse_command("   ", 6), ("noop", None))

    def test_bad_input_returns_a_message_not_an_exception(self):
        for line in ("k", "k 0", "k 7", "k -1", "k abc", "k 1 2", "x 1", "kill 1", "k 1.5", "k ٣", "s 1", "q now"):
            with self.subTest(line):
                result = demo.parse_command(line, 6)
                self.assertIsInstance(result, str, line)
                self.assertTrue(result)

    def test_robot_count_is_respected(self):
        self.assertEqual(demo.parse_command("k 3", 3), ("kill", 2))
        self.assertIsInstance(demo.parse_command("k 4", 3), str)


class CommandLineTest(unittest.TestCase):
    def test_robot_command_per_mode(self):
        normal = demo.robot_command("ARM-04", 5024, 2, "normal")
        self.assertEqual(normal[normal.index("--name") + 1], "ARM-04")
        self.assertEqual(normal[normal.index("--port") + 1], "5024")
        self.assertEqual(normal[normal.index("--seed") + 1], "2")
        self.assertEqual(normal[normal.index("--heat-rate") + 1], "0.3")
        self.assertNotIn("--delay-ms", normal)
        fault = demo.robot_command("ARM-05", 5025, 7, "fault")
        self.assertGreater(float(fault[fault.index("--heat-rate") + 1]), 3.0)       # 빨리 과열
        slow = demo.robot_command("ARM-02", 5022, 5, "slow")
        self.assertGreater(int(slow[slow.index("--delay-ms") + 1]), 1000)           # 수집기 제한 시간(1초)보다 길다

    def test_every_mode_has_options_and_a_label(self):
        for mode in demo.MODES:
            self.assertIn(mode, demo.MODE_LABELS)
        self.assertIn("off", demo.MODE_LABELS)

    def test_robots_match_the_default_collector_devices(self):
        """수집기 기본 설비 목록(ARM-01~06, 포트 5021~5026)과 시연 로봇이 같아야 한다."""
        from collector.__main__ import DEFAULT_DEVICES, parse_devices
        self.assertEqual([(n, p) for n, _, p in parse_devices(DEFAULT_DEVICES)], [(n, p) for n, p, _ in demo.ROBOTS])

    def test_collector_command_uses_demo_database_not_the_real_one(self):
        cmd = demo.collector_command(demo.DB_PATH, demo.WEB_PORT)
        self.assertEqual(Path(cmd[cmd.index("--db") + 1]).name, "demo.db")
        self.assertNotIn("factory.db", " ".join(cmd))
        self.assertEqual(cmd[cmd.index("--web-port") + 1], "8000")


class PortOffsetTest(unittest.TestCase):
    def test_shifted_robots_move_only_the_ports(self):
        shifted = demo.shifted_robots(1000)
        self.assertEqual([(n, s) for n, _, s in shifted], [(n, s) for n, _, s in demo.ROBOTS])      # 이름과 시드는 그대로
        self.assertEqual([p for _, p, _ in shifted], [p + 1000 for _, p, _ in demo.ROBOTS])
        self.assertEqual(demo.shifted_robots(0), list(demo.ROBOTS))

    def test_collector_gets_the_shifted_device_list(self):
        robots = demo.shifted_robots(1000)
        cmd = demo.collector_command(demo.DB_PATH, 8100, robots)
        devices = cmd[cmd.index("--devices") + 1].split(",")
        self.assertEqual(devices[0], "ARM-01:127.0.0.1:6021")
        self.assertEqual(devices[-1], "ARM-06:127.0.0.1:6026")
        self.assertEqual(cmd[cmd.index("--web-port") + 1], "8100")

    def test_default_collector_command_has_no_device_override(self):
        self.assertNotIn("--devices", demo.collector_command(demo.DB_PATH, 8000))

    def test_demo_object_uses_the_given_robots_and_web_port(self):
        robots = demo.shifted_robots(1000)
        d = demo.Demo(robots, 8100)
        self.assertEqual(d.robots, robots)
        self.assertIn("6021", d.status_lines()[0])


class DemoStateTest(unittest.TestCase):
    def test_status_before_start_shows_everything_off(self):
        d = demo.Demo()
        lines = d.status_lines()
        self.assertEqual(len(lines), 7)                                   # 로봇 6 + 수집 서버
        self.assertTrue(all("꺼짐" in line for line in lines[:6] + lines[6:]))

    def test_stopping_a_robot_that_is_not_running_is_harmless(self):
        d = demo.Demo()
        d.stop_robot(2)
        self.assertEqual(d.modes["ARM-03"], "off")


if __name__ == "__main__":
    unittest.main()
