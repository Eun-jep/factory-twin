"""수집 주기와 재접속 대기 시험 (고정 주기 폴링, 시작 엇갈리기, 재접속 대기 상한).

7단계 실측에서 나온 약점을 고친 것을 확인한다:
- 설비 응답이 느려도 수집 주기가 늘어나지 않는다 (고정 주기)
- 응답이 주기보다 길어도 밀린 만큼 몰아서 읽지 않는다
- 설비가 많아도 같은 순간에 모두 읽지 않는다 (시작 엇갈리기)
- 설비가 다시 켜졌을 때 복구까지 걸리는 시간의 상한이 줄었다
"""
import asyncio
import statistics
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_collector import SimTestCase  # noqa: E402
from collector.device import ConnState, DeviceWorker, next_schedule, stagger_delay  # noqa: E402
from modbus_sim import Faults  # noqa: E402


class ScheduleMathTest(unittest.TestCase):
    def test_next_schedule_keeps_the_fixed_beat(self):
        # 일정 10.0에 읽었고 간격 0.5, 지금 10.2 -> 다음 일정은 10.5, 0.3초 기다린다
        nxt, wait = next_schedule(10.0, 0.5, 10.2)
        self.assertAlmostEqual(nxt, 10.5)
        self.assertAlmostEqual(wait, 0.3)

    def test_next_schedule_does_not_drift_with_slow_reads(self):
        # 읽는 데 0.3초 걸렸어도(지금 10.3) 다음 일정은 여전히 10.5: 응답 시간이 주기에 더해지지 않는다
        nxt, wait = next_schedule(10.0, 0.5, 10.3)
        self.assertAlmostEqual(nxt, 10.5)
        self.assertAlmostEqual(wait, 0.2)

    def test_next_schedule_resets_when_late_instead_of_bursting(self):
        # 일정 10.0, 간격 0.5인데 읽기가 끝난 지금이 11.2 -> 밀린 읽기를 몰아서 하지 않고 지금을 새 기준으로
        nxt, wait = next_schedule(10.0, 0.5, 11.2)
        self.assertEqual((nxt, wait), (11.2, 0.0))

    def test_next_schedule_exactly_on_time(self):
        nxt, wait = next_schedule(10.0, 0.5, 10.5)
        self.assertAlmostEqual(nxt, 10.5)
        self.assertAlmostEqual(wait, 0.0)

    def test_stagger_spreads_workers_evenly_within_one_interval(self):
        delays = [stagger_delay(i, 4, 0.5) for i in range(4)]
        self.assertEqual(delays, [0.0, 0.125, 0.25, 0.375])
        self.assertTrue(all(0 <= d < 0.5 for d in delays))
        self.assertEqual(len({round(d, 6) for d in [stagger_delay(i, 30, 0.5) for i in range(30)]}), 30)

    def test_stagger_single_worker_has_no_delay(self):
        self.assertEqual(stagger_delay(0, 1, 0.5), 0.0)
        self.assertEqual(stagger_delay(0, 0, 0.5), 0.0)


