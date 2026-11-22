from configparser import ConfigParser
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch

from calendar_source import Event, KST, build_events, parse_title
from soop_client import SoopClient, SoopError, load_config
from sync import changes, synchronize


def soop_event(remote_id, title, day="2026-11-23", event_time="20:00:00", calendar_type=1):
    return {"idx": remote_id, "title": title, "eventDate": day,
            "eventTime": event_time, "calendarType": calendar_type}


class SyncTests(unittest.TestCase):
    def test_schedule_cell_and_blank_row(self):
        events, rolling = build_events(
            [("1", "dkcal_mdays_20261117", "20:00 방송\r\n!개인 일정\r\n합방"),
             ("2", "dkcal_mdays_20261118", "")], [], date(2026, 11, 15))
        self.assertEqual(events, [Event("cell:1", "2026-11-17", "방송", "20:00"),
                                  Event("cell:1", "2026-11-17", "합방", "12:00", 2)])
        self.assertFalse(rolling)
        self.assertEqual(parse_title("24:00 확인"), ("24:00 확인", "12:00", 1))
        self.assertEqual(parse_title("a" * 14 + "😀끝"), ("a" * 14, "12:00", 1))

    def test_synchronize_sends_calendar_types_from_full_event_content(self):
        title = "a" * 15
        with TemporaryDirectory() as folder, patch("sync.datetime") as clock, \
                patch("sync.read_rows") as read_rows, patch.object(SoopClient, "calendar_request") as request:
            config_path = Path(folder) / "config.ini"
            state_path = Path(folder) / "sync.db"
            config_path.write_text("[soop]\nusername = example\npassword = example\n", encoding="utf-8")
            clock.now.return_value = datetime(2026, 11, 22, tzinfo=KST)
            cells = [("1", "dkcal_mdays_20261123", f"20:00 {title} 합방\n21:00 정기 방송")]
            rows = [("2", "", "2026-11-23", "", "", "22:00 합방 대신 휴방", "FREQ=DAILY;COUNT=1")]
            read_rows.return_value = (cells, rows)
            request.side_effect = [{"days": []}, {"days": []}, {"events": [{"idx": 1}]},
                                   {"days": []}, {"events": [{"idx": 2}]},
                                   {"days": []}, {"events": [{"idx": 3}]}]

            synchronize(config_path, state_path, lambda: False)

            posts = [entry.kwargs["data"] for entry in request.call_args_list if entry.args == ("POST",)]
            self.assertEqual([(post["title"], post["calendarType"]) for post in posts],
                             [(title, 2), ("정기 방송", 1), ("합방 대신 휴방", 3)])

            cells[0] = ("1", "dkcal_mdays_20261123", f"20:00 {title} 휴방\n21:00 정기 방송")
            request.reset_mock()
            remote = {"days": [{"events": [{"idx": 1}, {"idx": 2}, {"idx": 3}]}]}
            request.side_effect = [remote, {"events": [{"idx": 1}]}, remote]
            synchronize(config_path, state_path, lambda: False)
            synchronize(config_path, state_path, lambda: False)

            mutations = [entry for entry in request.call_args_list if entry.args != ("GET",)]
            self.assertEqual(mutations, [call("PUT", event_id=1, payload={
                "userId": "example", "title": title, "calendarType": 3, "eventTime": "20:00",
                "calendarNo": 1, "startDate": "2026-11-23",
            })])

    def test_matching_multiple_and_deleted_lines(self):
        event = Event("cell:1", "2026-11-17", "방송", "03:00")
        changed = Event("cell:1", "2026-11-17", "방송", "20:00")
        self.assertEqual(changes([event, changed], {1: event, 2: event}), ([], [(2, changed)], []))
        self.assertEqual(changes([event], {1: event}), ([], [], []))
        self.assertEqual(changes([], {1: event}), ([], [], [(1, event)]))

    def test_retiming_events_in_the_same_cell_keeps_their_ids(self):
        current = {1: Event("cell:1", "2026-11-17", "방송", "03:00"),
                   2: Event("cell:1", "2026-11-17", "합방", "03:00", 2)}
        wanted = [Event("cell:1", "2026-11-17", "합방", "12:00", 2),
                  Event("cell:1", "2026-11-17", "방송", "12:00")]
        self.assertEqual(changes(wanted, current), ([], [(1, wanted[1]), (2, wanted[0])], []))

    def test_synchronize_leaves_past_events_and_links_intact(self):
        with TemporaryDirectory() as folder, patch("sync.datetime") as clock, \
                patch("sync.read_rows") as read_rows, patch("sync.SoopClient") as make_client:
            config_path = Path(folder) / "config.ini"
            state_path = Path(folder) / "sync.db"
            client = make_client.return_value
            client.user = "example"
            client.list_events.return_value = []
            client.create.side_effect = [1, 2, 3, 4, 5, 6]

            clock.now.return_value = datetime(2026, 11, 20, tzinfo=KST)
            read_rows.return_value = ([("1", "dkcal_mdays_20261120", "방송 A\n방송 B"),
                                       ("2", "dkcal_mdays_20261120", "방송 C"),
                                       ("3", "dkcal_mdays_20261120", "방송 D")], [])
            synchronize(config_path, state_path, lambda: False)
            client.reset_mock()

            clock.now.return_value = datetime(2026, 11, 22, 21, tzinfo=KST)
            read_rows.return_value = ([("1", "dkcal_mdays_20261120", "변경 A\n변경 B"),
                                       ("2", "dkcal_mdays_20261120", "변경 C"),
                                       ("4", "dkcal_mdays_20261121", "지난 일정"),
                                       ("5", "dkcal_mdays_20261122", "당일 일정"),
                                       ("6", "dkcal_mdays_20261123", "내일 일정")], [])
            synchronize(config_path, state_path, lambda: False)

            self.assertEqual(client.list_events.call_args_list, [call("2026-11-22"), call("2026-11-23")])
            self.assertEqual(client.create.call_args_list, [call(Event("cell:5", "2026-11-22", "당일 일정", "12:00")),
                                                            call(Event("cell:6", "2026-11-23", "내일 일정", "12:00"))])
            client.update.assert_not_called()
            client.delete.assert_not_called()
            with closing(sqlite3.connect(state_path)) as db:
                self.assertEqual(db.execute("SELECT remote_id, title FROM synced WHERE day < ? ORDER BY remote_id",
                                            ("2026-11-22",)).fetchall(),
                                 [(1, "방송 A"), (2, "방송 B"), (3, "방송 C"), (4, "방송 D")])

    def test_synchronize_registers_only_within_90_days_including_today(self):
        with TemporaryDirectory() as folder, patch("sync.datetime") as clock, \
                patch("sync.read_rows") as read_rows, patch("sync.SoopClient") as make_client:
            config_path = Path(folder) / "config.ini"
            state_path = Path(folder) / "sync.db"
            client = make_client.return_value
            client.user = "example"
            client.create.side_effect = [1, 2]
            client.list_events.return_value = []
            read_rows.return_value = ([("1", "dkcal_mdays_20270220", "다음 날"),
                                       ("2", "dkcal_mdays_20270219", "마지막 날")], [])

            clock.now.return_value = datetime(2026, 11, 22, tzinfo=KST)
            synchronize(config_path, state_path, lambda: False)
            self.assertEqual(client.list_events.call_args_list, [call("2027-02-19"), call("2027-02-19")])
            client.create.assert_called_once_with(Event("cell:2", "2027-02-19", "마지막 날", "12:00"))

            client.reset_mock()
            client.list_events.return_value = [soop_event(1, "마지막 날", day="2027-02-19")]
            clock.now.return_value = datetime(2026, 11, 23, tzinfo=KST)
            synchronize(config_path, state_path, lambda: False)
            client.create.assert_called_once_with(Event("cell:1", "2027-02-20", "다음 날", "12:00"))

    def test_synchronize_leaves_existing_events_when_replacements_are_too_far_out(self):
        with TemporaryDirectory() as folder, patch("sync.datetime") as clock, \
                patch("sync.read_rows") as read_rows, patch("sync.SoopClient") as make_client:
            config_path = Path(folder) / "config.ini"
            state_path = Path(folder) / "sync.db"
            client = make_client.return_value
            client.user = "example"
            client.list_events.return_value = []
            client.create.side_effect = [1, 2]
            clock.now.return_value = datetime(2026, 11, 23, tzinfo=KST)
            read_rows.return_value = ([("1", "dkcal_mdays_20270220", "방송 A\n방송 B")], [])
            synchronize(config_path, state_path, lambda: False)

            client.reset_mock()
            clock.now.return_value = datetime(2026, 11, 22, tzinfo=KST)
            read_rows.return_value = ([("1", "dkcal_mdays_20270220", "변경 A\n변경 B")], [])
            client.list_events.return_value = [soop_event(1, "방송 A", day="2027-02-20"),
                                               soop_event(2, "방송 B", day="2027-02-20")]
            synchronize(config_path, state_path, lambda: False)
            client.create.assert_not_called()
            client.update.assert_not_called()
            client.delete.assert_not_called()

    def test_synchronize_restores_missing_link_and_continues_other_changes(self):
        with TemporaryDirectory() as folder, patch("sync.datetime") as clock, \
                patch("sync.read_rows") as read_rows, patch("sync.SoopClient") as make_client:
            config_path = Path(folder) / "config.ini"
            state_path = Path(folder) / "sync.db"
            client = make_client.return_value
            client.user = "example"
            client.create.side_effect = [1, 2, 3, 4, 5]
            client.list_events.return_value = []
            clock.now.return_value = datetime(2026, 11, 22, tzinfo=KST)
            unchanged = ("1", "dkcal_mdays_20261123", "20:00 복구할 일정")
            read_rows.return_value = ([unchanged, ("2", "dkcal_mdays_20261123", "20:00 방송"),
                                       ("3", "dkcal_mdays_20261123", "20:00 함께 삭제된 일정")], [])
            synchronize(config_path, state_path, lambda: False)

            client.reset_mock()
            read_rows.return_value = ([unchanged, ("2", "dkcal_mdays_20261123", "21:00 방송"),
                                       ("4", "dkcal_mdays_20261123", "20:00 신규")], [])
            client.list_events.return_value = [{"title": "ID 없는 응답"}]
            with self.assertRaisesRegex(SoopError, "일정 ID"):
                synchronize(config_path, state_path, lambda: False)
            client.create.assert_not_called()
            with closing(sqlite3.connect(state_path)) as db:
                self.assertEqual(db.execute("SELECT remote_id FROM synced ORDER BY remote_id").fetchall(),
                                 [(1,), (2,), (3,)])

            client.list_events.return_value = [soop_event(2, "방송")]
            synchronize(config_path, state_path, lambda: False)
            self.assertEqual(client.create.call_args_list,
                             [call(Event("cell:1", "2026-11-23", "복구할 일정", "20:00")),
                              call(Event("cell:4", "2026-11-23", "신규", "20:00"))])
            client.update.assert_called_once_with(2, Event("cell:2", "2026-11-23", "방송", "21:00"))
            client.delete.assert_not_called()
            with closing(sqlite3.connect(state_path)) as db:
                self.assertEqual(db.execute("SELECT remote_id FROM synced ORDER BY remote_id").fetchall(),
                                 [(2,), (4,), (5,)])

    def test_synchronize_recovers_when_event_disappears_before_update(self):
        with TemporaryDirectory() as folder, patch("sync.datetime") as clock, \
                patch("sync.read_rows") as read_rows, patch.object(SoopClient, "_has_ticket", return_value=True), \
                patch.object(SoopClient, "_request") as request:
            config_path = Path(folder) / "config.ini"
            state_path = Path(folder) / "sync.db"
            config_path.write_text("[soop]\nusername = example\npassword = example\n", encoding="utf-8")
            clock.now.return_value = datetime(2026, 11, 22, tzinfo=KST)
            read_rows.return_value = ([("1", "dkcal_mdays_20261123", "20:00 방송")], [])
            empty = SimpleNamespace(status_code=200, json=lambda: {"days": []})
            created = SimpleNamespace(status_code=200, json=lambda: {"events": [{"idx": 1}]})
            request.side_effect = [empty, empty, created]
            synchronize(config_path, state_path, lambda: False)

            request.reset_mock()
            read_rows.return_value = ([("1", "dkcal_mdays_20261123", "21:00 방송"),
                                       ("2", "dkcal_mdays_20261123", "20:00 신규")], [])
            listed = SimpleNamespace(status_code=200, json=lambda: {"days": [{"events": [soop_event(1, "방송")]}]})
            missing = SimpleNamespace(status_code=404)
            new = SimpleNamespace(status_code=200, json=lambda: {"events": [{"idx": 2}]})
            restored = SimpleNamespace(status_code=200, json=lambda: {"events": [{"idx": 3}]})
            request.side_effect = [listed, missing, empty, empty, new, empty, restored]
            synchronize(config_path, state_path, lambda: False)

            methods = [entry.args[0] for entry in request.call_args_list]
            self.assertEqual(methods, ["GET", "PUT", "GET", "GET", "POST", "GET", "POST"])
            with closing(sqlite3.connect(state_path)) as db:
                self.assertEqual(db.execute("SELECT remote_id, source, time FROM synced ORDER BY remote_id").fetchall(),
                                 [(2, "cell:2", "20:00"), (3, "cell:1", "21:00")])

    def test_synchronize_recovers_schedule_after_state_loss(self):
        with TemporaryDirectory() as folder, patch("sync.datetime") as clock, \
                patch("sync.read_rows") as read_rows, patch("sync.SoopClient") as make_client:
            config_path = Path(folder) / "config.ini"
            state_path = Path(folder) / "sync.db"
            clock.now.return_value = datetime(2026, 11, 22, tzinfo=KST)
            read_rows.return_value = ([("1", "dkcal_mdays_20261123", "20:00 방송")], [])
            client = make_client.return_value
            client.user = "example"
            client.list_events.return_value = []
            client.create.return_value = 7

            synchronize(config_path, state_path, lambda: False)
            client.create.assert_called_once()
            state_path.unlink()
            client.reset_mock()
            client.list_events.return_value = [soop_event("7", "방송", calendar_type="1")]

            synchronize(config_path, state_path, lambda: False)
            client.create.assert_not_called()
            client.list_events.assert_called_once()

            read_rows.return_value = ([("1", "dkcal_mdays_20261123", "21:00 방송")], [])
            synchronize(config_path, state_path, lambda: False)
            client.update.assert_called_once_with(7, Event("cell:1", "2026-11-23", "방송", "21:00"))
            self.assertEqual(client.list_events.call_args_list, [call("2026-11-23"), call("2026-11-23")])

    def test_synchronize_links_each_matching_soop_id_once(self):
        with TemporaryDirectory() as folder, patch("sync.datetime") as clock, \
                patch("sync.read_rows") as read_rows, patch("sync.SoopClient") as make_client:
            config_path = Path(folder) / "config.ini"
            state_path = Path(folder) / "sync.db"
            clock.now.return_value = datetime(2026, 11, 22, tzinfo=KST)
            read_rows.return_value = ([("1", "dkcal_mdays_20261123", "20:00 방송\n20:00 합방\n20:00 합방\n20:00 회의"),
                                       ("2", "dkcal_mdays_20261121", "20:00 지난 방송")], [])
            client = make_client.return_value
            client.user = "example"
            guest = soop_event(8, "합방", calendar_type=2)
            client.list_events.return_value = [soop_event(7, "방송"), soop_event(3, "방송"), guest, guest,
                                               soop_event(6, "합방"),
                                               soop_event(9, "회의", event_time="19:00:00"),
                                               soop_event(10, "회의", calendar_type=2),
                                               soop_event(5, "지난 방송", day="2026-11-21")]
            client.create.side_effect = [11, 12, 13, 14]

            synchronize(config_path, state_path, lambda: False)

            self.assertEqual(client.create.call_args_list, [call(Event("cell:1", "2026-11-23", "합방", "20:00", 2)),
                                                            call(Event("cell:1", "2026-11-23", "회의", "20:00"))])
            with closing(sqlite3.connect(state_path)) as db:
                self.assertEqual(db.execute("SELECT remote_id FROM synced ORDER BY remote_id").fetchall(),
                                 [(3,), (8,), (11,), (12,)])
            client.update.assert_not_called()
            client.delete.assert_not_called()

    def test_synchronize_retries_recovery_after_a_month_lookup_fails(self):
        with TemporaryDirectory() as folder, patch("sync.datetime") as clock, \
                patch("sync.read_rows") as read_rows, patch("sync.SoopClient") as make_client:
            config_path = Path(folder) / "config.ini"
            state_path = Path(folder) / "sync.db"
            clock.now.return_value = datetime(2026, 11, 22, tzinfo=KST)
            read_rows.return_value = ([("1", "dkcal_mdays_20261123", "20:00 방송\n20:00 신규"),
                                       ("2", "dkcal_mdays_20261201", "20:00 합방")], [])
            client = make_client.return_value
            client.user = "example"
            november = [soop_event(1, "방송")]
            december = [soop_event(2, "합방", day="2026-12-01", calendar_type="2")]
            client.list_events.side_effect = [november, SoopError("목록 조회 실패")]
            client.create.return_value = 3

            with self.assertRaisesRegex(SoopError, "목록 조회 실패"):
                synchronize(config_path, state_path, lambda: False)
            client.create.assert_not_called()
            with closing(sqlite3.connect(state_path)) as db:
                self.assertEqual(db.execute("SELECT remote_id FROM synced").fetchall(), [])

            client.list_events.side_effect = [november, december, november]
            synchronize(config_path, state_path, lambda: False)
            client.create.assert_called_once_with(Event("cell:1", "2026-11-23", "신규", "20:00"))

    def test_synchronize_preserves_links_when_recovering_pending_create(self):
        with TemporaryDirectory() as folder, patch("sync.datetime") as clock, \
                patch("sync.read_rows") as read_rows, patch("sync.SoopClient") as make_client:
            config_path = Path(folder) / "config.ini"
            state_path = Path(folder) / "sync.db"
            clock.now.return_value = datetime(2026, 11, 22, tzinfo=KST)
            read_rows.return_value = ([("1", "dkcal_mdays_20261123", "20:00 방송\n20:00 합방")], [])
            client = make_client.return_value
            client.user = "example"
            client.list_events.return_value = []
            client.create.side_effect = [1, SoopError("등록 응답 없음", ambiguous=True)]

            with self.assertRaisesRegex(SoopError, "등록 응답 없음"):
                synchronize(config_path, state_path, lambda: False)
            read_rows.return_value = ([("1", "dkcal_mdays_20261123", "20:00 방송\n20:00 합방"),
                                       ("2", "dkcal_mdays_20261123", "20:00 방송")], [])
            client.reset_mock(side_effect=True)
            client.list_events.return_value = [soop_event(1, "방송"), soop_event(2, "합방", calendar_type=2),
                                               soop_event(3, "합방", calendar_type=3)]
            client.create.return_value = 4

            synchronize(config_path, state_path, lambda: False)

            client.create.assert_called_once_with(Event("cell:2", "2026-11-23", "방송", "20:00"))
            with closing(sqlite3.connect(state_path)) as db:
                self.assertEqual(db.execute("SELECT remote_id, source, calendar_type FROM synced ORDER BY remote_id").fetchall(),
                                 [(1, "cell:1", 1), (2, "cell:1", 2), (4, "cell:2", 1)])
                self.assertEqual(db.execute("SELECT user FROM pending").fetchall(), [])

    def test_synchronize_applies_configured_default_time_to_untimed_events(self):
        with TemporaryDirectory() as folder, patch("sync.datetime") as clock, \
                patch("sync.read_rows") as read_rows, patch("sync.SoopClient") as make_client:
            config_path = Path(folder) / "config.ini"
            config_path.write_text("[sync]\ndefault_time = 09:30\n", encoding="utf-8")
            clock.now.return_value = datetime(2026, 11, 22, tzinfo=KST)
            read_rows.return_value = ([("1", "dkcal_mdays_20261122", "방송\n18:00 합방")],
                                      [("2", "", "2026-11-22", "", "", "반복", "FREQ=DAILY;COUNT=1")])
            client = make_client.return_value
            client.user = "example"
            client.list_events.return_value = []
            client.create.side_effect = [1, 2, 3]

            synchronize(config_path, Path(folder) / "sync.db", lambda: False)

            self.assertEqual(client.create.call_args_list, [call(Event("cell:1", "2026-11-22", "방송", "09:30")),
                                                            call(Event("cell:1", "2026-11-22", "합방", "18:00", 2)),
                                                            call(Event("event:2", "2026-11-22", "반복", "09:30"))])

    def test_rolling_and_unsupported_recurrence(self):
        row = ("5", "", "2026-10-01", "", "", "반복", "RRULE:FREQ=DAILY")
        events, rolling = build_events([], [row], date(2026, 11, 15))
        self.assertEqual(len(events), 90)
        self.assertEqual(events[0].day, "2026-11-15")
        self.assertEqual(rolling, {"event:5"})
        with self.assertRaises(ValueError):
            build_events([], [(*row[:-1], "unknown")], date(2026, 11, 15))

    def test_missing_ini_created_with_defaults(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "config.ini"
            load_config(path)

            config = ConfigParser(interpolation=None)
            self.assertEqual(config.read(path, encoding="utf-8-sig"), [str(path)])
            self.assertEqual(dict(config["soop"]), {"username": "", "password": ""})
            self.assertEqual(config["sync"].getint("interval_minutes"), 10)
            self.assertEqual(config["sync"]["default_time"], "12:00")
            self.assertEqual(config["session"]["auth"], "")

    def test_ini_password_used_directly_and_preserved_when_saving_session(self):
        password = "dpapi:example%#;=@!*"
        with TemporaryDirectory() as folder:
            path = Path(folder) / "config.ini"
            path.write_text(f"[soop]\nusername = example\npassword = {password}\n", encoding="utf-8")
            client = SoopClient(path)

            def request(method, _address, **kwargs):
                if method == "GET":
                    return SimpleNamespace(status_code=200, url="https://login.sooplive.com/")
                self.assertEqual(kwargs["data"]["szPassword"], password)
                client.session.cookies.set("AuthTicket", "example", domain=".sooplive.com")
                return SimpleNamespace(status_code=200, content=b'{"RESULT": 1}')

            with client.session, patch.object(client, "_request", side_effect=request), \
                    patch("soop_client.seal", return_value="dpapi:encrypted-session"):
                client._login()
            saved = load_config(path)
            self.assertEqual(saved["soop"]["password"], password)
            self.assertEqual(saved["session"]["auth"], "dpapi:encrypted-session")


if __name__ == "__main__":
    unittest.main()
