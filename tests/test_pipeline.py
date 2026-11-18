"""수집 -> 저장 -> 알람 전체 흐름과 조회 도구 시험.

    python -m unittest discover -s tests -v
"""
import asyncio
import contextlib
import io
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import snap  # noqa: E402

import robot_sim as rs  # noqa: E402
from collector import tools  # noqa: E402
from collector.alarms import AlarmManager, KIND_FAULT, KIND_TEMP_WARNING  # noqa: E402
from collector.device import DeviceWorker  # noqa: E402
from collector.store import Store, alarm_state  # noqa: E402
from modbus_sim import SimServer  # noqa: E402


class PipelineTest(unittest.IsolatedAsyncioTestCase):
    async def wait_until(self, condition, timeout=15.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                return
            await asyncio.sleep(0.05)
        self.fail("조건이 시간 안에 충족되지 않았습니다.")

    async def test_overheat_flows_from_device_to_alarms(self):
        """가상 설비를 빨리 과열시키고, 측정값 저장과 알람 발생·해제가 이어지는지 본다."""
        sim = rs.RobotSim("ARM-X", seed=1, heat_rate=20.0, cool_rate=20.0, box_interval=(0.2, 0.2))
        server = SimServer(sim, "127.0.0.1", 0)
        server.start()
        self.addCleanup(server.stop)

        store = Store()
        self.addCleanup(store.close)
        actions = []
        manager = AlarmManager(store, clear_delay=0.3,
                               on_alarm=lambda action, row: actions.append((action, row["kind"])))

        def on_snapshot(name, snapshot):
            store.add_sample(name, snapshot)
            manager.on_snapshot(name, snapshot)

        def on_event(event):
            store.add_event(event.time, event.device, event.kind, event.message)
            manager.on_event(event)

        worker = DeviceWorker("ARM-X", "127.0.0.1", server.port, interval=0.05, timeout=0.3,
                              on_snapshot=on_snapshot, on_event=on_event)
        task = asyncio.create_task(worker.run())

        async def cleanup():
            worker.stop()
            await task
        self.addAsyncCleanup(cleanup)

        def finished():
            # 이상 알람이 발생 -> 해제 -> 다시 열림까지 한 바퀴 도는 것을 기다린다
            return (("raised", KIND_FAULT) in actions
                    and ("cleared", KIND_FAULT) in actions
                    and ("reopened", KIND_FAULT) in actions
                    and (("raised", KIND_TEMP_WARNING) in actions))

        await self.wait_until(finished)

        self.assertGreaterEqual(store.sample_count("ARM-X"), 20)           # 측정값이 쌓였다
        faults = [r for r in store.all_alarms() if r["kind"] == KIND_FAULT]
        warnings = [r for r in store.all_alarms() if r["kind"] == KIND_TEMP_WARNING]
        # 과열과 회복이 몇 번이나 반복됐어도 확인 전까지는 알람이 종류당 하나로 묶인다
        self.assertEqual(len(faults), 1)
        self.assertEqual(len(warnings), 1)
        self.assertGreaterEqual(faults[0]["repeat_count"], 2)
        self.assertEqual(faults[0]["severity"], 1)
        self.assertGreaterEqual(faults[0]["value"], rs.FAULT_TEMP)          # 처음 발생 당시 온도가 기준 이상
        self.assertTrue(any(r["kind"] == "connected" for r in store.recent_events(10)))
        # 같은 종류의 알람이 동시에 둘 이상 열려 있으면 안 된다 (중복 방지)
        active = [(r["device"], r["kind"]) for r in store.active_alarms()]
        self.assertEqual(len(active), len(set(active)))
        # 저장된 시계열은 시간순이고, 온도가 기준을 넘은 표본이 실제로 있다
        rows = store.samples_between("ARM-X", 0.0, time.time() + 1)
        stamps = [r["ts"] for r in rows]
        self.assertEqual(stamps, sorted(stamps))
        self.assertTrue(any(r["temp"] >= rs.FAULT_TEMP for r in rows))


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "t.db")

        store = Store(self.path)
        manager = AlarmManager(store, clear_delay=0)
        now = time.time()
        for i, count in enumerate([0, 1, 2, 3]):
            store.add_sample("ARM-01", snap(now - 40 + i * 10, count=count, temp=60.0 + i, status=0))
        store.add_sample("ARM-02", snap(now - 30, count=0))
        store.add_sample("ARM-02", snap(now - 20, count=2))
        manager.on_snapshot("ARM-01", snap(now - 5, count=3, temp=66.0, status=1))
        store.add_event(now - 5, "ARM-01", "connected", "접속됨")
        store.close()

    def run_tool(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tools.main(["--db", self.path, *args])
        return code, out.getvalue()

    def test_alarms_list_and_ack(self):
        code, out = self.run_tool("alarms")
        self.assertEqual(code, 0)
        self.assertIn("TEMP_WARNING", out)
        self.assertIn("활성·미확인", out)

        code, out = self.run_tool("ack", "1", "--by", "홍길동")
        self.assertEqual(code, 0)
        self.assertIn("확인했습니다", out)

        code, out = self.run_tool("alarms")
        self.assertIn("활성·확인됨", out)
        self.assertIn("홍길동", out)

        code, out = self.run_tool("ack", "1", "--by", "다른사람")      # 이미 확인됨
        self.assertEqual(code, 1)
        self.assertIn("이미 확인", out)

    def test_alarm_table_fits_in_narrow_terminal_and_detail_is_optional(self):
        # 한글은 2칸이므로 글자 수가 아니라 화면 칸 수로 잰다
        _, out = self.run_tool("alarms")
        for line in out.splitlines():
            self.assertLessEqual(tools.display_width(line), 100, f"줄이 너무 넓습니다: {line!r}")
        self.assertNotIn("주의 기준 초과", out)                     # 내용은 기본 표에 넣지 않는다
        _, detail = self.run_tool("alarms", "--detail")
        self.assertIn("주의 기준 초과", detail)                     # --detail이면 줄 아래에 나온다
        # 모든 열이 줄마다 같은 칸에서 시작한다 (머리글과 데이터 줄의 '상태' 열 시작 위치가 같다)
        lines = out.splitlines()
        header_col = tools.display_width(lines[0][:lines[0].index("상태")])
        data_col = tools.display_width(lines[1][:lines[1].index("활성")])
        self.assertEqual(header_col, data_col)

    def test_repeat_count_column(self):
        store = Store(self.path)
        manager = AlarmManager(store, clear_delay=0)
        now = time.time()
        manager.on_snapshot("ARM-03", snap(now, temp=66.0, status=1))
        manager.on_snapshot("ARM-03", snap(now + 1, status=0))                   # 해제
        manager.on_snapshot("ARM-03", snap(now + 2, temp=67.0, status=1))        # 다시 발생 -> 같은 알람
        store.close()
        _, out = self.run_tool("alarms", "--detail")
        line = next(l for l in out.splitlines() if "ARM-03" in l)
        self.assertEqual(line.split()[-2], "2")                                  # '횟수' 열
        self.assertIn("마지막 발생", out)
        self.assertIn("횟수", out.splitlines()[0])
        for text in out.splitlines():
            self.assertLessEqual(tools.display_width(text), 100)

    def test_ack_unknown_alarm_fails(self):
        code, out = self.run_tool("ack", "999", "--by", "홍길동")
        self.assertEqual(code, 1)
        self.assertIn("없습니다", out)

    def test_events_and_samples(self):
        _, out = self.run_tool("events")
        self.assertIn("connected", out)
        _, out = self.run_tool("samples", "ARM-01", "--limit", "2")
        self.assertEqual(len(out.strip().splitlines()), 3)             # 머리글 + 2줄
        _, out = self.run_tool("samples", "ARM-99")
        self.assertIn("측정값이 없습니다", out)

    def test_production(self):
        _, out = self.run_tool("production", "--minutes", "5")
        self.assertIn("ARM-01", out)
        self.assertRegex(out, r"ARM-01\s+3개")                          # 0 -> 3
        self.assertRegex(out, r"ARM-02\s+2개")                          # 0 -> 2
        self.assertRegex(out, r"합계\s+5개")

    def test_all_flag_shows_closed_alarms(self):
        self.run_tool("ack", "1", "--by", "홍길동")
        store = Store(self.path)
        store.clear_alarm(1, time.time())                               # 확인 후 해제 -> 종료
        store.close()
        _, out = self.run_tool("alarms")
        self.assertIn("표시할 알람이 없습니다", out)
        _, out = self.run_tool("alarms", "--all")
        self.assertIn("종료", out)


if __name__ == "__main__":
    unittest.main()
