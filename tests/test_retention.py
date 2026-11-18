"""측정값 보관 정책(collector/retention.py, Store.prune_old) 시험."""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import snap  # noqa: E402

from collector.retention import DEFAULT_RETENTION_DAYS, prune_loop, prune_once, retention_cutoff  # noqa: E402
from collector.store import Store  # noqa: E402

DAY = 86400.0


class CutoffTest(unittest.TestCase):
    def test_cutoff_is_now_minus_days(self):
        self.assertEqual(retention_cutoff(1_000_000.0, 7), 1_000_000.0 - 7 * DAY)
        self.assertEqual(retention_cutoff(1_000_000.0, 0.5), 1_000_000.0 - 0.5 * DAY)

    def test_zero_or_negative_days_disables_it(self):
        self.assertIsNone(retention_cutoff(1_000_000.0, 0))
        self.assertIsNone(retention_cutoff(1_000_000.0, -3))

    def test_default_is_seven_days(self):
        self.assertEqual(DEFAULT_RETENTION_DAYS, 7.0)


class PruneOldTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)

    def fill(self):
        for device in ("ARM-01", "ARM-02"):
            for t in range(100, 110):                           # 오래된 측정값 10개
                self.store.add_sample(device, snap(float(t)))
            for t in range(1000, 1005):                         # 최근 측정값 5개
                self.store.add_sample(device, snap(float(t)))
        self.store.add_event(50.0, "ARM-01", "connected", "오래된 이벤트")
        self.store.add_event(1000.0, "ARM-01", "connected", "최근 이벤트")

    def test_deletes_only_older_rows_for_every_device(self):
        self.fill()
        deleted = self.store.prune_old(500.0)
        self.assertEqual(deleted, 2 * 10 + 1)                   # 측정값 20개 + 이벤트 1개
        for device in ("ARM-01", "ARM-02"):
            self.assertEqual(self.store.sample_count(device), 5)
            self.assertEqual(self.store.first_sample_ts(device), 1000.0)
        self.assertEqual([r["message"] for r in self.store.recent_events(10)], ["최근 이벤트"])

    def test_row_exactly_at_the_cutoff_is_kept(self):
        self.store.add_sample("ARM-01", snap(500.0))
        self.store.add_sample("ARM-01", snap(499.9))
        self.assertEqual(self.store.prune_old(500.0), 1)
        self.assertEqual(self.store.first_sample_ts("ARM-01"), 500.0)

    def test_alarms_are_never_deleted(self):
        self.store.raise_alarm("ARM-01", "TEMP_WARNING", 2, "아주 오래된 알람", 66.0, 10.0)
        self.fill()
        self.store.prune_old(500.0)
        alarms = self.store.open_alarms()
        self.assertEqual(len(alarms), 1)
        self.assertEqual(alarms[0]["raised_at"], 10.0)

    def test_limit_deletes_in_chunks_per_device_and_eventually_everything(self):
        self.fill()
        first = self.store.prune_old(500.0, limit=4)
        self.assertEqual(first, 2 * 4 + 1)                      # 설비마다 최대 4건, 이벤트 1건
        self.assertEqual(self.store.sample_count("ARM-01"), 15 - 4)
        total = first
        while True:
            n = self.store.prune_old(500.0, limit=4)
            if n == 0:
                break
            total += n
        self.assertEqual(total, 21)
        self.assertEqual(self.store.sample_count(), 10)

    def test_nothing_to_delete_returns_zero(self):
        self.assertEqual(self.store.prune_old(500.0), 0)
        self.fill()
        self.store.prune_old(500.0)
        self.assertEqual(self.store.prune_old(500.0), 0)        # 두 번째는 지울 것이 없다


class PruneOnceTest(unittest.IsolatedAsyncioTestCase):
    async def test_repeats_until_nothing_is_left_and_yields_between_chunks(self):
        store = Store()
        self.addCleanup(store.close)
        for t in range(1000):
            store.add_sample("ARM-01", snap(float(t)))
        ticks = []

        async def ticker():                                     # 정리 중에도 다른 작업(수집)이 돌아야 한다
            while True:
                ticks.append(1)
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        total = await prune_once(store, 5000.0, chunk=100, pause=0.02)
        task.cancel()
        self.assertEqual(total, 1000)
        self.assertEqual(store.sample_count(), 0)
        self.assertGreaterEqual(len(ticks), 5)                  # 10덩어리 x 0.02초 동안 다른 작업이 여러 번 돌았다


class PruneLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_prunes_at_start_and_again_as_rows_age(self):
        store = Store()
        self.addCleanup(store.close)
        now = [1000.0]
        for t in (100.0, 900.0):
            store.add_sample("ARM-01", snap(t))
        reports = []
        task = asyncio.create_task(prune_loop(store, 0.005, period=0.05, clock=lambda: now[0],
                                              chunk=10, pause=0.0, on_pruned=reports.append))
        # 보관 기간 0.005일 = 432초. 시작 시각 1000 -> 기준 568: ts 100만 지워진다
        await asyncio.sleep(0.2)
        self.assertEqual(store.sample_count(), 1)
        self.assertEqual(reports, [1])
        now[0] = 2000.0                                         # 시간이 흘러 900도 오래된 것이 됐다
        await asyncio.sleep(0.2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(store.sample_count(), 0)
        self.assertEqual(reports, [1, 1])

    async def test_disabled_when_days_is_zero(self):
        store = Store()
        self.addCleanup(store.close)
        store.add_sample("ARM-01", snap(1.0))
        task = asyncio.create_task(prune_loop(store, 0, period=0.02, clock=lambda: 10 ** 9))
        await asyncio.sleep(0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(store.sample_count(), 1)               # 아무것도 지우지 않았다

    async def test_a_failing_prune_does_not_kill_the_loop(self):
        class Broken:
            calls = 0

            def prune_old(self, *_):
                Broken.calls += 1
                raise RuntimeError("디스크 오류")

        task = asyncio.create_task(prune_loop(Broken(), 1, period=0.03, clock=lambda: 10 ** 9))
        await asyncio.sleep(0.2)
        self.assertFalse(task.done())                           # 오류가 나도 계속 돈다
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertGreaterEqual(Broken.calls, 3)                # 다음 주기에 다시 시도했다


if __name__ == "__main__":
    unittest.main()
