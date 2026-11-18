"""알람 관리 시험. 시각을 직접 넣어서 시간 규칙(해제 지연 등)을 결정적으로 확인한다.

    python -m unittest discover -s tests -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import snap  # noqa: E402

from collector.alarms import (  # noqa: E402
    AlarmManager, KIND_COMM_LOST, KIND_COUNTER_RESET, KIND_FAULT, KIND_TEMP_WARNING,
    SEV_P1, SEV_P2, SEV_P3,
)
from collector.device import Event  # noqa: E402
from collector.store import Store, alarm_state  # noqa: E402

DEV = "ARM-01"


def fail(ts, kind="read_failed", device=DEV):
    return Event(ts, device, kind, "시험용 실패")


class AlarmTestCase(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)
        self.log = []
        self.mgr = self.new_manager()

    def new_manager(self, **kwargs):
        kwargs.setdefault("clear_delay", 3.0)
        return AlarmManager(self.store, on_alarm=lambda action, row: self.log.append(
            (action, row["kind"], row["id"])), **kwargs)

    def alarms(self, kind=None):
        rows = self.store.all_alarms()
        return [r for r in rows if kind is None or r["kind"] == kind]

    def state_of(self, kind):
        return alarm_state(self.alarms(kind)[0])


class StatusAlarmTest(AlarmTestCase):
    def test_warning_raises_once_and_updates_value(self):
        for ts, temp in [(0, 66.0), (1, 67.0), (2, 68.0)]:
            self.mgr.on_snapshot(DEV, snap(float(ts), temp=temp, status=1))
        rows = self.alarms()
        self.assertEqual(len(rows), 1)                               # 중복으로 만들지 않는다
        self.assertEqual((rows[0]["kind"], rows[0]["severity"]), (KIND_TEMP_WARNING, SEV_P2))
        self.assertEqual(rows[0]["value"], 66.0)                      # 발생 당시 값
        self.assertEqual(rows[0]["last_value"], 68.0)                 # 가장 최근 값
        self.assertEqual([a for a, _, _ in self.log], ["raised"])

    def test_normal_status_raises_nothing(self):
        for ts in range(5):
            self.mgr.on_snapshot(DEV, snap(float(ts), temp=40.0, status=0))
        self.assertEqual(self.alarms(), [])

    def test_clears_only_after_delay(self):
        self.mgr.on_snapshot(DEV, snap(0.0, temp=66.0, status=1))
        self.mgr.on_snapshot(DEV, snap(1.0, temp=64.0, status=0))      # 조건이 사라진 시각
        self.assertEqual(self.state_of(KIND_TEMP_WARNING), "활성·미확인")
        self.mgr.on_snapshot(DEV, snap(3.9, temp=64.0, status=0))      # 2.9초 지남
        self.assertEqual(self.state_of(KIND_TEMP_WARNING), "활성·미확인")
        self.mgr.on_snapshot(DEV, snap(4.0, temp=64.0, status=0))      # 3.0초 지남 -> 해제
        self.assertEqual(self.state_of(KIND_TEMP_WARNING), "해제됨·미확인")
        self.assertEqual([a for a, _, _ in self.log], ["raised", "cleared"])

    def test_flapping_does_not_create_new_alarms(self):
        for ts, status in [(0, 1), (1, 0), (2, 1), (3, 0), (4, 1), (5, 0), (6, 1)]:
            self.mgr.on_snapshot(DEV, snap(float(ts), temp=65.0, status=status))
        self.assertEqual(len(self.alarms()), 1)                        # 한 번만 발생
        self.assertEqual(self.state_of(KIND_TEMP_WARNING), "활성·미확인")
        self.assertEqual([a for a, _, _ in self.log], ["raised"])

    def test_clear_timer_restarts_after_flap(self):
        self.mgr.on_snapshot(DEV, snap(0.0, status=1))
        self.mgr.on_snapshot(DEV, snap(1.0, status=0))                 # 사라짐 시작
        self.mgr.on_snapshot(DEV, snap(2.0, status=1))                 # 다시 켜짐 -> 타이머 초기화
        self.mgr.on_snapshot(DEV, snap(3.0, status=0))                 # 다시 사라짐 시작
        self.mgr.on_snapshot(DEV, snap(5.5, status=0))                 # 2.5초: 아직
        self.assertEqual(self.state_of(KIND_TEMP_WARNING), "활성·미확인")
        self.mgr.on_snapshot(DEV, snap(6.0, status=0))                 # 3.0초: 해제
        self.assertEqual(self.state_of(KIND_TEMP_WARNING), "해제됨·미확인")

    def test_escalation_from_warning_to_fault(self):
        self.mgr.on_snapshot(DEV, snap(0.0, temp=66.0, status=1))
        self.mgr.on_snapshot(DEV, snap(1.0, temp=81.0, status=2))
        fault = self.alarms(KIND_FAULT)[0]
        self.assertEqual(fault["severity"], SEV_P1)
        self.assertEqual(self.state_of(KIND_TEMP_WARNING), "활성·미확인")   # 아직 지연 중
        self.mgr.on_snapshot(DEV, snap(4.0, temp=82.0, status=2))
        self.assertEqual(self.state_of(KIND_TEMP_WARNING), "해제됨·미확인")  # 주의는 해제
        self.assertEqual(alarm_state(self.alarms(KIND_FAULT)[0]), "활성·미확인")  # 이상은 유지

    def test_devices_are_independent(self):
        self.mgr.on_snapshot("ARM-01", snap(0.0, status=1))
        self.mgr.on_snapshot("ARM-02", snap(0.0, status=0))
        self.assertEqual([r["device"] for r in self.alarms()], ["ARM-01"])


class AckTest(AlarmTestCase):
    def test_ack_then_clear_closes(self):
        self.mgr.on_snapshot(DEV, snap(0.0, status=1))
        alarm_id = self.alarms()[0]["id"]
        self.assertTrue(self.mgr.ack(alarm_id, "홍길동", now=1.0))
        self.assertEqual(self.state_of(KIND_TEMP_WARNING), "활성·확인됨")
        self.mgr.on_snapshot(DEV, snap(2.0, status=0))
        self.mgr.on_snapshot(DEV, snap(5.0, status=0))
        self.assertEqual(self.state_of(KIND_TEMP_WARNING), "종료")
        self.assertEqual(self.store.open_alarms(), [])
        self.assertEqual([a for a, _, _ in self.log], ["raised", "acked", "cleared"])

    def test_clear_then_ack_closes(self):
        self.mgr.on_snapshot(DEV, snap(0.0, status=1))
        self.mgr.on_snapshot(DEV, snap(1.0, status=0))
        self.mgr.on_snapshot(DEV, snap(4.0, status=0))
        alarm_id = self.alarms()[0]["id"]
        self.assertEqual(alarm_state(self.alarms()[0]), "해제됨·미확인")
        self.assertEqual(len(self.store.open_alarms()), 1)             # 해제됐어도 확인 전엔 열려 있다
        self.assertTrue(self.mgr.ack(alarm_id, "홍길동", now=5.0))
        self.assertFalse(self.mgr.ack(alarm_id, "홍길동", now=6.0))     # 두 번 확인할 수 없다
        self.assertEqual(self.store.open_alarms(), [])

    def test_new_alarm_after_acked_and_closed(self):
        self.mgr.on_snapshot(DEV, snap(0.0, status=1))
        self.mgr.ack(self.alarms()[0]["id"], "홍길동", now=1.0)
        self.mgr.on_snapshot(DEV, snap(2.0, status=0))
        self.mgr.on_snapshot(DEV, snap(5.0, status=0))                 # 해제 -> 확인된 알람이라 종료
        self.mgr.on_snapshot(DEV, snap(10.0, status=1))                # 다시 발생 -> 새 알람
        rows = self.alarms(KIND_TEMP_WARNING)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(alarm_state(r) for r in rows), ["종료", "활성·미확인"])


class CommAlarmTest(AlarmTestCase):
    def test_comm_lost_after_three_consecutive_failures(self):
        self.mgr.on_event(fail(1.0))
        self.mgr.on_event(fail(2.0, "connect_failed"))
        self.assertEqual(self.alarms(), [])                            # 2번까지는 알람이 아니다
        self.mgr.on_event(fail(3.0, "connect_failed"))
        rows = self.alarms(KIND_COMM_LOST)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["severity"], SEV_P1)
        self.mgr.on_event(fail(4.0, "connect_failed"))
        self.assertEqual(len(self.alarms(KIND_COMM_LOST)), 1)          # 중복 없이 값만 갱신
        self.assertEqual(self.alarms(KIND_COMM_LOST)[0]["last_value"], 4.0)

    def test_comm_recovery_clears_immediately(self):
        for ts in (1.0, 2.0, 3.0):
            self.mgr.on_event(fail(ts))
        self.mgr.on_snapshot(DEV, snap(4.0))                           # 값이 다시 오면 바로 해제
        self.assertEqual(self.state_of(KIND_COMM_LOST), "해제됨·미확인")

    def test_success_resets_failure_count(self):
        self.mgr.on_event(fail(1.0))
        self.mgr.on_event(fail(2.0))
        self.mgr.on_snapshot(DEV, snap(3.0))
        self.mgr.on_event(fail(4.0))
        self.mgr.on_event(fail(5.0))
        self.assertEqual(self.alarms(), [])                            # 연속이 아니므로 알람 없음

    def test_non_failure_events_are_ignored(self):
        for ts in range(6):
            self.mgr.on_event(Event(float(ts), DEV, "connected", "접속됨"))
        self.assertEqual(self.alarms(), [])

    def test_disconnect_counts_as_failure(self):
        self.mgr.on_event(fail(1.0, "disconnected"))
        self.mgr.on_event(fail(2.0, "connect_failed"))
        self.mgr.on_event(fail(3.0, "connect_failed"))
        self.assertEqual(len(self.alarms(KIND_COMM_LOST)), 1)

    def test_failures_counted_per_device(self):
        self.mgr.on_event(fail(1.0, device="ARM-01"))
        self.mgr.on_event(fail(2.0, device="ARM-02"))
        self.mgr.on_event(fail(3.0, device="ARM-03"))
        self.assertEqual(self.alarms(), [])                            # 합치면 3번이지만 설비별로는 1번


class CounterResetTest(AlarmTestCase):
    def test_reset_creates_informational_alarm(self):
        for ts, count in [(1, 5), (2, 6), (3, 0)]:
            self.mgr.on_snapshot(DEV, snap(float(ts), count=count))
        rows = self.alarms(KIND_COUNTER_RESET)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["severity"], SEV_P3)
        self.assertIn("6 -> 0", rows[0]["message"])
        self.assertEqual(alarm_state(rows[0]), "해제됨·미확인")           # 일회성: 확인을 기다린다

    def test_wraparound_is_not_a_reset(self):
        for ts, count in [(1, 65534), (2, 65535), (3, 1)]:
            self.mgr.on_snapshot(DEV, snap(float(ts), count=count))
        self.assertEqual(self.alarms(), [])

    def test_increasing_count_is_fine(self):
        for ts in range(5):
            self.mgr.on_snapshot(DEV, snap(float(ts), count=ts * 2))
        self.assertEqual(self.alarms(), [])

    def test_first_sample_is_never_a_reset(self):
        self.mgr.on_snapshot(DEV, snap(1.0, count=0))
        self.assertEqual(self.alarms(), [])


class RepeatTest(AlarmTestCase):
    """해제된 지 얼마 안 된 미확인 알람이 다시 발생하면 새로 만들지 않고 다시 연다."""

    def cycle(self, start, temp=66.0):
        """start에 발생, start+1에 조건이 사라지고, start+4에 해제된다."""
        self.mgr.on_snapshot(DEV, snap(start, temp=temp, status=1))
        self.mgr.on_snapshot(DEV, snap(start + 1, status=0))
        self.mgr.on_snapshot(DEV, snap(start + 4, status=0))

    def test_recurrence_reopens_same_alarm(self):
        self.cycle(0.0, temp=66.0)
        self.assertEqual(self.state_of(KIND_TEMP_WARNING), "해제됨·미확인")
        self.mgr.on_snapshot(DEV, snap(10.0, temp=67.5, status=1))     # 6초 뒤 다시 발생
        rows = self.alarms(KIND_TEMP_WARNING)
        self.assertEqual(len(rows), 1)                                  # 새로 만들지 않았다
        row = rows[0]
        self.assertEqual(row["repeat_count"], 2)
        self.assertEqual(alarm_state(row), "활성·미확인")
        self.assertEqual(row["raised_at"], 0.0)                         # 최초 발생 시각은 그대로
        self.assertEqual(row["last_raised_at"], 10.0)                   # 마지막 발생 시각
        self.assertEqual(row["value"], 66.0)                            # 처음 값은 보존
        self.assertEqual(row["last_value"], 67.5)
        self.assertIn("67.5", row["message"])                           # 내용은 최신으로
        self.assertEqual([a for a, _, _ in self.log], ["raised", "cleared", "reopened"])

    def test_window_boundary(self):
        self.cycle(0.0)                                                 # 4.0에 해제됨
        self.mgr.on_snapshot(DEV, snap(64.0, status=1))                 # 정확히 60초 뒤: 아직 묶는다
        self.assertEqual(len(self.alarms(KIND_TEMP_WARNING)), 1)

    def test_recurrence_after_window_creates_new_alarm(self):
        self.cycle(0.0)
        self.mgr.on_snapshot(DEV, snap(64.1, status=1))                 # 60초를 넘김
        rows = self.alarms(KIND_TEMP_WARNING)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["repeat_count"] for r in rows], [1, 1])

    def test_custom_window(self):
        short = self.new_manager(reopen_window=5.0)
        short.on_snapshot(DEV, snap(0.0, status=1))
        short.on_snapshot(DEV, snap(1.0, status=0))
        short.on_snapshot(DEV, snap(4.0, status=0))                     # 4.0에 해제
        short.on_snapshot(DEV, snap(10.0, status=1))                    # 6초 뒤: 창(5초)을 넘김
        self.assertEqual(len(self.alarms(KIND_TEMP_WARNING)), 2)

    def test_many_cycles_make_one_alarm(self):
        for i in range(5):
            self.cycle(i * 10.0, temp=66.0 + i)
        self.mgr.on_snapshot(DEV, snap(50.0, status=1))
        rows = self.alarms(KIND_TEMP_WARNING)
        self.assertEqual(len(rows), 1)                                  # 알람이 줄줄이 쌓이지 않는다
        self.assertEqual(rows[0]["repeat_count"], 6)

    def test_acked_alarm_is_not_reopened(self):
        self.cycle(0.0)
        first = self.alarms(KIND_TEMP_WARNING)[0]["id"]
        self.mgr.ack(first, "홍길동", now=5.0)                          # 확인하면 종료된다
        self.mgr.on_snapshot(DEV, snap(10.0, status=1))                 # 다시 발생 -> 새 알람이어야 한다
        rows = self.alarms(KIND_TEMP_WARNING)
        self.assertEqual(len(rows), 2)
        self.assertEqual({alarm_state(r) for r in rows}, {"종료", "활성·미확인"})

    def test_active_alarm_is_never_reopened(self):
        self.mgr.on_snapshot(DEV, snap(0.0, status=1))
        self.mgr.on_snapshot(DEV, snap(1.0, status=1))
        self.assertEqual(self.alarms(KIND_TEMP_WARNING)[0]["repeat_count"], 1)   # 계속 켜져 있으면 횟수가 오르지 않는다

    def test_reopened_alarm_can_be_acked_and_closed(self):
        self.cycle(0.0)
        self.mgr.on_snapshot(DEV, snap(10.0, status=1))                 # 다시 열림
        alarm_id = self.alarms()[0]["id"]
        self.assertTrue(self.mgr.ack(alarm_id, "홍길동", now=11.0))
        self.assertEqual(self.state_of(KIND_TEMP_WARNING), "활성·확인됨")
        self.mgr.on_snapshot(DEV, snap(12.0, status=0))
        self.mgr.on_snapshot(DEV, snap(15.0, status=0))
        self.assertEqual(self.state_of(KIND_TEMP_WARNING), "종료")
        self.assertEqual(self.store.open_alarms(), [])

    def test_devices_and_kinds_are_not_merged(self):
        self.mgr.on_snapshot("ARM-01", snap(0.0, status=1))
        self.mgr.on_snapshot("ARM-01", snap(1.0, status=0))
        self.mgr.on_snapshot("ARM-01", snap(4.0, status=0))             # ARM-01 주의 해제
        self.mgr.on_snapshot("ARM-02", snap(10.0, status=1))            # 다른 설비
        self.mgr.on_snapshot("ARM-01", snap(10.0, status=2))            # 같은 설비의 다른 종류
        self.assertEqual(len(self.alarms()), 3)

    def test_counter_reset_repeats_merge(self):
        for ts, count in [(1, 5), (2, 0), (3, 3), (4, 0)]:              # 5->0, 0->3, 3->0: 초기화 두 번
            self.mgr.on_snapshot(DEV, snap(float(ts), count=count))
        rows = self.alarms(KIND_COUNTER_RESET)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["repeat_count"], 2)
        self.assertEqual(alarm_state(rows[0]), "해제됨·미확인")           # 일회성이라 해제된 채 확인을 기다린다
        self.assertEqual([a for a, k, _ in self.log if k == KIND_COUNTER_RESET], ["raised", "reopened"])

    def test_comm_lost_flaps_merge(self):
        for ts in (1.0, 2.0, 3.0):
            self.mgr.on_event(fail(ts))
        self.mgr.on_snapshot(DEV, snap(4.0))                            # 복구 -> 해제
        for ts in (5.0, 6.0, 7.0):
            self.mgr.on_event(fail(ts))                                 # 다시 끊김
        rows = self.alarms(KIND_COMM_LOST)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["repeat_count"], 2)
        self.assertEqual(alarm_state(rows[0]), "활성·미확인")


class ContinuityTest(AlarmTestCase):
    def test_new_manager_takes_over_open_alarms(self):
        self.mgr.on_snapshot(DEV, snap(0.0, status=1))
        again = self.new_manager()                                     # 수집 서버를 다시 켠 것과 같다
        again.on_snapshot(DEV, snap(10.0, status=1))
        self.assertEqual(len(self.alarms(KIND_TEMP_WARNING)), 1)       # 새로 만들지 않는다
        again.on_snapshot(DEV, snap(11.0, status=0))
        again.on_snapshot(DEV, snap(14.0, status=0))
        self.assertEqual(self.state_of(KIND_TEMP_WARNING), "해제됨·미확인")

    def test_callback_error_does_not_break_manager(self):
        def boom(action, row):
            raise RuntimeError("콜백 오류")
        mgr = AlarmManager(self.store, on_alarm=boom)
        with self.assertLogs("collector.alarms", level="ERROR"):
            mgr.on_snapshot("ARM-09", snap(0.0, status=2))
        self.assertEqual(len(self.alarms(KIND_FAULT)), 1)               # 알람은 정상적으로 만들어졌다


if __name__ == "__main__":
    unittest.main()
