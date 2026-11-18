"""시험 도구(tools/)의 계산과 동작 시험.

통계 계산과 WebSocket 프레임 해석은 순수 함수라서 값을 직접 넣어 확인하고,
부하 시험 도구는 아주 작은 규모(설비 3대, 몇 초)로 실제로 돌려서 '도구 자체가 동작하는지'를 확인한다.
(큰 규모의 측정은 시간이 오래 걸려서 시험에 넣지 않고 docs/COMM_TEST.md에 결과를 적었다.)
"""
import json
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from measure import ProcessMeter, free_port, gap_stats, parse_frames, percentile  # noqa: E402


class StatsTest(unittest.TestCase):
    def test_percentile_basic_values(self):
        values = [1, 2, 3, 4, 5]
        self.assertEqual(percentile(values, 0), 1)
        self.assertEqual(percentile(values, 50), 3)
        self.assertEqual(percentile(values, 100), 5)
        self.assertAlmostEqual(percentile(values, 25), 2)
        self.assertAlmostEqual(percentile(values, 90), 4.6)

    def test_percentile_edge_cases(self):
        self.assertIsNone(percentile([], 50))
        self.assertEqual(percentile([7], 99), 7)
        self.assertEqual(percentile([5, 1, 3], 50), 3)             # 정렬되지 않은 입력
        data = [3.0, 1.0, 2.0]
        percentile(data, 50)
        self.assertEqual(data, [3.0, 1.0, 2.0])                    # 원래 목록을 고치지 않는다

    def test_gap_stats(self):
        stats = gap_stats([0.0, 0.5, 1.0, 1.5, 3.5])               # 간격 0.5, 0.5, 0.5, 2.0
        self.assertEqual(stats["count"], 4)
        self.assertAlmostEqual(stats["mean"], 0.875)
        self.assertAlmostEqual(stats["p50"], 0.5)
        self.assertAlmostEqual(stats["max"], 2.0)

    def test_gap_stats_unsorted_and_too_short(self):
        self.assertAlmostEqual(gap_stats([1.5, 0.0, 1.0, 0.5])["max"], 0.5)   # 정렬해서 계산
        self.assertIsNone(gap_stats([]))
        self.assertIsNone(gap_stats([1.0]))


def frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """서버가 보내는 형태(마스크 없음)의 WebSocket 프레임을 만든다."""
    n = len(payload)
    if n < 126:
        return bytes([0x80 | opcode, n]) + payload
    if n < 65536:
        return bytes([0x80 | opcode, 126]) + struct.pack(">H", n) + payload
    return bytes([0x80 | opcode, 127]) + struct.pack(">Q", n) + payload


class FrameParseTest(unittest.TestCase):
    def test_parses_short_medium_and_long_frames(self):
        payloads = [b"a" * 5, b"b" * 300, b"c" * 70000]
        frames, rest = parse_frames(b"".join(frame(p) for p in payloads))
        self.assertEqual([f[1] for f in frames], payloads)
        self.assertEqual(rest, b"")

    def test_incomplete_data_is_kept_for_next_time(self):
        data = frame(b"hello") + frame(b"x" * 300)
        for cut in (1, 3, len(frame(b"hello")) + 1, len(frame(b"hello")) + 3, len(data) - 1):
            frames, rest = parse_frames(data[:cut])
            self.assertLessEqual(len(frames), 1)
            more, rest2 = parse_frames(rest + data[cut:])
            self.assertEqual(len(frames) + len(more), 2, f"cut={cut}")
            self.assertEqual(rest2, b"")

    def test_reports_opcode(self):
        frames, _ = parse_frames(frame(b"", 0x9) + frame(b"{}", 0x1))
        self.assertEqual([f[0] for f in frames], [0x9, 0x1])


class SmallHelpersTest(unittest.TestCase):
    def test_free_port_is_usable(self):
        import socket
        port = free_port()
        self.assertGreater(port, 1023)
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))                            # 받은 포트에 실제로 붙을 수 있다


@unittest.skipUnless(sys.platform == "win32", "CPU·메모리 측정은 윈도우 전용입니다")
class ProcessMeterTest(unittest.TestCase):
    def test_reads_cpu_and_memory_of_a_child_process(self):
        import subprocess
        proc = subprocess.Popen([sys.executable, "-c", "x = sum(i * i for i in range(2_000_000)); import time; time.sleep(3)"])
        try:
            meter = ProcessMeter(proc)
            self.assertTrue(meter.supported)
            import time
            time.sleep(1.5)
            cpu, mem = meter.cpu_seconds(), meter.memory_mb()
            self.assertIsNotNone(cpu)
            self.assertGreater(cpu, 0.05)                          # 계산을 했으니 CPU 시간이 0보다 크다
            self.assertGreater(mem, 5)                             # 파이썬 프로세스는 최소 몇 MB를 쓴다
            self.assertLess(mem, 2000)
        finally:
            proc.kill()
            proc.wait()


class LoadtestSmokeTest(unittest.TestCase):
    """부하 시험 도구를 설비 3대, 몇 초 규모로 돌려 본다 (큰 측정은 docs/COMM_TEST.md)."""

    def test_small_run_collects_samples_without_failures(self):
        from loadtest import run_load
        r = run_load(devices=3, duration=4, warmup=2, interval=0.5, web=True)
        self.assertEqual(r["devices"], 3)
        self.assertGreater(r["samples"], 3 * 4 / 0.5 * 0.7)        # 이상적인 개수(24)의 70% 이상
        self.assertLessEqual(r["samples"], 3 * 4 / 0.5 + 6)        # 이상적인 개수를 크게 넘지 않는다
        self.assertEqual(r["events"].get("read_failed", 0), 0)
        self.assertEqual(r["events"].get("connect_failed", 0), 0)
        self.assertLess(r["gap_max"], 1.5)
        self.assertIsNone(r["ws"]["error"])
        self.assertGreater(r["ws"]["by_type"].get("snapshot", 0), 10)
        self.assertGreaterEqual(r["ws"]["by_type"].get("state", 0), 2)
        json.dumps({k: v for k, v in r.items()})                   # 결과가 JSON으로 저장 가능한 값이다


if __name__ == "__main__":
    unittest.main()