class FixedRateTest(SimTestCase):
    def stamps(self, server_faults, interval, seconds, **worker_kwargs):
        """워커를 seconds초 돌리고, 읽기에 성공한 시각(monotonic)들을 돌려준다."""
        server = self.make_server(server_faults)
        times = []
        worker = self.make_worker(server.port, interval=interval, timeout=1.0,
                                  on_snapshot=lambda name, snap: times.append(time.monotonic()), **worker_kwargs)
        return server, worker, times

    async def test_period_does_not_grow_with_response_latency(self):
        # 응답 200ms + 간격 0.4초. 예전 방식(읽고 나서 쉬기)이면 주기가 0.6초, 고정 주기면 0.4초다.
        server, worker, times = self.stamps(Faults(delay_ms=200), 0.4, 4)
        await self.run_worker(worker)
        await self.wait_until(lambda: len(times) >= 9, timeout=8)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertLess(statistics.median(gaps), 0.46, f"주기가 늘어났습니다: {gaps}")
        self.assertGreater(statistics.median(gaps), 0.34)

    async def test_slow_response_does_not_cause_catch_up_bursts(self):
        # 응답 500ms > 간격 0.2초: 일정을 못 지킨다. 그래도 밀린 읽기를 몰아서 하면 안 된다(설비에 부담).
        server, worker, times = self.stamps(Faults(delay_ms=500), 0.2, 4)
        await self.run_worker(worker)
        await self.wait_until(lambda: len(times) >= 6, timeout=8)
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertGreater(min(gaps), 0.45, f"몰아서 읽었습니다: {gaps}")      # 응답 시간(0.5초)보다 짧게 연달아 읽지 않는다

    async def test_start_delay_postpones_the_first_read(self):
        server, worker, times = self.stamps(None, 0.1, 1, start_delay=0.6)
        started = time.monotonic()
        await self.run_worker(worker)
        await self.wait_until(lambda: len(times) >= 1, timeout=5)
        self.assertGreaterEqual(times[0] - started, 0.55)

    async def test_stop_during_start_delay_returns_promptly(self):
        server, worker, times = self.stamps(None, 0.1, 1, start_delay=30.0)
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.1)
        worker.stop()
        await asyncio.wait_for(task, timeout=1.0)                        # 30초를 기다리지 않는다
        self.assertEqual(times, [])

    async def test_schedule_restarts_after_reconnect(self):
        server = self.make_server()
        port = server.port
        times = []
        worker = self.make_worker(port, interval=0.1, on_snapshot=lambda n, s: times.append(time.monotonic()))
        await self.run_worker(worker)
        await self.wait_until(lambda: len(times) >= 3)
        server.stop()
        await self.wait_until(lambda: worker.stats.state == ConnState.BACKOFF)
        before = len(times)
        self.make_server(port=port, seed=2)
        await self.wait_until(lambda: len(times) >= before + 4, timeout=5)
        gaps = [b - a for a, b in zip(times[before:], times[before + 1:])]
        self.assertLess(max(gaps), 0.3, f"재접속 뒤 주기가 어긋났습니다: {gaps}")     # 끊긴 동안의 일정을 몰아서 따라잡지 않고 새로 시작
        self.assertGreater(min(gaps), 0.03, f"재접속 뒤 연달아 읽었습니다: {gaps}")   # 일정을 새로 시작하므로 바로 또 읽지 않는다 (간격 0.1초. 끝나는 시각은 읽기 시간만큼 흔들려서 여유를 둔다)


class ReconnectCapTest(SimTestCase):
    def test_default_backoff_cap_is_four_seconds(self):
        worker = DeviceWorker("T", "127.0.0.1", 1)
        self.assertEqual(worker.backoff_initial, 1.0)
        self.assertEqual(worker.backoff_max, 4.0)                          # 7단계 실측 때는 8초였다

    async def test_recovery_after_power_on_is_bounded_by_the_cap(self):
        # 비례를 줄여서 확인한다: 대기 상한 0.4초. 설비가 한참 꺼져 있다 켜져서 상한에 도달한 뒤에도, 켜진 지 상한 + 한 주기 안에 복구한다.
        server = self.make_server()
        port = server.port
        worker = self.make_worker(port, interval=0.1, backoff_initial=0.1, backoff_max=0.4)
        await self.run_worker(worker)
        await self.wait_until(lambda: worker.stats.reads_ok >= 2)
        server.stop()
        await self.wait_until(lambda: worker.stats.consecutive_failures >= 6, timeout=10)   # 대기가 상한(0.4초)까지 늘어난 뒤
        ok_before = worker.stats.reads_ok
        powered_on = time.monotonic()
        self.make_server(port=port, seed=2)
        await self.wait_until(lambda: worker.stats.reads_ok > ok_before, timeout=5)
        self.assertLess(time.monotonic() - powered_on, 0.4 + 0.1 + 0.3, "복구가 상한보다 늦습니다")


if __name__ == "__main__":
    unittest.main()
