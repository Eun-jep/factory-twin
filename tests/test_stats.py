"""이력·지표(KPI) 계산 시험.

    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import snap  # noqa: E402

from collector.device import DeviceWorker  # noqa: E402
from collector.stats import KpiCache, availability, compute_kpis, downsample, history  # noqa: E402
from collector.store import Store  # noqa: E402


def fill(store, device, start, n, dt=0.5, status=0, counts=None, temp=40.0):
    """start부터 dt초 간격으로 n개의 측정값을 넣는다."""
    for i in range(n):
        count = counts[i] if counts else 0
        status_i = status(i) if callable(status) else status
        store.add_sample(device, snap(start + i * dt, count=count, temp=temp, status=status_i))


def make_worker(name, ok=0, failed=0, latency=0.0, connects=0):
    w = DeviceWorker(name, "127.0.0.1", 1)
    w.stats.reads_ok, w.stats.reads_failed = ok, failed
    w.stats.avg_latency_ms, w.stats.connects = latency, connects
    return w


class DownsampleTest(unittest.TestCase):
    def test_small_input_is_returned_as_is(self):
        rows = list(range(10))
        self.assertEqual(downsample(rows, 10), rows)
        self.assertEqual(downsample(rows, 50), rows)
        self.assertEqual(downsample([], 5), [])
        self.assertIsNot(downsample(rows, 50), rows)                 # 복사본을 돌려준다

    def test_never_exceeds_max_and_keeps_the_latest(self):
        """모든 크기·최대 개수 조합에서: 개수 제한을 지키고, 최신 값을 남기고, 순서를 유지한다."""
        for n in range(1, 130):
            for max_points in (1, 2, 3, 7, 20, 100):
                rows = [object() for _ in range(n)]
                out = downsample(rows, max_points)
                with self.subTest(n=n, max_points=max_points):
                    self.assertLessEqual(len(out), max_points)
                    self.assertIs(out[-1], rows[-1])                  # 가장 최근 값은 항상 남는다
                    positions = [rows.index(o) for o in out]
                    self.assertEqual(positions, sorted(set(positions)))   # 원래 순서, 중복 없음

    def test_first_point_is_kept_when_there_is_room(self):
        rows = list(range(100))
        out = downsample(rows, 10)
        self.assertEqual(out[0], 0)
        self.assertEqual(out[-1], 99)

    def test_stride_is_even(self):
        out = downsample(list(range(100)), 10)
        self.assertEqual(out[:9], [0, 10, 20, 30, 40, 50, 60, 70, 80])


class StoreHelperTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)

    def test_first_sample_ts(self):
        self.assertIsNone(self.store.first_sample_ts("ARM-01"))
        fill(self.store, "ARM-01", 100.0, 3)
        fill(self.store, "ARM-02", 50.0, 3)
        self.assertEqual(self.store.first_sample_ts("ARM-01"), 100.0)
        self.assertEqual(self.store.first_sample_ts("ARM-02"), 50.0)

    def test_count_samples_with_status_filter(self):
        fill(self.store, "ARM-01", 100.0, 10, dt=1.0, status=lambda i: 2 if i >= 6 else 0)
        self.assertEqual(self.store.count_samples("ARM-01", 100.0, 109.0), 10)
        self.assertEqual(self.store.count_samples("ARM-01", 100.0, 109.0, status_not=2), 6)
        self.assertEqual(self.store.count_samples("ARM-01", 103.0, 105.0), 3)          # 양 끝 포함
        self.assertEqual(self.store.count_samples("ARM-02", 0.0, 1000.0), 0)


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)

    def test_shape_and_window(self):
        fill(self.store, "ARM-01", 1000.0, 1200, dt=0.5, temp=55.5)           # 10분 분량
        h = history(self.store, "ARM-01", minutes=1, now=1600.0, max_points=300)
        self.assertEqual((h["device"], h["to"], h["from"]), ("ARM-01", 1600.0, 1540.0))
        self.assertEqual(h["total_samples"], 120)                              # 1540.0 ~ 1599.5, 0.5초 간격
        self.assertEqual(h["thresholds"], {"warn": 65.0, "fault": 80.0})
        point = h["points"][0]
        self.assertEqual(set(point), {"ts", "temp", "joint1", "joint2", "status", "phase", "count"})
        self.assertEqual(point["temp"], 55.5)
        self.assertGreaterEqual(point["ts"], 1540.0)

    def test_old_samples_are_excluded_and_order_is_kept(self):
        fill(self.store, "ARM-01", 1000.0, 1200, dt=0.5)
        h = history(self.store, "ARM-01", minutes=2, now=1600.0)
        stamps = [p["ts"] for p in h["points"]]
        self.assertEqual(stamps, sorted(stamps))
        self.assertTrue(all(1480.0 <= t <= 1600.0 for t in stamps))

    def test_downsampling_limits_points_but_keeps_latest(self):
        fill(self.store, "ARM-01", 1000.0, 1200, dt=0.5)
        h = history(self.store, "ARM-01", minutes=10, now=1600.0, max_points=50)
        self.assertLessEqual(len(h["points"]), 50)
        self.assertGreater(h["total_samples"], 50)                             # 원래 개수는 알려 준다
        self.assertEqual(h["points"][-1]["ts"], 1599.5)                        # 최신 표본이 남아 있다

    def test_empty_history(self):
        h = history(self.store, "ARM-01", minutes=5, now=100.0)
        self.assertEqual((h["points"], h["total_samples"]), ([], 0))

    def test_only_the_requested_device(self):
        fill(self.store, "ARM-01", 1000.0, 10, temp=41.0)
        fill(self.store, "ARM-02", 1000.0, 10, temp=77.0)
        temps = {p["temp"] for p in history(self.store, "ARM-02", 5, 1010.0)["points"]}
        self.assertEqual(temps, {77.0})


class AvailabilityTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)

    def test_always_running_is_100_percent(self):
        fill(self.store, "ARM-01", 1000.0, 120)                                # 60초 분량
        self.assertEqual(availability(self.store, "ARM-01", 1060.0, 600, 0.5), 1.0)

    def test_fault_samples_do_not_count(self):
        fill(self.store, "ARM-01", 1000.0, 120, status=lambda i: 2 if i >= 60 else 0)
        self.assertAlmostEqual(availability(self.store, "ARM-01", 1060.0, 600, 0.5), 0.5)

    def test_warning_still_counts_as_running(self):
        fill(self.store, "ARM-01", 1000.0, 120, status=1)
        self.assertEqual(availability(self.store, "ARM-01", 1060.0, 600, 0.5), 1.0)

    def test_gap_in_collection_counts_as_downtime(self):
        fill(self.store, "ARM-01", 1000.0, 60)                                 # 처음 30초만 수집됨
        self.assertAlmostEqual(availability(self.store, "ARM-01", 1060.0, 600, 0.5), 0.5)

    def test_time_before_the_first_sample_is_not_counted(self):
        """처음 켠 직후(과거 데이터가 없음)에 가동률이 낮게 나오지 않는다."""
        fill(self.store, "ARM-01", 5000.0, 20)                                 # 10초 분량만 있다
        self.assertEqual(availability(self.store, "ARM-01", 5010.0, 600, 0.5), 1.0)

    def test_window_shorter_than_data(self):
        fill(self.store, "ARM-01", 1000.0, 120, status=lambda i: 2 if i < 60 else 0)   # 앞 30초는 정지
        self.assertEqual(availability(self.store, "ARM-01", 1060.0, 30, 0.5), 1.0)     # 최근 30초만 보면 100%

    def test_never_above_100_percent(self):
        fill(self.store, "ARM-01", 1000.0, 240, dt=0.25)                       # 예상보다 촘촘하게 쌓인 경우
        self.assertEqual(availability(self.store, "ARM-01", 1060.0, 600, 0.5), 1.0)

    def test_unknown_when_no_data_or_too_short(self):
        self.assertIsNone(availability(self.store, "ARM-01", 1000.0, 600, 0.5))
        fill(self.store, "ARM-01", 1000.0, 1)
        self.assertIsNone(availability(self.store, "ARM-01", 1000.2, 600, 0.5))


class KpiTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)
        fill(self.store, "ARM-01", 1000.0, 3, dt=1.0, counts=[0, 1, 2])
        fill(self.store, "ARM-02", 1000.0, 2, dt=1.0, counts=[5, 7])
        self.workers = [
            make_worker("ARM-01", ok=90, failed=10, latency=2.0, connects=3),
            make_worker("ARM-02", ok=10, failed=0, latency=4.0, connects=1),
            make_worker("ARM-03"),                                              # 아직 값을 못 받음
        ]

    def test_aggregates(self):
        k = compute_kpis(self.store, self.workers, now=1003.0, window_minutes=10, interval=0.5)
        self.assertAlmostEqual(k["comm_success_rate"], 100 / 110)
        self.assertAlmostEqual(k["avg_latency_ms"], 3.0)                        # 값을 받은 설비만 평균
        self.assertEqual(k["reconnects"], 2)                                    # 처음 접속은 뺀다
        self.assertEqual(k["window_minutes"], 10)

    def test_production_sums_per_device(self):
        k = compute_kpis(self.store, self.workers, now=1003.0)
        self.assertEqual(k["per_device"]["ARM-01"]["production"], 2)
        self.assertEqual(k["per_device"]["ARM-02"]["production"], 2)
        self.assertEqual(k["per_device"]["ARM-03"]["production"], 0)
        self.assertEqual(k["production"], 4)

    def test_availability_ignores_devices_without_data(self):
        k = compute_kpis(self.store, self.workers, now=1003.0, interval=0.5)
        self.assertIsNone(k["per_device"]["ARM-03"]["availability"])
        known = [d["availability"] for d in k["per_device"].values() if d["availability"] is not None]
        self.assertAlmostEqual(k["availability"], sum(known) / len(known))

    def test_everything_unknown_without_workers_or_data(self):
        k = compute_kpis(Store(), [], now=1.0)
        self.assertEqual((k["availability"], k["comm_success_rate"], k["avg_latency_ms"]), (None, None, None))
        self.assertEqual((k["reconnects"], k["production"], k["per_device"]), (0, 0, {}))

    def test_production_handles_restart_reset(self):
        fill(self.store, "ARM-03", 1000.0, 5, dt=1.0, counts=[3, 4, 0, 1, 2])  # 설비 재시작
        k = compute_kpis(self.store, self.workers, now=1005.0)
        self.assertEqual(k["per_device"]["ARM-03"]["production"], 1 + 0 + 1 + 1)


class KpiCacheTest(unittest.TestCase):
    def test_recomputes_only_after_ttl(self):
        store = Store()
        self.addCleanup(store.close)
        cache = KpiCache(store, [], interval=0.5, ttl=5.0)
        first = cache.get(now=100.0)
        self.assertIs(cache.get(now=102.0), first)                              # 5초가 안 지나면 같은 결과
        self.assertIs(cache.get(now=104.9), first)
        second = cache.get(now=105.0)
        self.assertIsNot(second, first)                                          # 5초가 지나면 다시 계산
        self.assertEqual(second["computed_at"], 105.0)

    def test_workers_added_later_are_included(self):
        """워커 목록은 나중에 채워진다 (수집 서버가 시작할 때). 캐시는 같은 목록을 계속 봐야 한다."""
        store = Store()
        self.addCleanup(store.close)
        workers: list = []
        cache = KpiCache(store, workers, interval=0.5, ttl=1.0)
        self.assertEqual(cache.get(now=1.0)["per_device"], {})
        workers.append(make_worker("ARM-01", ok=5))
        self.assertIn("ARM-01", cache.get(now=10.0)["per_device"])


if __name__ == "__main__":
    unittest.main()
