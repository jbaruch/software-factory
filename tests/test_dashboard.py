"""View tests use fixed task records and fake observations; never live Rex or providers."""
import copy
import base64
from contextlib import redirect_stderr, redirect_stdout
from http.client import HTTPConnection
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch

import dashboard
import factory


class OperatorView(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        inputs = dict(candidate="a" * 40, requirements="requirements-1")
        request = dict(role="backend", goal="Fix the supplied behavior", cwd=str(self.root),
                       command=[sys.executable, "private-command-argument"], instructions_sha256="a" * 64,
                       purpose="correction", inputs=dict(inputs))
        self.record: dict = dict(schema_version=2, task="fixture", admission=dict(goal="Deliver the agreed change",
            repository=str(self.root), initial_candidate="a" * 40, acceptance=["requirements.md"],
            limits=dict(correction=1, investigation=1)), inputs=dict(inputs), hold="Waiting for scope decision",
            rex=dict(binary=sys.executable, server="unix:///fixture.sock", host_id="fixture-host"),
            session_label="factory-fixture-00000000", session_id="fixture-session", assignments={"backend": dict(
                request=request, state="launched", brief=str(self.root / "brief.md"), window_label="backend-backend-00000000",
                window_id="fixture-window", block_id="fixture-block", result=dict(inputs=dict(inputs),
                    candidate="a" * 40, outcome="completed", artifacts=["evidence/original.txt"]))})
        self.path = self.root / "fixture.json"
        factory.save(self.path, self.record)

    def snapshot(self, **kwargs):
        return dashboard.snapshot(self.root, clock=lambda: 1000, timer=lambda: 10, **kwargs)

    def test_stored_view_derives_allowances_and_current_results_without_connecting(self):
        connect = Mock(side_effect=AssertionError("No live connection expected"))
        before = self.path.read_bytes()
        view = self.snapshot(connect=connect)
        task = view["tasks"][0]
        self.assertEqual(view["observed_at"], 1000)
        self.assertEqual(task["allowances"]["correction"]["remaining"], 0)
        self.assertTrue(task["assignments"][0]["result_current"])
        self.assertEqual(task["assignments"][0]["process"]["state"], "not_checked")
        self.assertEqual(task["hold"], "Waiting for scope decision")
        self.assertNotIn("private-command-argument", json.dumps(view))
        self.assertEqual(self.path.read_bytes(), before)
        connect.assert_not_called()

    def test_live_observation_never_changes_a_report_or_task(self):
        rex = Mock()
        rex.call.return_value = dict(child=dict(pid=123))
        before = self.path.read_bytes()
        task = self.snapshot(live=True, connect=lambda record: rex)["tasks"][0]
        self.assertEqual(task["assignments"][0]["process"]["state"], "running")
        self.assertEqual(task["assignments"][0]["result"]["outcome"], "completed")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(all(call.args[0:2] == ("block", "call") for call in rex.call.call_args_list))

    def test_live_failure_is_unavailable_and_stale_result_stays_visible(self):
        changed = copy.deepcopy(self.record)
        changed["inputs"]["requirements"] = "requirements-2"
        factory.save(self.path, changed)
        view = self.snapshot(live=True, connect=Mock(side_effect=factory.FactoryError("Inspect the pinned host")))
        assignment = view["tasks"][0]["assignments"][0]
        self.assertEqual(assignment["process"]["state"], "unavailable")
        self.assertIn("Inspect", assignment["process"]["error"])
        self.assertFalse(assignment["result_current"])
        self.assertEqual(assignment["result"]["artifacts"], ["evidence/original.txt"])

    def test_live_connection_and_process_timeouts_stay_unavailable_without_mutation(self):
        before = self.path.read_bytes()
        rex = Mock()
        rex.call.side_effect = subprocess.TimeoutExpired("fixture Rex process", 20)
        for connect in (Mock(side_effect=subprocess.TimeoutExpired("fixture Rex connect", 20)),
                        Mock(return_value=rex)):
            task = self.snapshot(live=True, connect=connect)["tasks"][0]
            assignment = task["assignments"][0]
            self.assertEqual(assignment["process"]["state"], "unavailable")
            self.assertIn("Inspect the", assignment["process"]["error"])
            self.assertEqual(assignment["result"]["outcome"], "completed")
            self.assertEqual(self.path.read_bytes(), before)

    def test_unreconciled_launch_is_not_guessed_or_relaunched(self):
        self.record["assignments"]["backend"].update(state="launching", result=None)
        del self.record["assignments"]["backend"]["block_id"]
        del self.record["assignments"]["backend"]["window_id"]
        factory.save(self.path, self.record)
        rex = Mock()
        assignment = self.snapshot(live=True, connect=lambda record: rex)["tasks"][0]["assignments"][0]
        self.assertEqual(assignment["process"]["state"], "unreconciled")
        rex.call.assert_not_called()

    def test_damaged_task_is_visible_as_an_error_and_evidence_files_are_not_tasks(self):
        (self.root / "damaged.json").write_text("{")
        (self.root / "fixture.backend.codex.json").write_text("{")
        view = self.snapshot()
        self.assertEqual([task["id"] for task in view["tasks"]], ["fixture"])
        self.assertEqual([item["task"] for item in view["errors"]], ["damaged"])
        self.assertIn("restore", view["errors"][0]["error"])

    def test_render_escapes_task_data_and_keeps_unknown_capacity_visible(self):
        self.record["admission"]["goal"] = '<script>alert("fixture")</script>'
        factory.save(self.path, self.record)
        page = dashboard.render(self.snapshot())
        self.assertNotIn('<script>alert', page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("Not checked", page)
        self.assertIn("Unknown", page)
        self.assertIn("do not accept a task", page)

    def test_http_serves_only_views_and_rejects_foreign_host_and_file_paths(self):
        key = "not-a-real-key"
        authorization = "Basic " + base64.b64encode(("operator:" + key).encode()).decode()
        server = dashboard.make_server(self.root, port=0, access_key=key)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(cleanup)
        def request(path, *, host=None, method="GET", auth: str | None = authorization):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                headers = {"Host": host} if host else {}
                if auth is not None:
                    headers["Authorization"] = auth
                connection.request(method, path, headers=headers)
                response = connection.getresponse()
                if response.status == 401:
                    self.assertTrue((response.getheader("WWW-Authenticate") or "").startswith("Basic "))
                return response.status, response.getheader("Cache-Control"), response.read().decode()
            finally:
                connection.close()
        status, cache, body = request("/api/state")
        self.assertEqual((status, cache), (200, "no-store"))
        self.assertEqual(json.loads(body)["tasks"][0]["id"], "fixture")
        with patch.object(dashboard, "snapshot", side_effect=AssertionError("Do not read private state")) as source:
            for auth in (None, "Basic !!!", "Bearer not-a-real-key", "Basic /w==",
                         "Basic " + base64.b64encode(b"operator:wrong-key").decode()):
                status, cache, body = request("/api/state?live=1", auth=auth)
                self.assertEqual((status, cache), (401, "no-store"))
                self.assertNotIn("Deliver the agreed change", body)
                self.assertNotIn(key, body)
            source.assert_not_called()
        self.assertEqual(request("/api/state", host="untrusted.example")[0], 403)
        self.assertEqual(request("/fixture.json")[0], 404)
        self.assertEqual(request("/?command=launch")[0], 400)
        self.assertEqual(request("/", method="POST")[0], 501)
        self.assertEqual(request("/")[0], 200)
        self.assertEqual(request("/dashboard.css")[0], 200)
        with patch.object(dashboard, "snapshot", side_effect=factory.FactoryError("Restore source records")), redirect_stderr(StringIO()):
            status, _, body = request("/api/state")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"], "Restore source records")

    def test_access_capability_file_is_private_and_expires_with_its_context(self):
        with dashboard.access_file("not-a-real-key", "http://127.0.0.1:8090") as path:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(json.loads(path.read_text()), dict(schema_version=1, username="operator",
                password="not-a-real-key", url="http://127.0.0.1:8090"))
        self.assertFalse(path.exists())

    def test_startup_announces_access_file_without_logging_capability(self):
        server = MagicMock()
        server.__enter__.return_value = server
        server.server_port = 8090
        server.serve_forever.side_effect = KeyboardInterrupt
        output = StringIO()
        with patch.object(dashboard, "make_server", return_value=server), \
             patch.object(dashboard.secrets, "token_urlsafe", return_value="not-a-real-key"), redirect_stdout(output):
            self.assertEqual(dashboard.main(["--state-dir", str(self.root)]), 0)
        announced = json.loads(output.getvalue())
        self.assertIn("access_file", announced)
        self.assertNotIn("not-a-real-key", output.getvalue())
        self.assertFalse(Path(announced["access_file"]).exists())

    def test_invalid_port_fails_before_creating_a_listener(self):
        for port in ("-1", "65536"):
            with self.subTest(port=port), patch.object(dashboard, "make_server") as start, redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    dashboard.main(["--port", port])
                self.assertEqual(stopped.exception.code, 2)
                start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
