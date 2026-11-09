"""View tests use fixed task records and fake observations; never live Rex or providers."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import copy
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.client import HTTPConnection
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.parse import urlencode

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
        self.record: dict = dict(schema_version=factory.new_task("fixture")["schema_version"], control_events=[], allowance_events=[], provisioning=None, task="fixture", admission=dict(goal="Deliver the agreed change",
            repository=str(self.root), initial_candidate="a" * 40, acceptance=["requirements.md"],
            repository_identity=dict(common_dir=str(self.root / ".git"), repository_id="a" * 32),
            limits=dict(correction=1, investigation=1)), inputs=dict(inputs), hold="Waiting for scope decision",
            rex=dict(binary=sys.executable, server="unix:///fixture.sock", host_id="fixture-host"),
            session_label="factory-fixture-00000000", session_id="fixture-session", assignments={"backend": dict(
                request=request, state="launched", brief=str(self.root / "brief.md"), window_label="backend-backend-00000000",
                window_id="fixture-window", block_id="fixture-block", native_collected=False, result=dict(inputs=dict(inputs),
                    candidate="a" * 40, outcome="completed", artifacts=["evidence/original.txt"]))})
        self.path = self.root / "fixture.json"
        factory.save(self.path, self.record)

    def snapshot(self, **kwargs):
        return dashboard.snapshot(self.root, clock=lambda: 1000, timer=lambda: 10, **kwargs)

    def native_command(self, harness="codex", *, historical=False):
        prefix = [sys.executable] + ([] if historical else ["-E", "-s"])
        command = prefix + [str(factory.ROOT / (harness + "_worker.py")), "--job", "fixture/backend",
            "--" + harness, "/fixture/native", "--model", "requested-model", "--effort", "high",
            "--seconds", "600", "--repo", "{repository}", "--cwd", "{cwd}", "--brief", "{brief}",
            "--brief-sha256", "{brief_sha256}", "--candidate", "{candidate}",
            "--requirements", "{requirements}", "--repo-identity", "{repository_identity}",
            "--worktree-git-dir", str(self.root / ".git"), "--events", str(self.root / "native.jsonl"),
            "--receipt", str(self.root / "native.json"), "--role", "backend"]
        if harness == "grok":
            command += ["--native-session", "00000000-0000-4000-8000-000000000000", "--turns", "30"]
        return command

    def fake_rex(self, *, process=None, output="Original <terminal> & output\n"):
        window = dict(window_id="fixture-window", label="backend-backend-00000000",
                      layers=[dict(blocks=[dict(block_id="fixture-block")])])
        session: dict = dict(session_id="fixture-session", label="factory-fixture-00000000", windows=[window])
        def present():
            # Like Rex, block reads fail once the pinned session or block is gone.
            if session["session_id"] != "fixture-session" or not any(
                    block["block_id"] == "fixture-block" for window in session["windows"]
                    for layer in window["layers"] for block in layer["blocks"]):
                raise factory.FactoryError('block target "fixture-block" not found')
        def call(*args, **kwargs):
            if args == ("whoami", "--json"):
                return dict(host_id="fixture-host", instance_id="fixture-instance")
            if args == ("session", "inspect", "fixture-session", "--json"):
                return session
            if args == ("ls", "--json"):
                return dict(sessions=[dict(session_id=session["session_id"], label=session["label"])])
            if args == ("block", "call", "-s", "fixture-session", "-b", "fixture-block", "process"):
                present()
                return process if process is not None else dict(child=dict(pid=123, argv="private-argv"))
            if args == ("capture", "-s", "fixture-session", "-b", "fixture-block", "--trim", "--unwrap") and kwargs == dict(raw=True):
                present()
                return output
            raise AssertionError("Unexpected Rex operation")
        return Mock(call=Mock(side_effect=call)), session

    def get_without_socket(self, path, *, auth=True, host="127.0.0.1:8090", method="GET", body=b"", headers=None, decision_store=None):
        # Exercise the actual HTTP handler without binding a socket in the native sandbox.
        with patch.object(dashboard, "ThreadingHTTPServer", return_value=Mock(server_port=8090)) as server:
            dashboard.make_server(self.root, access_key="fixture-key", decision_store=decision_store)
        handler_type = server.call_args.args[1]
        handler = handler_type.__new__(handler_type)
        handler.path = path
        handler.headers = {"Host": host}
        if auth:
            handler.headers["Authorization"] = "Basic " + base64.b64encode(b"operator:fixture-key").decode()
        handler.headers.update(headers or {})
        handler.wfile = BytesIO()
        handler.rfile = BytesIO(body)
        handler.send_response = Mock()
        handler.send_error = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        if method == "POST":
            handler.do_POST()
        else:
            handler.do_GET()
        return handler

    def test_known_native_requests_are_original_metadata_not_observed_models(self):
        for harness, historical in (("codex", False), ("codex", True), ("grok", False)):
            with self.subTest(harness=harness, historical=historical):
                self.record["assignments"]["backend"]["request"]["command"] = self.native_command(harness, historical=historical)
                factory.save(self.path, self.record)
                before = self.path.read_bytes()
                view = self.snapshot()
                assignment = view["tasks"][0]["assignments"][0]
                self.assertEqual(view["schema_version"], 4)
                self.assertEqual(assignment["requested_native"], dict(adapter=harness + "_worker", harness=harness,
                    route=harness + "-native", model="requested-model", effort="high"))
                self.assertIsNone(assignment["observed_model"])
                self.assertEqual(assignment["pane"], dict(host_id="fixture-host", session_id="fixture-session",
                    window_id="fixture-window", block_id="fixture-block"))
                self.assertEqual(assignment["pane_href"], "/tasks/fixture/assignments/backend/pane")
                page = dashboard.render(view)
                for label in ("Requested model", "Requested effort", "Requested subscription route", "Observed model: Unknown",
                              "runtime readiness", "Assignment source inputs", "Read owned Rex pane"):
                    self.assertIn(label, page)
                self.assertNotIn("rex://", page)
                self.assertNotIn("/fixture/native", page)
                self.assertEqual(self.path.read_bytes(), before)

    def application_checkouts(self):
        # Real local Git bindings, with inert worker files that must never execute.
        original = self.root.resolve() / "application"
        isolated = self.root.resolve() / "isolated"
        original.mkdir()
        environment = dict(factory.git_environment(), GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                           GIT_AUTHOR_DATE="2000-01-01T00:00:00+00:00", GIT_COMMITTER_DATE="2000-01-01T00:00:00+00:00")
        def git(root, *args):
            return subprocess.run([factory.git_binary(), "-C", str(root), "-c", "core.hooksPath=" + os.devnull,
                "-c", "commit.gpgsign=false", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                *args], env=environment, check=True, capture_output=True, text=True)
        git(original, "init")
        for harness in ("codex", "grok"):
            (original / (harness + "_worker.py")).write_text('raise AssertionError("Never execute another checkout")\n')
        (original / "config").mkdir()
        (original / "config/providers.json").write_bytes((factory.ROOT / "config/providers.json").read_bytes())
        git(original, "add", ".")
        git(original, "commit", "-m", "Fixed application fixture")
        git(original, "worktree", "add", "--detach", str(isolated))
        return original, isolated, git

    def test_original_owner_requests_bind_across_isolated_application_checkout(self):
        original, isolated, git = self.application_checkouts()
        project = self.root.resolve() / "unrelated-project"
        project.mkdir()
        git(project, "init")
        self.record["admission"]["repository"] = str(project)
        self.record["admission"]["repository_identity"]["common_dir"] = str(project / ".git")
        with patch.object(factory, "ROOT", isolated), \
             patch.object(factory, "repository_identity", side_effect=AssertionError("No marker access")):
            for harness, historical in (("codex", False), ("codex", True), ("grok", False)):
                with self.subTest(harness=harness, historical=historical):
                    command = self.native_command(harness, historical=historical)
                    command[1 if historical else 3] = str(original / (harness + "_worker.py"))
                    self.record["assignments"]["backend"]["request"]["command"] = command
                    factory.save(self.path, self.record)
                    before = self.path.read_bytes()
                    view = self.snapshot()
                    assignment = view["tasks"][0]["assignments"][0]
                    self.assertEqual(assignment["requested_native"], dict(adapter=harness + "_worker", harness=harness,
                        route=harness + "-native", model="requested-model", effort="high"))
                    self.assertIsNone(assignment["observed_model"])
                    page = dashboard.render(view)
                    self.assertIn("requested-model", page)
                    self.assertIn("Observed model: Unknown", page)
                    self.assertNotIn(str(original), page)
                    self.assertEqual(self.path.read_bytes(), before)
        for root in (original, isolated, project):
            self.assertFalse((factory.git_common_dir(root) / factory.REPOSITORY_MARKER).exists())

    def test_cross_checkout_binding_rejects_impostor_foreign_retired_and_damaged_paths(self):
        original, isolated, git = self.application_checkouts()
        foreign = self.root.resolve() / "foreign-clone"
        git(original, "clone", "--no-hardlinks", str(original), str(foreign))
        retired = self.root.resolve() / "retired"
        git(original, "worktree", "add", "--detach", str(retired))
        (retired / ".git").unlink()
        git(original, "worktree", "prune", "--expire", "now")
        damaged = self.root.resolve() / "damaged"
        git(original, "worktree", "add", "--detach", str(damaged))
        (damaged / ".git").write_text("gitdir: " + str(foreign / ".git") + "\n")
        impostor = original / "nested"
        impostor.mkdir()
        worker = original / "codex_worker.py"
        (impostor / worker.name).write_bytes(worker.read_bytes())
        alias = self.root.resolve() / "alias"
        alias.symlink_to(original, target_is_directory=True)
        with patch.object(factory, "ROOT", isolated):
            good = self.native_command()
            def unknown(command):
                self.record["assignments"]["backend"]["request"]["command"] = command
                result = dashboard.requested_native(self.record, "backend", self.record["assignments"]["backend"])
                self.assertTrue(all(value is None for value in result.values()), command[:4])
            for root in (foreign, retired, damaged, impostor, alias, self.root / "missing"):
                with self.subTest(root=root.name):
                    unknown([*good[:3], str(root / worker.name), *good[4:]])
            # A task's arbitrary project/repository labels cannot grant application ownership.
            self.record["admission"]["repository"] = str(foreign)
            self.record["admission"]["repository_identity"]["common_dir"] = str(foreign / ".git")
            unknown([*good[:3], str(foreign / worker.name), *good[4:]])
            owned = [*good[:3], str(worker), *good[4:]]
            for prefix in ([sys.executable, "-I"], ["/foreign/python", "-E", "-s"], [sys.executable, "-m"]):
                unknown([*prefix, str(worker), *good[4:]])
            for option, value in (("--job", "other/backend"), ("--role", "frontend"), ("--cwd", "/other"),
                                  ("--repo", "/other"), ("--brief", "/other"), ("--brief-sha256", "b" * 64),
                                  ("--candidate", "b" * 40), ("--requirements", "other"), ("--repo-identity", "{}")):
                command = owned.copy()
                command[command.index(option) + 1] = value
                unknown(command)
            # Exact known filenames still refuse missing, linked and non-file paths.
            saved = worker.read_bytes()
            worker.unlink()
            unknown(owned)
            worker.symlink_to(foreign / worker.name)
            unknown(owned)
            worker.unlink()
            worker.hardlink_to(foreign / worker.name)
            unknown(owned)
            worker.unlink()
            worker.mkdir()
            unknown(owned)
            worker.rmdir()
            worker.write_bytes(saved)
            with patch.object(factory, "git_run", side_effect=factory.FactoryError("Damaged Git metadata")):
                unknown(owned)
            # The damaged checkout is still registered, but belongs to a foreign Git directory.
            inventory = git(isolated, "worktree", "list", "--porcelain", "-z").stdout
            self.assertIn("worktree " + str(damaged) + "\0", inventory)

    def test_misleading_generic_unsupported_and_damaged_commands_stay_unknown(self):
        good = self.native_command()
        commands = [
            [sys.executable, "--model", "codex", "--effort", "high"],
            ["/fixture/codex", "exec", "--model", "requested-model"],
            [sys.executable, "-E", "-s", str(factory.ROOT / "claude_worker.py"), *good[4:]],
            [sys.executable, "-E", "-s", "/foreign/codex_worker.py", *good[4:]],
            [sys.executable, "-c", "codex_worker.py --model requested-model"],
            good[:-2], good + ["--model", "private-duplicate"], good + ["--unsupported", "private-argument"],
            good + ["--help"], good + ["-h"],
        ]
        # Removing role is supported for historical Codex; make that case damaged elsewhere.
        commands[5] = good[:good.index("--model")]
        for option, value in (("--role", "frontend"), ("--job", "foreign/backend"), ("--cwd", "/foreign"),
                              ("--candidate", "b" * 40), ("--brief-sha256", "b" * 64),
                              ("--repo-identity", "damaged"), ("--model", " "), ("--seconds", "0")):
            command = good.copy()
            command[command.index(option) + 1] = value
            commands.append(command)
        command = good.copy()
        command[command.index("--model")] = "--mod"
        commands.append(command)
        commands.append(good + ["--commit-message", "invalid without writer mode"])
        grok = self.native_command("grok")
        for option, value in (("--native-session", "damaged"), ("--turns", "0")):
            command = grok.copy()
            command[command.index(option) + 1] = value
            commands.append(command)
        with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
            for command in commands:
                self.record["assignments"]["backend"]["request"]["command"] = command
                factory.save(self.path, self.record)
                view = self.snapshot()
                self.assertTrue(all(value is None for value in view["tasks"][0]["assignments"][0]["requested_native"].values()))
                self.assertNotIn("private-argument", json.dumps(view))
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_legacy_unadmitted_record_keeps_unknown_identity_and_original_command(self):
        legacy = copy.deepcopy(self.record)
        request = legacy["assignments"]["backend"]["request"]
        request.pop("purpose")
        request.pop("inputs")
        original_command = request["command"].copy()
        legacy["assignments"]["backend"].pop("result")
        legacy["assignments"]["backend"].pop("native_collected")
        legacy["assignments"]["backend"].pop("rex_instance", None)
        for key in ("schema_version", "admission", "hold", "inputs", "control_events", "allowance_events", "provisioning"):
            legacy.pop(key)
        factory.save(self.path, legacy)
        assignment = self.snapshot()["tasks"][0]["assignments"][0]
        self.assertTrue(all(value is None for value in assignment["requested_native"].values()))
        self.assertIsNone(assignment["inputs_current"])
        self.assertIsNone(assignment["observed_model"])
        self.assertEqual(factory.load_task(self.path)["assignments"]["backend"]["request"]["command"], original_command)

    def test_process_observation_times_are_per_read_not_collection_or_report_times(self):
        rex, _ = self.fake_rex()
        ticks = iter((900, 1000))
        view = dashboard.snapshot(self.root, live=True, connect=lambda record: rex, clock=lambda: next(ticks))
        assignment = view["tasks"][0]["assignments"][0]
        self.assertEqual(assignment["process"]["observed_at"], 900)
        self.assertEqual(view["observed_at"], 1000)
        self.assertNotIn("private-argv", json.dumps(view))
        self.assertIn("1970-01-01 00:15:00 UTC", dashboard.render(view))
        self.assertIsNone(self.snapshot()["tasks"][0]["assignments"][0]["process"]["observed_at"])
        failure = self.snapshot(live=True, connect=Mock(side_effect=factory.FactoryError("private stderr argv credentials")))
        self.assertIsNone(failure["tasks"][0]["assignments"][0]["process"]["observed_at"])
        self.assertNotIn("private stderr", json.dumps(failure))

    def test_original_requested_identity_survives_stale_inputs_and_result(self):
        self.record["assignments"]["backend"]["request"]["command"] = self.native_command()
        self.record["inputs"]["requirements"] = "requirements-2"
        factory.save(self.path, self.record)
        view = self.snapshot()
        assignment = view["tasks"][0]["assignments"][0]
        self.assertEqual(assignment["requested_native"]["model"], "requested-model")
        self.assertFalse(assignment["inputs_current"])
        self.assertFalse(assignment["result_current"])
        page = dashboard.render(view)
        self.assertIn("Assignment source inputs (stale)", page)
        self.assertIn("Completed (stale)", page)
        self.assertIn("requirements-1", page)
        self.assertIn("requirements-2", page)

    def test_process_states_have_only_actual_observations_and_no_native_objects(self):
        for process, expected in (({}, "unknown"), (dict(child=dict(pid=1)), "running"),
                                  (dict(last_exit=dict(exit_code=0, argv="private")), "exited"),
                                  (dict(last_exit=dict(exit_code=1, stderr="private")), "failed")):
            rex, _ = self.fake_rex(process=process)
            assignment = self.snapshot(live=True, connect=lambda record: rex)["tasks"][0]["assignments"][0]
            self.assertEqual(assignment["process"]["state"], expected)
            self.assertEqual(assignment["process"]["observed_at"], 1000)
            self.assertNotIn("private", json.dumps(assignment))
        rex, _ = self.fake_rex(process=dict(last_exit=dict(exit_code="damaged")))
        assignment = self.snapshot(live=True, connect=lambda record: rex)["tasks"][0]["assignments"][0]
        self.assertEqual(assignment["process"]["state"], "unavailable")
        self.assertIsNone(assignment["process"]["observed_at"])

    def test_rex_restart_is_lost_not_a_running_restored_shell(self):
        self.record["assignments"]["backend"]["rex_instance"] = "launch-instance"
        factory.save(self.path, self.record)
        rex, _ = self.fake_rex(process=dict(child=dict(pid=1, path="/bin/zsh")))
        assignment = self.snapshot(live=True, connect=lambda record: rex)["tasks"][0]["assignments"][0]
        self.assertEqual(assignment["process"]["state"], "lost")
        self.assertIn("restarted", assignment["process"]["error"])
        self.assertIsNone(assignment["process"]["exit_code"])
        self.assertIn("Lost", dashboard.render(self.snapshot(live=True, connect=lambda record: rex)))

    def close_tab(self, *, capture=True):
        path = self.root / "fixture.backend.capture.txt"
        if capture:
            factory.save(path, "Retained <terminal> output\n")
        self.record["assignments"]["backend"]["closed"] = dict(
            state="exited" if capture else "lost", exit=dict(exit_code=0) if capture else None,
            reason=None if capture else "Rex server restarted after launch", session_id="fixture-session",
            capture=str(path) if capture else None, at=900)
        factory.save(self.path, self.record)

    def test_tidied_tab_shows_retained_output_and_status_without_rex(self):
        self.close_tab()
        with patch.object(factory, "Rex", side_effect=AssertionError("A tidied tab needs no Rex")):
            detail = dashboard.pane_detail(self.root, "fixture", "backend")
        self.assertIsNone(detail["error"])
        self.assertEqual(detail["output"], "Retained <terminal> output\n")
        self.assertEqual(detail["captured_at"], 900)
        self.assertEqual((detail["process"]["state"], detail["process"]["closed"]), ("exited", True))
        unavailable = Mock(call=Mock(side_effect=AssertionError("A tidied tab needs no Rex")))
        view = self.snapshot(live=True, connect=lambda record: unavailable)
        self.assertEqual(view["tasks"][0]["assignments"][0]["process"]["state"], "exited")
        self.assertIn("Exited successfully (tab closed)", dashboard.render(view))

    def test_tidied_tab_keeps_the_session_it_lived_in(self):
        self.close_tab()
        self.record.update(session_id=None, session_label="factory-fixture-11111111")
        factory.save(self.path, self.record)
        detail = dashboard.pane_detail(self.root, "fixture", "backend")
        self.assertEqual(detail["pane"]["session_id"], "fixture-session")
        self.assertEqual(detail["output"], "Retained <terminal> output\n")

    def test_tidied_tab_without_output_explains_why(self):
        self.close_tab(capture=False)
        detail = dashboard.pane_detail(self.root, "fixture", "backend")
        self.assertEqual(detail["process"]["state"], "lost")
        self.assertIn("without retained output: Rex server restarted", detail["error"])
        self.assertIsNone(detail["output"])

    def test_render_accepts_old_static_snapshots_and_escapes_new_fields(self):
        view = self.snapshot()
        assignment = view["tasks"][0]["assignments"][0]
        for key in ("requested_native", "observed_model", "pane", "pane_href"):
            assignment.pop(key)
        assignment["process"].pop("observed_at")
        view["schema_version"] = 2
        page = dashboard.render(view)
        self.assertIn("Unknown", page)
        self.assertNotIn("method=\"post\"", page)
        self.assertNotIn("Read owned Rex pane", page)
        assignment.update(requested_native=dict(model='<script>" &'), pane=dict(host_id='<host>" &',
            session_id="s", window_id="w", block_id="b"), id='evil/"<script>', pane_href="https://foreign/")
        page = dashboard.render(view)
        self.assertIn("&lt;host&gt;&quot; &amp;", page)
        self.assertIn("&lt;script&gt;&quot; &amp;", page)
        self.assertNotIn('href="https://foreign', page)
        self.assertNotIn("Read owned Rex pane", page)

    def test_owned_pane_read_revalidates_exact_scope_and_refresh_only_reads(self):
        rex, _ = self.fake_rex()
        before = self.path.read_bytes()
        with patch.object(factory, "Rex", return_value=rex) as transport:
            for _ in range(2):
                detail = dashboard.pane_detail(self.root, "fixture", "backend", clock=lambda: 900)
                self.assertIsNone(detail["error"])
                self.assertEqual(detail["process"]["observed_at"], 900)
                page = dashboard.render_pane(detail)
                self.assertIn("Original &lt;terminal&gt; &amp; output", page)
                self.assertIn("fixture-window", page)
                self.assertIn("1970-01-01 00:15:00 UTC", page)
                self.assertIn("All text returned by this capture is shown", page)
        transport.assert_called_with(sys.executable, "unix:///fixture.sock")
        self.assertEqual(rex.call.call_count, 8)
        self.assertEqual(self.path.read_bytes(), before)

    @contextmanager
    def pending_pane(self, operation):
        entered, release = threading.Event(), threading.Event()
        rex, _ = self.fake_rex()
        original = rex.call.side_effect
        def call(*args, **kwargs):
            if args[0] == operation:
                entered.set()
                if not release.wait(timeout=5):
                    raise AssertionError("Test did not release fake Rex")
            return original(*args, **kwargs)
        rex.call.side_effect = call
        with patch.object(factory, "Rex", return_value=rex), ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(dashboard.pane_detail, self.root, "fixture", "backend", clock=lambda: 900)
            try:
                self.assertTrue(entered.wait(timeout=5), "Pane read did not reach fake Rex")
                yield pending
            finally:
                release.set()

    def test_stalled_capture_allows_valid_operator_hold_and_preserves_it(self):
        self.record["hold"] = None
        factory.save(self.path, self.record)
        original = factory.hold_task
        def fixed_clock(*args, **kwargs):
            return original(*args, **kwargs, clock=lambda: 800)
        with self.pending_pane("capture") as pending:
            with patch.object(dashboard.secrets, "token_urlsafe", return_value="fixed-csrf"), \
                 patch.object(factory, "hold_task", side_effect=fixed_clock):
                page = self.get_without_socket("/").wfile.getvalue().decode()
                self.assertIn('name="csrf" value="fixed-csrf"', page)
                body = urlencode(dict(csrf="fixed-csrf", request_id="pause-during-capture", expected_revision="0",
                                      action="pause", reason="Operator hold during capture")).encode()
                handler = self.get_without_socket("/tasks/fixture/hold", method="POST", body=body,
                    headers={"Origin": "http://127.0.0.1:8090", "Content-Length": str(len(body)),
                             "Content-Type": "application/x-www-form-urlencoded"})
                handler.send_error.assert_not_called()
                handler.send_response.assert_called_with(303)
            # The same lock used by launchers is available while capture still waits.
            with factory.task_lock(self.root, "fixture") as path:
                saved = factory.load_task(path)
                self.assertEqual(saved["hold"], "Operator hold during capture")
                self.assertEqual(saved["control_events"], [dict(id="pause-during-capture", expected_revision=0,
                    previous_hold=None, hold="Operator hold during capture", at=800)])
                held_bytes = path.read_bytes()
            self.assertFalse(pending.done())
        detail = pending.result(timeout=5)
        self.assertIsNone(detail["error"])
        self.assertEqual(detail["output"], "Original <terminal> & output\n")
        self.assertEqual(self.path.read_bytes(), held_bytes)
        self.assertIn("Operator hold during capture", dashboard.render(self.snapshot()))

    def test_saved_scope_changes_never_retarget_pending_ownership_checks_or_reads(self):
        for operation in ("whoami", "session", "block", "capture"):
            with self.subTest(operation=operation):
                factory.save(self.path, self.record)
                with self.pending_pane(operation) as pending:
                    with factory.task_lock(self.root, "fixture") as path:
                        changed = factory.load_task(path)
                        changed["rex"].update(host_id="replacement-host", server="unix:///replacement.sock")
                        changed.update(session_id="replacement-session", session_label="factory-fixture-11111111")
                        changed["inputs"]["candidate"] = "b" * 40
                        changed["assignments"]["backend"].update(window_id="replacement-window", block_id="replacement-block",
                            window_label="backend-backend-11111111")
                        factory.save(path, changed)
                        self.assertEqual(factory.load_task(path), changed)
                        changed_bytes = path.read_bytes()
                    self.assertFalse(pending.done())
                detail = pending.result(timeout=5)
                self.assertIsNone(detail["error"])
                self.assertEqual(detail["pane"], dict(host_id="fixture-host", session_id="fixture-session",
                    window_id="fixture-window", block_id="fixture-block"))
                self.assertEqual(detail["process"], dict(state="running", observed_at=900, exit_code=None))
                self.assertEqual(detail["output"], "Original <terminal> & output\n")
                self.assertNotIn("replacement", dashboard.render_pane(detail))
                self.assertEqual(self.path.read_bytes(), changed_bytes)

    def test_pane_missing_damaged_and_launching_records_never_connect(self):
        connect = Mock(side_effect=AssertionError("No transport for missing identity"))
        for task, assignment in (("missing", "backend"), ("fixture", "missing"), ("../fixture", "backend")):
            detail = dashboard.pane_detail(self.root, task, assignment, connect=connect)
            self.assertIsNotNone(detail["error"])
            self.assertIsNone(detail["output"])
        self.record["assignments"]["backend"].update(state="launching", result=None)
        for key in ("window_id", "block_id"):
            self.record["assignments"]["backend"].pop(key)
        factory.save(self.path, self.record)
        detail = dashboard.pane_detail(self.root, "fixture", "backend", connect=connect)
        self.assertIn("reconciliation", detail["error"])
        self.record["assignments"]["backend"]["state"] = "launched"
        factory.save(self.path, self.record)
        self.assertIsNotNone(dashboard.pane_detail(self.root, "fixture", "backend", connect=connect)["error"])
        self.path.write_text("{")
        self.assertIsNotNone(dashboard.pane_detail(self.root, "fixture", "backend", connect=connect)["error"])
        connect.assert_not_called()

    def test_pane_refuses_foreign_host_and_session_identity(self):
        for mismatch in ("host", "session-label"):
            rex, session = self.fake_rex()
            if mismatch == "host":
                original = rex.call.side_effect
                rex.call.side_effect = lambda *a, **k: dict(host_id="foreign") if a[0] == "whoami" else original(*a, **k)
            else:
                session["label"] = "foreign"
            with self.subTest(mismatch=mismatch), patch.object(factory, "Rex", return_value=rex):
                detail = dashboard.pane_detail(self.root, "fixture", "backend")
                self.assertIsNotNone(detail["error"])
                self.assertIsNone(detail["output"])
                self.assertTrue(all(call.args[0] in ("whoami", "session", "ls") for call in rex.call.call_args_list))

    def test_pane_follows_renamed_or_moved_tabs_and_never_captures_a_closed_one(self):
        for change in ("window", "window-label"):
            rex, session = self.fake_rex()
            session["windows"][0]["window_id" if change == "window" else "label"] = "operator-" + change
            with self.subTest(change=change), patch.object(factory, "Rex", return_value=rex):
                detail = dashboard.pane_detail(self.root, "fixture", "backend")
                self.assertIsNone(detail["error"])
                self.assertEqual(detail["output"], "Original <terminal> & output\n")
        for change in ("session", "block", "missing-window"):
            rex, session = self.fake_rex()
            if change == "session":
                session["session_id"] = "foreign"
            elif change == "block":
                session["windows"][0]["layers"][0]["blocks"][0]["block_id"] = "foreign"
            else:
                session["windows"] = []
            with self.subTest(change=change), patch.object(factory, "Rex", return_value=rex):
                detail = dashboard.pane_detail(self.root, "fixture", "backend")
                self.assertEqual(detail["process"]["state"], "lost")
                self.assertIn("closed outside the factory", detail["error"])
                self.assertIsNone(detail["output"])
                self.assertFalse(any(call.args[0] == "capture" for call in rex.call.call_args_list))

    def test_pane_unavailable_transport_and_capture_never_expose_errors(self):
        for failure in (factory.FactoryError("private stderr credentials argv"), OSError("private path"),
                        subprocess.TimeoutExpired("private argv", 20)):
            for fail_capture in (False, True):
                rex, _ = self.fake_rex()
                original = rex.call.side_effect
                def call(*args, **kwargs):
                    if not fail_capture or args[0] == "capture":
                        raise failure
                    return original(*args, **kwargs)
                rex.call.side_effect = call
                with patch.object(factory, "Rex", return_value=rex):
                    detail = dashboard.pane_detail(self.root, "fixture", "backend", clock=lambda: 900)
                self.assertIsNotNone(detail["error"])
                self.assertIsNone(detail["output"])
                self.assertNotIn("private", dashboard.render_pane(detail))
                self.assertEqual(detail["process"]["observed_at"], 900 if fail_capture else None)

    def test_pane_partial_output_is_explicit_and_unknown_process_is_not_success(self):
        output = "omitted prefix\n" + '<script>&"' + "x" * dashboard.PANE_LIMIT
        rex, _ = self.fake_rex(process={}, output=output)
        with patch.object(factory, "Rex", return_value=rex):
            detail = dashboard.pane_detail(self.root, "fixture", "backend", clock=lambda: 900)
        self.assertTrue(detail["partial"])
        self.assertEqual(len(detail["output"]), dashboard.PANE_LIMIT)
        self.assertEqual(detail["process"]["state"], "unknown")
        self.assertEqual(detail["process"]["observed_at"], 900)
        page = dashboard.render_pane(detail)
        self.assertIn("Partial view", page)
        self.assertIn("fixture-block", page)
        self.assertNotIn("omitted prefix", page)
        self.assertNotIn("Exited successfully", page)

    def test_pane_http_authentication_and_strict_routes_precede_any_state_access(self):
        with patch.object(factory, "load_task", side_effect=AssertionError("No state before auth/route checks")) as load:
            for path in ("/", "/api/state?live=1", "/dashboard.css", "/tasks/fixture/assignments/backend/pane"):
                handler = self.get_without_socket(path, auth=False)
                handler.send_response.assert_called_with(401)
                handler = self.get_without_socket(path, host="foreign:8090")
                handler.send_error.assert_called_with(403, "Use this dashboard's loopback address")
            for path in ("/tasks/fixture/assignments/backend/pane?host=foreign", "/tasks/fixture/assignments/backend/pane?live=1",
                         "/tasks/fixture/assignments/backend/pane?command=send", "/tasks/fixture/assignments/backend/pane#fragment",
                         "http://127.0.0.1:8090/tasks/fixture/assignments/backend/pane", "/?live=1&live=1",
                         "/?live", "/?%6cive=1", "//tasks/fixture/assignments/backend/pane",
                         "/tasks/fix\tture/assignments/backend/pane", " /tasks/fixture/assignments/backend/pane"):
                handler = self.get_without_socket(path)
                self.assertEqual(handler.send_error.call_args.args[0], 400)
            for path in ("/tasks/%66ixture/assignments/backend/pane", "/tasks/../assignments/backend/pane",
                         "/tasks/fixture/assignments/backend/pane/extra"):
                handler = self.get_without_socket(path)
                self.assertEqual(handler.send_error.call_args.args[0], 404)
            load.assert_not_called()

    def test_pane_http_success_and_unavailable_views_use_no_store_and_escape(self):
        rex, _ = self.fake_rex(output='Visible \ud800 café <script>')
        with patch.object(factory, "Rex", return_value=rex):
            handler = self.get_without_socket("/tasks/fixture/assignments/backend/pane")
        handler.send_response.assert_called_with(200)
        handler.send_header.assert_any_call("Cache-Control", "no-store")
        self.assertIn(r"Visible \ud800 café &lt;script&gt;", handler.wfile.getvalue().decode())
        handler = self.get_without_socket("/tasks/fixture/assignments/missing/pane")
        handler.send_response.assert_called_with(503)
        self.assertIn("Restore the original", handler.wfile.getvalue().decode())

    def test_team_controls_still_post_with_csrf_and_keep_idempotent_history(self):
        original = factory.hold_task
        def fixed_clock(*args, **kwargs):
            return original(*args, **kwargs, clock=lambda: 900)
        def post(fields, **kwargs):
            body = urlencode(fields).encode()
            return self.get_without_socket("/tasks/fixture/hold", method="POST", body=body,
                headers={"Content-Length": str(len(body)), "Content-Type": "application/x-www-form-urlencoded"}, **kwargs)
        resume = dict(csrf="fixed-csrf", request_id="resume-one", expected_revision="0", action="resume", reason="")
        with patch.object(dashboard.secrets, "token_urlsafe", return_value="fixed-csrf"), \
             patch.object(factory, "hold_task", side_effect=fixed_clock):
            page = self.get_without_socket("/").wfile.getvalue().decode()
            self.assertIn('action="/tasks/fixture/hold"', page)
            self.assertIn('name="csrf" value="fixed-csrf"', page)
            handler = post(resume)
            handler.send_response.assert_called_with(303)
            handler.send_header.assert_any_call("Location", "/#task-fixture")
            handler = post(dict(resume, request_id="pause-two", expected_revision="1", action="pause", reason="Hold again"))
            handler.send_response.assert_called_with(303)
            before = self.path.read_bytes()
            handler = post(resume)
            handler.send_response.assert_called_with(303)
            self.assertEqual(self.path.read_bytes(), before)
            handler = post(dict(resume, request_id="stale-form"))
            self.assertEqual(handler.send_error.call_args.args[0], 409)
            with patch.object(factory, "load_task", side_effect=AssertionError("No source reads before CSRF")) as load:
                handler = post(dict(resume, csrf="bad"))
                self.assertEqual(handler.send_error.call_args.args[0], 403)
                handler = post(resume, auth=False)
                handler.send_response.assert_called_with(401)
                load.assert_not_called()
        saved = factory.load_task(self.path)
        self.assertEqual(saved["hold"], "Hold again")
        self.assertEqual(len(saved["control_events"]), 2)
        page = dashboard.render(self.snapshot())
        self.assertIn("Recent control requests", page)
        self.assertIn("Hold again", page)

    def test_provisioning_and_inbox_controls_preserve_owner_facts_together(self):
        for state in ("provisioning", "provisioned"):
            with self.subTest(state=state):
                item = dict(id="writer", owner_token="b" * 32, state=state,
                    git_dir=str(self.root / ".git/worktrees/writer") if state == "provisioned" else None,
                    request=dict(path=str(self.root / "writer"), branch="factory/fixture/writer",
                                 inputs=dict(self.record["inputs"])))
                self.record["provisioning"] = item
                factory.save(self.path, self.record)
                original = self.path.read_bytes()
                with patch.object(factory, "connect", side_effect=AssertionError("Offline only")):
                    view = self.snapshot()
                task, = view["tasks"]
                self.assertEqual(task["provisioning"], dict(item, inputs_current=True))
                self.assertEqual(task["admission"]["limits"], self.record["admission"]["limits"])
                self.assertEqual(task["allowance_events"], [])
                self.assertEqual(task["allowances"]["correction"], dict(used=1, limit=1, remaining=0))
                page = dashboard.render(view, csrf_token="fixture-csrf")
                self.assertIn("Writer worktree writer: " + state, page)
                self.assertIn("current inputs", page)
                self.assertIn('action="/tasks/fixture/allowance"', page)
                self.assertIn("Original admission", page)
                self.assertIn("Waiting for scope decision", page)
                self.assertEqual(self.path.read_bytes(), original)

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
        self.assertEqual(request("/", method="POST")[0], 404)
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

    def test_http_displays_lone_surrogate_without_losing_the_page_or_source(self):
        goal = "Visible \ud800 café <script>"
        self.record["admission"]["goal"] = goal
        factory.save(self.path, self.record)
        before = self.path.read_bytes()
        key = "not-a-real-key"
        authorization = "Basic " + base64.b64encode(("operator:" + key).encode()).decode()
        with dashboard.make_server(self.root, port=0, access_key=key) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("GET", "/", headers={"Authorization": authorization})
                response = connection.getresponse()
                body = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn(r"Visible \ud800 café &lt;script&gt;", body)
                connection.request("GET", "/api/state", headers={"Authorization": authorization})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["tasks"][0]["goal"], goal)
            finally:
                connection.close()
                server.shutdown()
                thread.join()
        self.assertEqual(self.path.read_bytes(), before)

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
