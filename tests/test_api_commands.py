"""조회 주소(api.py)와 알람 확인 명령(commands.py) 시험.

    python -m unittest discover -s tests -v
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import snap  # noqa: E402

from collector.alarms import AlarmManager, KIND_TEMP_WARNING  # noqa: E402
from collector.api import ParamError, int_param, make_api  # noqa: E402
from collector.commands import MAX_NAME_LENGTH, clean_name, handle_command  # noqa: E402
from collector.store import Store  # noqa: E402


class PacketApiTest(unittest.TestCase):
    """/api/packet: 패킷 뷰어가 고른 설비 하나의 패킷 기록만 가져간다."""

    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)
        self.asked = []

        def packets(name):
            self.asked.append(name)
            return {"last": {"tid": 7}, "last_failed": None}

        self.api = make_api(self.store, ["ARM-01", "ARM-02"], packets=packets)

    def test_returns_packets_of_the_requested_device_only(self):
        status, body = self.api["/api/packet"]({"device": "ARM-02"})
        self.assertEqual(status, 200)
        self.assertEqual(body, {"device": "ARM-02", "last": {"tid": 7}, "last_failed": None})
        self.assertEqual(self.asked, ["ARM-02"])                # 다른 설비의 기록은 만들지도 않는다
        json.dumps(body)

    def test_unknown_or_missing_device_is_404_and_provider_is_not_called(self):
        for query in ({"device": "ARM-99"}, {}, {"device": ""}, {"device": "ARM-01; DROP TABLE samples"}):
            with self.subTest(query):
                self.assertEqual(self.api["/api/packet"](query)[0], 404)
        self.assertEqual(self.asked, [])

    def test_without_provider_it_answers_404(self):
        api = make_api(self.store, ["ARM-01"])
        status, body = api["/api/packet"]({"device": "ARM-01"})
        self.assertEqual(status, 404)
        self.assertIn("error", body)


class IntParamTest(unittest.TestCase):
    def test_default_valid_and_boundaries(self):
        self.assertEqual(int_param({}, "n", 10, 1, 100), 10)
        self.assertEqual(int_param({"n": "50"}, "n", 10, 1, 100), 50)
        self.assertEqual(int_param({"n": "1"}, "n", 10, 1, 100), 1)
        self.assertEqual(int_param({"n": "100"}, "n", 10, 1, 100), 100)
        self.assertEqual(int_param({"n": " 7 "}, "n", 10, 1, 100), 7)           # 공백은 허용

    def test_rejects_bad_values(self):
        for raw in ("abc", "1.5", "", "0", "101", "-3", "1e3", "٣", "--3", "-", "+5", "5\n5"):
            with self.subTest(raw), self.assertRaises(ParamError):
                int_param({"n": raw}, "n", 10, 1, 100)


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)
        for i in range(200):
            self.store.add_sample("ARM-01", snap(1000.0 + i * 0.5, count=i // 10, temp=40.0 + i / 10))
        self.store.add_event(1010.0, "ARM-01", "connected", "접속됨")
        self.store.add_event(1020.0, "ARM-02", "read_failed", "시간 초과")
        AlarmManager(self.store).on_snapshot("ARM-01", snap(1050.0, temp=66.0, status=1))
        self.api = make_api(self.store, ["ARM-01", "ARM-02"], clock=lambda: 1100.0)

    def call(self, path, **query):
        return self.api[path](query)

    def test_routes(self):
        self.assertEqual(set(self.api), {"/api/history", "/api/events", "/api/alarms", "/api/packet"})

    def test_history_ok(self):
        status, body = self.call("/api/history", device="ARM-01", minutes="1", points="40")
        self.assertEqual(status, 200)
        self.assertEqual(body["device"], "ARM-01")
        self.assertLessEqual(len(body["points"]), 40)
        self.assertEqual(body["points"][-1]["ts"], 1099.5)
        json.dumps(body)

    def test_history_defaults(self):
        status, body = self.call("/api/history", device="ARM-01")
        self.assertEqual(status, 200)
        self.assertEqual(body["to"] - body["from"], 600.0)                       # 기본 10분

    def test_history_unknown_or_missing_device(self):
        for query in ({"device": "ARM-99"}, {}, {"device": ""}, {"device": "ARM-01; DROP TABLE samples"},
                      {"device": "../../etc/passwd"}):
            with self.subTest(query):
                status, body = self.call("/api/history", **query)
                self.assertEqual(status, 404)
                self.assertIn("error", body)

    def test_history_bad_numbers_are_400(self):
        for query in ({"minutes": "0"}, {"minutes": "1441"}, {"minutes": "x"}, {"points": "19"}, {"points": "2001"}):
            with self.subTest(query):
                status, body = self.call("/api/history", device="ARM-01", **query)
                self.assertEqual(status, 400)
                self.assertIn("error", body)

    def test_the_sql_injection_attempt_did_not_break_anything(self):
        self.call("/api/history", device="ARM-01; DROP TABLE samples")
        self.assertGreater(self.store.sample_count(), 0)                         # 표는 그대로

    def test_events_newest_first_and_limit(self):
        status, body = self.call("/api/events", limit="1")
        self.assertEqual(status, 200)
        self.assertEqual([e["kind"] for e in body["events"]], ["read_failed"])
        status, body = self.call("/api/events")
        self.assertEqual([e["device"] for e in body["events"]], ["ARM-02", "ARM-01"])
        self.assertEqual(set(body["events"][0]), {"ts", "device", "kind", "message"})
        self.assertEqual(self.call("/api/events", limit="0")[0], 400)

    def test_alarms_open_and_all(self):
        status, body = self.call("/api/alarms")
        self.assertEqual(status, 200)
        self.assertEqual([a["kind"] for a in body["alarms"]], [KIND_TEMP_WARNING])
        self.assertEqual(body["alarms"][0]["state"], "활성·미확인")
        alarm_id = body["alarms"][0]["id"]
        self.store.ack_alarm(alarm_id, "홍길동", 1060.0)
        self.store.clear_alarm(alarm_id, 1061.0)                                 # 확인 후 해제 -> 종료
        self.assertEqual(self.call("/api/alarms")[1]["alarms"], [])              # 열린 알람은 없다
        all_alarms = self.call("/api/alarms", all="1")[1]["alarms"]
        self.assertEqual([a["state"] for a in all_alarms], ["종료"])             # 이력에는 남는다


class CleanNameTest(unittest.TestCase):
    def test_valid_names(self):
        self.assertEqual(clean_name("홍길동"), "홍길동")
        self.assertEqual(clean_name("  kim  "), "kim")
        self.assertEqual(clean_name("a" * MAX_NAME_LENGTH), "a" * MAX_NAME_LENGTH)
        self.assertEqual(clean_name("<b>x</b>"), "<b>x</b>")                     # 태그 글자는 그대로 저장 (화면이 textContent로 표시)

    def test_invalid_names(self):
        bad = ["", "   ", "a" * (MAX_NAME_LENGTH + 1), "홍\n길동", "홍\x00길동", "a\tb", "\x7f", None, 3, ["x"], {"a": 1}, True]
        for value in bad:
            with self.subTest(value=value):
                self.assertIsNone(clean_name(value))


class HandleCommandTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.addCleanup(self.store.close)
        self.manager = AlarmManager(self.store)
        self.manager.on_snapshot("ARM-01", snap(10.0, temp=66.0, status=1))
        self.alarm_id = self.store.open_alarms()[0]["id"]

    def run_cmd(self, msg):
        return handle_command(msg, self.manager, self.store, now=20.0)

    def test_ack_succeeds_once(self):
        reply = self.run_cmd({"type": "ack", "id": self.alarm_id, "by": "홍길동"})
        self.assertEqual(reply, {"type": "ack_result", "id": self.alarm_id, "ok": True, "reason": None})
        row = self.store.get_alarm(self.alarm_id)
        self.assertEqual((row["acked_by"], row["acked_at"]), ("홍길동", 20.0))
        again = self.run_cmd({"type": "ack", "id": self.alarm_id, "by": "다른사람"})
        self.assertFalse(again["ok"])
        self.assertIn("이미 확인", again["reason"])
        self.assertEqual(self.store.get_alarm(self.alarm_id)["acked_by"], "홍길동")   # 처음 확인한 사람이 남는다

    def test_unknown_alarm(self):
        reply = self.run_cmd({"type": "ack", "id": 999, "by": "홍길동"})
        self.assertEqual((reply["type"], reply["ok"]), ("ack_result", False))
        self.assertIn("없는 알람", reply["reason"])

    def test_invalid_ids_are_rejected_as_errors(self):
        for bad in ("3", 0, -1, True, False, 1.5, None, [1], {"a": 1}, "1; DROP TABLE alarms"):
            with self.subTest(bad):
                reply = self.run_cmd({"type": "ack", "id": bad, "by": "홍길동"})
                self.assertEqual(reply["type"], "error")
        self.assertIsNone(self.store.get_alarm(self.alarm_id)["acked_at"])        # 아무것도 확인되지 않았다

    def test_invalid_names_are_rejected(self):
        for bad in (None, "", "   ", "x" * 100, "a\nb", 5):
            with self.subTest(bad):
                reply = self.run_cmd({"type": "ack", "id": self.alarm_id, "by": bad})
                self.assertEqual(reply["type"], "error")
        reply = self.run_cmd({"type": "ack", "id": self.alarm_id})
        self.assertEqual(reply["type"], "error")                                  # 이름이 아예 없을 때
        self.assertIsNone(self.store.get_alarm(self.alarm_id)["acked_at"])

    def test_unknown_commands_and_weird_messages_never_raise(self):
        for msg in ({}, {"type": "reboot"}, {"type": None}, {"type": ["ack"]}, {"type": {"a": 1}},
                    {"id": 1, "by": "x"}, {"type": "ACK", "id": 1, "by": "x"}, {"type": "ack ", "id": 1}):
            with self.subTest(msg):
                reply = self.run_cmd(msg)
                self.assertEqual(reply["type"], "error")

    def test_extra_fields_are_ignored(self):
        reply = self.run_cmd({"type": "ack", "id": self.alarm_id, "by": "홍길동", "admin": True, "sql": "x"})
        self.assertTrue(reply["ok"])

    def test_ack_notifies_alarm_listeners(self):
        seen = []
        manager = AlarmManager(self.store, on_alarm=lambda action, row: seen.append((action, row["id"])))
        reply = handle_command({"type": "ack", "id": self.alarm_id, "by": "홍길동"}, manager, self.store, now=30.0)
        self.assertTrue(reply["ok"])
        self.assertEqual(seen, [("acked", self.alarm_id)])                        # 화면에도 '확인' 알림이 간다


if __name__ == "__main__":
    unittest.main()
