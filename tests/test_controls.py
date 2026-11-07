"""Operator actions use fixed records and loopback HTTP, never Rex/providers."""
import base64
import copy
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

import dashboard
import factory


class ControlRecord(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "fixture.json"
        factory.save(self.path, factory.new_task("fixture"))

    def hold(self, reason, request_id, revision):
        return factory.hold_task(self.path, reason, request_id=request_id,
                                 expected_revision=revision, clock=lambda: 1000)


class TaskControls(ControlRecord):
    def test_replay_after_an_intervening_resume_preserves_the_later_decision(self):
        paused = self.hold("Operator decision", "pause-one", 0)
        self.assertEqual(paused["hold"], "Operator decision")
        self.hold(None, "resume-one", 1)
        before = self.path.read_bytes()
        replay = self.hold("Operator decision", "pause-one", 0)
        self.assertIsNone(replay["hold"])
        self.assertEqual(len(replay["control_events"]), 2)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(replay["control_events"][0], dict(id="pause-one", expected_revision=0,
            previous_hold=None, hold="Operator decision", at=1000))

    def test_stale_forms_and_reused_ids_cannot_overwrite_newer_holds(self):
        self.hold("Original", "pause-one", 0)
        before = self.path.read_bytes()
        for reason, identity, revision in ((None, "stale", 0), ("Changed", "pause-one", 0),
                                            ("Original", "pause-one", None)):
            with self.subTest(identity=identity), self.assertRaises(factory.ControlConflict):
                self.hold(reason, identity, revision)
            self.assertEqual(self.path.read_bytes(), before)

    def test_lost_save_response_is_reconciled_by_the_same_request_id(self):
        original = factory.save
        def lost_reply(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("Reply lost after atomic replace")
        with patch.object(factory, "save", side_effect=lost_reply), self.assertRaises(OSError):
            self.hold("Keep this decision", "pause-one", 0)
        restored = self.hold("Keep this decision", "pause-one", 0)
        self.assertEqual(len(restored["control_events"]), 1)
        self.assertEqual(restored["hold"], "Keep this decision")

    def test_invalid_control_inputs_leave_the_record_unchanged(self):
        before = self.path.read_bytes()
        for reason, identity, revision in ((" ", "valid", 0), (False, "valid", 0),
                (None, "../other", 0), (None, "", 0), (None, "valid", True), (None, "valid", -1)):
            with self.subTest(identity=identity), self.assertRaises(factory.FactoryError):
                self.hold(reason, identity, revision)
            self.assertEqual(self.path.read_bytes(), before)
        for value in (float("nan"), float("inf"), -1, 10 ** 400):
            with self.subTest(clock=value), self.assertRaises(factory.FactoryError):
                factory.hold_task(self.path, None, clock=lambda: value)
            self.assertEqual(self.path.read_bytes(), before)

    def test_version_four_migrates_without_inventing_control_history(self):
        legacy = factory.new_task("fixture")
        legacy.update(schema_version=4, hold="Earlier operator hold")
        del legacy["control_events"]
        del legacy["allowance_events"]
        factory.save(self.path, legacy)
        migrated = factory.load_task(self.path)
        self.assertEqual(migrated["schema_version"], 7)
        self.assertEqual(migrated["control_events"], [])
        self.assertEqual(migrated["hold"], legacy["hold"])
        self.assertEqual(migrated["assignments"], legacy["assignments"])
        resumed = self.hold(None, "resume-one", 0)
        self.assertEqual(resumed["control_events"][0]["previous_hold"], legacy["hold"])

    def test_damaged_or_downgraded_history_refuses_without_rewriting(self):
        original = self.hold("Keep", "pause-one", 0)
        variants = []
        for update in ({"hold": None}, {"schema_version": 4}, {"schema_version": 6},
                       {"control_events": None}):
            variants.append({**copy.deepcopy(original), **update})
        for change in (dict(id=True), dict(expected_revision=1), dict(at=float("inf")),
                       dict(hold=" "), dict(extra=True)):
            item = copy.deepcopy(original)
            item["control_events"][0].update(change)
            variants.append(item)
        item = copy.deepcopy(original)
        item["control_events"].append(copy.deepcopy(item["control_events"][0]))
        variants.append(item)
        for item in variants:
            with self.subTest(item=item):
                factory.save(self.path, item)
                before = self.path.read_bytes()
                with self.assertRaises(factory.FactoryError):
                    factory.load_task(self.path)
                self.assertEqual(self.path.read_bytes(), before)

    def test_blank_holds_refuse_without_migration_or_a_false_unpaused_view(self):
        for version in (4, 5):
            for reason in ("", " \t"):
                with self.subTest(version=version, reason=reason):
                    record = factory.new_task("fixture")
                    record.update(schema_version=version, hold=reason)
                    del record["allowance_events"]
                    if version == 4:
                        del record["control_events"]
                    factory.save(self.path, record)
                    before = self.path.read_bytes()
                    with self.assertRaisesRegex(factory.FactoryError, "Invalid task hold"):
                        factory.load_task(self.path)
                    view = dashboard.snapshot(self.root, clock=lambda: 1000, timer=lambda: 10)
                    self.assertEqual(view["tasks"], [])
                    self.assertIn("Invalid task hold", view["errors"][0]["error"])
                    self.assertEqual(self.path.read_bytes(), before)


class HTTPControls(ControlRecord):
    def setUp(self):
        super().setUp()
        self.key = "fixture-access"
        self.auth = "Basic " + base64.b64encode(("operator:" + self.key).encode()).decode()
        for name, value in (("token_urlsafe", "fixed-csrf"), ("token_hex", "render-request")):
            patcher = patch.object(dashboard.secrets, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        original = factory.hold_task
        def fixed_clock(*args, **kwargs):
            kwargs.setdefault("clock", lambda: 1000)
            return original(*args, **kwargs)
        patcher = patch.object(factory, "hold_task", side_effect=fixed_clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = dashboard.make_server(self.root, port=0, access_key=self.key)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path="/tasks/fixture/hold", *, method="POST", fields=None, body=None, headers=None):
        fields = fields if fields is not None else dict(csrf="fixed-csrf", request_id="pause-one",
            expected_revision="0", action="pause", reason="Review current scope")
        content = (urlencode(fields) if body is None else body) if method == "POST" else None
        extra = {"Authorization": self.auth, "Content-Type": "application/x-www-form-urlencoded"}
        extra.update(headers or {})
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=content, headers=extra)
            reply = connection.getresponse()
            return reply.status, dict(reply.getheaders()), reply.read().decode()
        finally:
            connection.close()

    def test_http_form_redirect_trace_resume_and_old_replay(self):
        status, _, page = self.request("/", method="GET")
        self.assertEqual(status, 200)
        self.assertIn('for="reason-fixture"', page)
        self.assertIn('name="csrf" value="fixed-csrf"', page)
        self.assertIn("Pause new assignments", page)
        status, headers, _ = self.request()
        self.assertEqual((status, headers["Location"]), (303, "/#task-fixture"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        before = self.path.read_bytes()
        page = self.request("/", method="GET")[2]
        self.assertIn("Paused: Review current scope", page)
        self.assertIn("Resume new assignments", page)
        self.assertIn("Request pause-one", page)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.request(fields=dict(csrf="fixed-csrf", request_id="resume-one",
            expected_revision="1", action="resume", reason=""))[0], 303)
        before = self.path.read_bytes()
        self.assertEqual(self.request()[0], 303)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(factory.load_task(self.path)["hold"])

    def test_web_stale_form_and_duplicate_id_return_conflict(self):
        self.assertEqual(self.request()[0], 303)
        before = self.path.read_bytes()
        for fields in (dict(csrf="fixed-csrf", request_id="new-request", expected_revision="0", action="resume", reason=""),
                       dict(csrf="fixed-csrf", request_id="pause-one", expected_revision="0", action="pause", reason="Changed")):
            self.assertEqual(self.request(fields=fields)[0], 409)
            self.assertEqual(self.path.read_bytes(), before)

    def test_unauthorized_foreign_origin_and_bad_csrf_never_access_state(self):
        before = self.path.read_bytes()
        with patch.object(factory, "task_lock", side_effect=AssertionError("Do not access task state")):
            for headers, code in (({"Authorization": ""}, 401), ({"Host": "foreign.example"}, 403),
                                  ({"Origin": "https://foreign.example"}, 403)):
                self.assertEqual(self.request(headers=headers)[0], code)
            self.assertEqual(self.request(fields=dict(csrf="incorrect", request_id="pause-one",
                expected_revision="0", action="pause", reason="No"))[0], 403)
        self.assertEqual(self.path.read_bytes(), before)

    def test_malformed_and_oversize_forms_leave_holds_unchanged(self):
        before = self.path.read_bytes()
        for body in ("csrf=fixed-csrf", "csrf=fixed-csrf&csrf=duplicate", "x=" + "a" * 8192,
                     "csrf=%FF", "csrf=fixed-csrf&request_id=pause-one&expected_revision=0&action=pause&reason="):
            self.assertEqual(self.request(body=body)[0], 400)
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.request(path="/tasks/../fixture/hold")[0], 404)
        self.assertEqual(self.request(headers={"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.path.read_bytes(), before)

    def test_busy_or_damaged_task_is_unavailable_without_resetting_it(self):
        with factory.task_lock(self.root, "fixture"):
            self.assertEqual(self.request()[0], 503)
        self.path.write_text("{damaged")
        self.assertEqual(self.request()[0], 503)
        self.assertEqual(self.path.read_text(), "{damaged")


if __name__ == "__main__":
    unittest.main()
