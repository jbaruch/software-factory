"""Native result/lifecycle tests use fake processes and Rex, never model calls."""
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import codex_worker as worker
import factory
from test_rex_launcher import FakeRex


def events():
    return [dict(type="thread.started", thread_id="thread-fixture"),
            dict(type="turn.started"),
            dict(type="item.completed", item=dict(type="agent_message", text="Checking the candidate")),
            dict(type="item.completed", item=dict(type="agent_message", text=json.dumps(
                dict(outcome="approved", artifacts=["evidence/qa.txt"])))),
            dict(type="turn.completed", usage=dict(input_tokens=100, output_tokens=30))]


class FakeProcess:
    def __init__(self, payload, exit_code=0, timeout=False):
        self.lines = iter([json.dumps(event).encode() + b"\n" for event in payload])
        self.stdout = self
        self.returncode = None
        self.exit_code, self.timeout = exit_code, timeout
        self.signals = []

    async def readline(self):
        line = next(self.lines, None)
        if line is None and self.timeout:
            raise TimeoutError("fixture")
        return line or b""

    async def wait(self):
        self.returncode = self.exit_code
        return self.returncode

    def send_signal(self, signal):
        self.signals.append(signal)


class NativeResults(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repo = self.root / "repo"
        subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                        "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--allow-empty", "-m", "Fixture"],
                       check=True, env={**os.environ, "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
                                        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"})
        previous_cwd = Path.cwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, previous_cwd)
        self.candidate = factory.git_commit(str(self.repo), "HEAD")
        self.identity: dict = factory.repository_identity(str(self.repo), create=True)
        self.brief = self.root / "brief.md"
        self.brief.write_text("Review the exact candidate and link evidence.")
        self.args = worker.parser().parse_args([
            "--codex", sys.executable, "--repo", str(self.repo), "--cwd", str(self.repo),
            "--brief", str(self.brief), "--brief-sha256", hashlib.sha256(self.brief.read_bytes()).hexdigest(),
            "--candidate", self.candidate, "--requirements", "requirements-1", "--job", "task/qa",
            "--repo-identity", json.dumps(self.identity),
            "--worktree-git-dir", str(factory.git_dir(str(self.repo))),
            "--model", "fixture-model", "--effort", "high", "--events", str(self.root / "events.jsonl"),
            "--receipt", str(self.root / "receipt.json")])
        self.env = patch.dict(os.environ, {"CODEX_API_KEY": "", "OPENAI_API_KEY": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.spawns = []
        self.process = FakeProcess(events())

    async def spawn(self, *command, **options):
        self.spawns.append(command)
        return self.process

    def execute(self):
        with redirect_stdout(StringIO()):
            return asyncio.run(worker.execute(self.args, spawn=self.spawn,
                                               clock=lambda: 1000.0, timer=lambda: 10.0))

    def test_completion_preserves_original_events_and_private_bound_receipt(self):
        receipt = self.execute()
        self.assertEqual(receipt["state"], "completed")
        self.assertEqual(receipt["schema_version"], 4)
        self.assertEqual(receipt["repository_identity"], self.identity)
        self.assertEqual(receipt["worktree_git_dir"], str(factory.git_dir(str(self.repo))))
        self.assertEqual(receipt["candidate"], self.candidate)
        self.assertEqual(receipt["inputs"]["requirements"], "requirements-1")
        self.assertEqual(receipt["thread_id"], "thread-fixture")
        self.assertEqual(receipt["usage"], dict(input_tokens=100, output_tokens=30))
        self.assertEqual(receipt["finished_at"], 1000.0)
        self.assertEqual(json.loads(self.args.receipt.read_text()), receipt)
        self.assertEqual(worker.read_events(self.args.events), events())
        first = json.loads(self.args.events.read_text().splitlines()[0])
        self.assertEqual(first, dict(schema_version=1, kind="codex-event-line",
                                    data=json.dumps(events()[0]) + "\n"))
        self.assertEqual(self.args.events.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.args.receipt.stat().st_mode & 0o777, 0o600)
        self.assertIn("read-only", self.spawns[0])
        self.assertEqual(self.spawns[0][self.spawns[0].index("--cd") + 1], ".")
        self.assertEqual(self.spawns[0][-1], self.brief.read_text())
        before = self.args.receipt.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "never relaunch"):
            self.execute()
        self.assertEqual(len(self.spawns), 1)
        self.assertEqual(self.args.receipt.read_bytes(), before)

    def test_duplicate_web_search_telemetry_ids_are_not_used_as_evidence(self):
        raw = (b'{"type":"item.started","item":{"id":"item_20","type":"web_search",'
               b'"id":"exec-fixed","query":"","action":{"type":"other"}}}\n')
        payload = events()
        lines = [json.dumps(event).encode() + b"\n" for event in payload]
        self.process.lines = iter([*lines[:2], raw, *lines[2:]])
        receipt = self.execute()
        self.assertEqual(receipt["state"], "completed")
        retained = [json.loads(line) for line in self.args.events.read_text().splitlines()]
        self.assertEqual(retained[2]["data"], raw.decode())
        decoded = worker.read_events(self.args.events)
        self.assertNotIn("id", decoded[2]["item"])
        self.assertEqual(worker.parse_events(decoded)["report"], dict(outcome="approved", artifacts=["evidence/qa.txt"]))

    def test_other_native_duplicate_fields_still_refuse_decoding(self):
        for raw in (
            '{"type":"thread.started","thread_id":"first","thread_id":"second"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"first","text":"second"}}',
            '{"type":"turn.completed","usage":{"input_tokens":1,"input_tokens":2,"output_tokens":3}}',
            '{"type":"item.started","item":{"type":"web_search","id":"one","id":"two","query":"first","query":"second"}}',
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                json.loads(raw, object_pairs_hook=worker.native_object)

    def test_spawn_failure_retains_an_actionable_failed_receipt(self):
        async def unavailable(*args, **kwargs):
            raise FileNotFoundError("Fixture executable disappeared")
        receipt = asyncio.run(worker.execute(self.args, spawn=unavailable, clock=lambda: 1000, timer=lambda: 10))
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("restore executable access/local process resources", receipt["error"])
        self.assertIsNone(receipt["exit_code"])
        self.assertIsNone(receipt["report"])
        self.assertEqual(json.loads(self.args.receipt.read_text()), receipt)

    def test_native_pipe_failure_retains_recovery_and_stops_owned_child(self):
        with patch.object(self.process, "readline", side_effect=ConnectionResetError("raw fixture errno")):
            receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("inspect the retained stream, storage and Rex pane", receipt["error"])
        self.assertNotIn("raw fixture errno", receipt["error"])
        self.assertIsNone(receipt["report"])
        self.assertIsNotNone(self.process.returncode)
        self.assertEqual(json.loads(self.args.receipt.read_text()), receipt)

    def test_exit_before_cleanup_kill_still_saves_the_final_receipt(self):
        class RaceProcess(FakeProcess):
            waits = 0
            async def wait(self):
                self.waits += 1
                if self.waits == 1:
                    raise TimeoutError("Fixture cleanup deadline")
                self.returncode = 0
                return 0
            def kill(self):
                raise ProcessLookupError("Already exited")
        self.process = RaceProcess(events(), timeout=True)
        receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(self.process.waits, 2)
        self.assertEqual(json.loads(self.args.receipt.read_text()), receipt)

    def test_zero_exit_without_result_is_failed_and_never_retried(self):
        self.process = FakeProcess(events()[:2])
        receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIsNone(receipt["report"])
        self.assertIn("completion", receipt["error"])
        with self.assertRaises(factory.FactoryError):
            self.execute()
        self.assertEqual(len(self.spawns), 1)

    def test_malformed_native_lines_retain_evidence_and_explain_recovery(self):
        for index, line in enumerate((b'{"truncated":\n', b'\xff\n',
                b'{"type":"thread.started","thread_id":"fixture","unused":[NaN]}\n',
                b'{"type":"thread.started","thread_id":"fixture","unused":{"n":Infinity}}\n',
                b'{"type":"thread.started","thread_id":"fixture","unused":-Infinity}\n',
                b'{"type":"thread.started","thread_id":"fixture","unused":1e999}\n',
                ('{"type":"thread.started","thread_id":"fixture","unused":{"n":' + str(10 ** 400) + '}}\n').encode())):
            self.args.events = self.root / f"bad-{index}.jsonl"
            self.args.receipt = self.root / f"bad-{index}.json"
            self.process = FakeProcess([])
            self.process.lines = iter([line])
            receipt = self.execute()
            self.assertEqual(receipt["state"], "failed")
            self.assertIn("inspect the retained stream and update the adapter", receipt["error"])
            self.assertIsNone(receipt["report"])
            record = json.loads(self.args.events.read_text())
            self.assertEqual(record["data"].encode("utf-8", errors="surrogateescape"), line)
            with self.assertRaisesRegex(worker.WorkerError, "Malformed native events; inspect"):
                worker.read_events(self.args.events)

    def test_timeout_stops_only_owned_child_and_retains_failed_attempt(self):
        self.process = FakeProcess(events()[:2], exit_code=-2, timeout=True)
        receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("allowance expired", receipt["error"])
        self.assertEqual(len(self.process.signals), 1)
        self.assertEqual(receipt["exit_code"], -2)
        self.assertEqual(receipt["thread_id"], "thread-fixture")
        self.assertTrue(self.args.events.exists())

    def test_changed_brief_and_api_override_refuse_before_native_spawn(self):
        self.brief.write_text("Changed instructions")
        with self.assertRaisesRegex(factory.FactoryError, "brief changed"):
            self.execute()
        self.assertEqual(self.spawns, [])
        self.assertFalse(self.args.events.exists())
        with patch.dict(os.environ, {"OPENAI_API_KEY": "not-a-real-key"}):
            with self.assertRaisesRegex(factory.FactoryError, "API-key overrides"):
                self.execute()

    def test_unrepresentable_allowance_refuses_before_creating_execution_artifacts(self):
        for seconds in (0, -1, True, factory.MAX_NATIVE_SECONDS + 1, 10 ** 400):
            self.args.seconds = seconds
            with self.subTest(seconds=seconds), self.assertRaisesRegex(factory.FactoryError,
                    "execution allowance of at most 86400 seconds"):
                self.execute()
            self.assertEqual(self.spawns, [])
            for path in (self.args.events, self.args.receipt, self.args.receipt.with_suffix('.schema.json')):
                self.assertFalse(path.exists())

    def test_missing_native_binary_and_damaged_brief_identify_preflight_recovery(self):
        original = self.args.codex
        self.args.codex = self.root / "missing-native"
        with self.assertRaisesRegex(factory.FactoryError, "Cannot access native CLI .*inspect/restore the saved executable"):
            self.execute()
        self.args.codex = original
        for data in (None, b'\xff'):
            if data is None:
                self.brief.unlink()
            else:
                self.brief.write_bytes(data)
            with self.assertRaisesRegex(factory.FactoryError, "Cannot read original brief .*inspect/restore the saved assignment input"):
                self.execute()
        self.assertEqual(self.spawns, [])
        self.assertFalse(self.args.receipt.exists())
        self.assertFalse(self.args.events.exists())

    def test_dirty_checkout_after_native_exit_cannot_be_verification(self):
        async def changed(*command, **options):
            (self.repo / "uncommitted.txt").write_text("Uncommitted changes")
            return self.process
        with redirect_stdout(StringIO()):
            receipt = asyncio.run(worker.execute(self.args, spawn=changed,
                                                 clock=lambda: 1000.0, timer=lambda: 10.0))
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("clean", receipt["error"])

    def test_invalid_streams_and_reports_cannot_be_registered(self):
        malformed = [events()[1:], events() + [dict(type="turn.completed")],
                     events() + [dict(type="turn.failed")], [None],
                     events() + [dict(type="item.completed", item=None)]]
        for change in (dict(artifacts=[""]), dict(outcome="accepted")):
            payload = events()
            report = dict(outcome="approved", artifacts=["evidence.txt"])
            payload[-2]["item"]["text"] = json.dumps({**report, **change})
            malformed.append(payload)
        payload = events()
        payload[-1]["usage"] = dict(input_tokens=True, output_tokens=0)
        malformed.append(payload)
        for payload in malformed:
            with self.subTest(payload=payload), self.assertRaises(worker.WorkerError):
                worker.parse_events(payload)
        payload = events()
        del payload[-1]["usage"]
        self.assertIsNone(worker.parse_events(payload)["usage"])

    def replace_with_clone(self):
        preserved = self.root / "original-repository"
        self.repo.rename(preserved)
        subprocess.run(["git", "clone", "--quiet", str(preserved), str(self.repo)], check=True)

    def test_replaced_history_sharing_checkout_refuses_before_worker_spawn(self):
        self.replace_with_clone()
        self.assertEqual(factory.git_commit(str(self.repo), "HEAD"), self.candidate)
        with self.assertRaisesRegex(factory.FactoryError, "repository marker"):
            self.execute()
        self.assertEqual(self.spawns, [])
        self.assertFalse(self.args.receipt.exists())

    def test_repository_replacement_during_execution_cannot_supply_a_completed_receipt(self):
        async def replaced(*command, **options):
            self.replace_with_clone()
            return self.process
        with redirect_stdout(StringIO()):
            receipt = asyncio.run(worker.execute(self.args, spawn=replaced))
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["repository_identity"], self.identity)
        self.assertIn("repository identity differs", receipt["error"])

    def launch_assignment(self, codex=sys.executable):
        self.path = self.root / "task.json"
        factory.admit(self.path, task="task", goal="Verify the change", repository=str(self.repo),
                      acceptance=["docs/acceptance.md"])
        command = factory.codex_command(self.path, task="task", assignment_id="qa", codex=codex,
                                        model="fixture-model", effort="high", cwd=str(self.repo))
        FakeRex.sessions, FakeRex.calls = [], []
        FakeRex.fail_after_create, FakeRex.host = False, "test-host"
        with patch.object(factory, "Rex", FakeRex):
            record, assignment = factory.launch(self.path, task="task", assignment_id="qa", role="qa",
                goal="Review the change", cwd=str(self.repo), command=command, purpose="work",
                binary=sys.executable, server="unix:///test.sock")
        actual = next(call for call in FakeRex.calls if call[0] == "new")
        argv = actual[actual.index("--") + 1:]
        self.assertEqual(argv[:4], (sys.executable, "-E", "-s", str(factory.ROOT / "checkout_exec.py")))
        argv = argv[argv.index("--") + 1:]
        self.args = worker.parser().parse_args(argv[2:])
        self.assertEqual(self.args.candidate, self.candidate)
        self.assertEqual(self.args.brief_sha256, assignment["request"]["instructions_sha256"])
        self.assertEqual(json.loads(self.args.repo_identity), record["admission"]["repository_identity"])
        self.execute()
        self.rex = FakeRex(sys.executable, "unix:///test.sock")
        return record, assignment

    def test_native_preflight_preserves_allowances_and_reconnect_survives_removed_binary(self):
        path = self.root / "task.json"
        factory.admit(path, task="task", goal="Verify the change", repository=str(self.repo),
                      acceptance=["docs/acceptance.md"])
        before = path.read_bytes()
        for binary in (self.root / "missing", self.root, self.brief):
            with self.subTest(binary=binary), self.assertRaises(factory.FactoryError):
                factory.codex_command(path, task="task", assignment_id="qa", codex=str(binary),
                                      model="fixture-model", effort="high", cwd=str(self.repo))
            self.assertEqual(path.read_bytes(), before)
        binary = self.root / "native-fixture"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)
        record, assignment = self.launch_assignment(codex=str(binary))
        binary.unlink()
        command = factory.codex_command(path, task="task", assignment_id="qa", codex=str(binary),
                                        model="fixture-model", effort="high", cwd=str(self.repo))
        self.assertEqual(command, assignment["request"]["command"])
        self.assertEqual(factory.load_task(path), record)

    def test_native_selection_freezes_symlink_target(self):
        binary = self.root / "native-fixture"
        binary.symlink_to(sys.executable)
        command = factory.codex_command(self.root / "task.json", task="task", assignment_id="qa",
                                        codex=str(binary), model="fixture-model", effort="high", cwd=str(self.repo))
        chosen = command[command.index("--codex") + 1]
        binary.unlink()
        binary.symlink_to("/bin/false")
        self.assertEqual(chosen, str(Path(sys.executable).resolve()))
        self.assertNotEqual(chosen, str(binary.resolve()))

    def test_blank_native_selection_refuses_before_reading_or_mutating_task(self):
        path = self.root / "task.json"
        record = factory.admit(path, task="task", goal="Verify the change", repository=str(self.repo),
                               acceptance=["docs/acceptance.md"], corrections=1, investigations=1)
        before = path.read_bytes()
        for model, effort in (("", "high"), ("  ", "high"), ("fixture-model", ""), ("fixture-model", "\t")):
            with self.subTest(model=model, effort=effort, cwd=str(self.repo)), patch.object(factory, "load_task") as read:
                with self.assertRaisesRegex(factory.FactoryError, "discovered native model and supported effort"):
                    factory.codex_command(path, task="task", assignment_id="qa", codex=sys.executable,
                                          model=model, effort=effort, cwd=str(self.repo))
                read.assert_not_called()
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(factory.task_view(record)["allowances"]["correction"]["remaining"], 1)

    def test_damaged_saved_native_arguments_name_repair_without_collection(self):
        record, assignment = self.launch_assignment()
        command = assignment["request"]["command"]
        at = command.index("--model")
        del command[at:at + 2]
        factory.save(self.path, record)
        factory.load_task(self.path)
        before = self.path.read_bytes()
        with redirect_stderr(StringIO()), self.assertRaisesRegex(factory.FactoryError,
                "restore the original assignment arguments.*without relaunching"):
            factory.collect_codex(self.path, "qa", self.rex, record, assignment)
        self.assertEqual(self.path.read_bytes(), before)

    def test_public_cli_blank_selection_does_not_upgrade_legacy_task(self):
        path = self.root / "task.json"
        record = factory.admit(path, task="task", goal="Verify the change", repository=str(self.repo),
                               acceptance=["docs/acceptance.md"], corrections=1, investigations=1)
        record["schema_version"] = 3
        record["admission"]["repository_identity"] = dict(
            common_dir=str(self.repo / ".git"), device=1, inode=2)
        factory.save(path, record)
        before = path.read_bytes()
        for model, effort in ((" ", "high"), ("fixture-model", "\t")):
            errors = StringIO()
            with self.subTest(model=model, effort=effort, cwd=str(self.repo)), redirect_stderr(errors), redirect_stdout(StringIO()):
                code = factory.main(["--state-dir", str(self.root), "codex-start", "--task", "task",
                    "--id", "qa", "--role", "qa", "--goal", "Inspect only", "--cwd", str(self.repo),
                    "--codex", sys.executable, "--model", model, "--effort", effort, "--purpose", "correction"])
            self.assertEqual(code, 1)
            self.assertIn("discovered native model and supported effort", errors.getvalue())
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(path.with_suffix(".lock").exists())

    def test_public_cli_path_and_allowance_errors_do_not_upgrade_legacy_task(self):
        path = self.root / "task.json"
        record = factory.admit(path, task="task", goal="Verify the change", repository=str(self.repo),
                               acceptance=["docs/acceptance.md"], corrections=1, investigations=1)
        record["schema_version"] = 3
        record["admission"]["repository_identity"] = dict(
            common_dir=str(self.repo / ".git"), device=1, inode=2)
        factory.save(path, record)
        before = path.read_bytes()
        for codex, seconds in (("relative-codex", "60"), (sys.executable, "0"), (sys.executable, "-1"),
                               (sys.executable, str(factory.MAX_NATIVE_SECONDS + 1)), (sys.executable, str(10 ** 400))):
            errors = StringIO()
            with self.subTest(codex=codex, seconds=seconds), redirect_stderr(errors), redirect_stdout(StringIO()):
                code = factory.main(["--state-dir", str(self.root), "codex-start", "--task", "task",
                    "--id", "qa", "--role", "qa", "--goal", "Inspect only", "--cwd", str(self.repo),
                    "--codex", codex, "--model", "fixture-model", "--effort", "high",
                    "--seconds", seconds, "--purpose", "correction"])
            self.assertEqual(code, 1)
            self.assertIn("absolute native Codex path and a positive execution allowance", errors.getvalue())
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(path.with_suffix(".lock").exists())

    def test_another_linked_worktree_cannot_run_or_supply_a_writer_result(self):
        other = self.root / "other-worktree"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "--quiet", "--detach", str(other), "HEAD"], check=True)
        self.args.write = True
        os.chdir(other)
        try:
            with self.assertRaisesRegex(factory.FactoryError, "Assignment worktree differs"):
                self.execute()
        finally:
            os.chdir(self.repo)
        self.assertEqual(self.spawns, [])
        self.assertFalse(self.args.events.exists())
        self.assertFalse(self.args.receipt.exists())

        async def changed_actual_directory(*command, **options):
            os.chdir(other)
            return self.process
        try:
            with redirect_stdout(StringIO()):
                receipt = asyncio.run(worker.execute(self.args, spawn=changed_actual_directory,
                    clock=lambda: 1000.0, timer=lambda: 10.0))
        finally:
            os.chdir(self.repo)
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("Assignment worktree differs", receipt["error"])
        self.assertEqual(receipt["worktree_git_dir"], str(factory.git_dir(str(self.repo))))

    def test_old_worktree_unbound_command_reconnects_but_cannot_collect(self):
        record, assignment = self.launch_assignment()
        command = assignment["request"]["command"]
        index = command.index("--worktree-git-dir")
        del command[index:index + 2]
        factory.save(self.path, record)
        frozen = factory.codex_command(self.path, task="task", assignment_id="qa", codex=sys.executable,
            model="fixture-model", effort="high", cwd=str(self.repo))
        self.assertEqual(frozen, command)
        with self.assertRaisesRegex(factory.FactoryError, "predates worktree identity"):
            factory.collect_codex(self.path, "qa", self.rex, record, assignment)

    def test_reordered_native_lifecycle_cannot_register_a_result(self):
        record, assignment = self.launch_assignment()
        original = events()
        orders = ((0, 4, 3, 1), (0, 3, 1, 4), (0, 1, 4, 3), (1, 0, 3, 4),
                  (0, 1, 3, 4, 1), (0, 1, 3, 4, 3), (0, 1, 1, 3, 4), (0, 1, 3, 4, 4))
        before = self.path.read_bytes()
        for order in orders:
            stream = [original[index] for index in order]
            self.args.events.write_text("".join(json.dumps(dict(schema_version=1, kind="codex-event-line",
                data=json.dumps(event) + "\n")) + "\n" for event in stream))
            with self.subTest(order=order), patch.object(self.rex, "call", return_value={"last_exit": dict(exit_code=0)}):
                with self.assertRaises(worker.WorkerError):
                    factory.collect_codex(self.path, "qa", self.rex, record, assignment)
            self.assertEqual(self.path.read_bytes(), before)

    def test_nonfatal_startup_diagnostics_do_not_supply_lifecycle_evidence(self):
        stream = events()
        diagnostic = dict(type="item.completed", item=dict(type="error", message="Fixture startup diagnostic"))
        stream.insert(1, diagnostic)
        self.assertEqual(worker.parse_events(stream)["report"]["outcome"], "approved")
        with self.assertRaises(worker.WorkerError):
            worker.parse_events([stream[0], diagnostic, stream[-1], stream[-2], stream[2]])

    def test_legacy_command_reconnects_without_retrofitted_scope_but_refuses_collection(self):
        record, assignment = self.launch_assignment()
        command = assignment["request"]["command"]
        index = command.index("--repo-identity")
        del command[index:index + 2]
        record["schema_version"] = 2
        del record["admission"]["repository_identity"]
        factory.save(self.path, record)
        frozen = factory.codex_command(self.path, task="task", assignment_id="qa", codex=sys.executable,
                                       model="fixture-model", effort="high", cwd=str(self.repo))
        self.assertEqual(frozen, command)
        FakeRex.calls.clear()
        with patch.object(factory, "Rex", FakeRex):
            _, reconnected = factory.launch(self.path, task="task", assignment_id="qa", role="qa",
                goal="Review the change", cwd=str(self.repo), command=frozen, purpose="work",
                binary=sys.executable, server="unix:///test.sock")
        self.assertEqual(reconnected["block_id"], assignment["block_id"])
        self.assertFalse(any(call[0] == "new" or call[:2] == ("window", "new") for call in FakeRex.calls))
        with self.assertRaisesRegex(factory.FactoryError, "predates repository identity"):
            factory.collect_codex(self.path, "qa", self.rex, record, assignment)

    def test_impostor_worker_command_cannot_collect_a_native_result(self):
        record, assignment = self.launch_assignment()
        for prefix in (["/bin/true", str(factory.ROOT / "codex_worker.py")],
                       [sys.executable, "/tmp/codex_worker.py"]):
            impostor = copy.deepcopy(assignment)
            impostor["request"]["command"][:2] = prefix
            with self.assertRaisesRegex(factory.FactoryError, "not a native Codex worker"):
                factory.collect_codex(self.path, "qa", self.rex, record, impostor)
        self.assertIsNone(factory.load_task(self.path)["assignments"]["qa"]["result"])

    def test_unsupported_or_damaged_local_event_records_refuse_collection(self):
        record, assignment = self.launch_assignment()
        original = self.args.events.read_text().splitlines()
        first = json.loads(original[0])
        for damaged in (dict(schema_version=99, kind="codex-event-line", data=first["data"]),
                        events()[0], dict(schema_version=1, kind="codex-event-line", data=None)):
            self.args.events.write_text("\n".join([json.dumps(damaged), *original[1:]]) + "\n")
            with patch.object(self.rex, "call", return_value={"last_exit": dict(exit_code=0)}):
                with self.assertRaisesRegex(worker.WorkerError, "Unsupported native event record"):
                    factory.collect_codex(self.path, "qa", self.rex, record, assignment)
            self.assertIsNone(factory.load_task(self.path)["assignments"]["qa"]["result"])
        for number in (float("nan"), float("inf"), float("-inf")):
            damaged = {**first, "unused": {"numbers": [number]}}
            self.args.events.write_text("\n".join([json.dumps(damaged), *original[1:]]) + "\n")
            with patch.object(self.rex, "call", return_value={"last_exit": dict(exit_code=0)}):
                with self.assertRaisesRegex(worker.WorkerError, "Malformed native events"):
                    factory.collect_codex(self.path, "qa", self.rex, record, assignment)
            self.assertIsNone(factory.load_task(self.path)["assignments"]["qa"]["result"])

    def test_collection_binds_current_inputs_and_reuses_completed_result(self):
        record, assignment = self.launch_assignment()
        with patch.object(self.rex, "call", return_value={"last_exit": dict(exit_code=0)}):
            first = factory.collect_codex(self.path, "qa", self.rex, record, assignment)
            second = factory.collect_codex(self.path, "qa", self.rex, record, assignment)
        self.assertEqual(first, second)
        self.assertTrue(factory.task_view(first)["assignments"]["qa"]["result_current"])
        self.assertEqual(first["assignments"]["qa"]["result"]["artifacts"][0], str(self.args.receipt))
        factory.set_inputs(self.path, candidate=self.candidate, requirements="requirements-2")
        self.assertFalse(factory.task_view(factory.load_task(self.path))["assignments"]["qa"]["result_current"])

    def test_tampered_receipt_and_unfinished_process_cannot_supply_result(self):
        record, assignment = self.launch_assignment()
        original = json.loads(self.args.receipt.read_text())
        for change in (dict(job="task/other"), dict(report=dict(outcome="completed", artifacts=["other"])),
                       dict(schema_version=True), dict(schema_version=1.0),
                       dict(exit_code=False), dict(writer=0), dict(binary="/bin/false"), dict(schema_version=1), dict(schema_version=2), dict(schema_version=3),
                       dict(worktree_git_dir="/fixture/other-git-dir"),
                       dict(repository_identity={**self.identity, "repository_id": "b" * 32})):
            factory.save(self.args.receipt, {**copy.deepcopy(original), **change})
            with patch.object(self.rex, "call", return_value={"last_exit": dict(exit_code=0)}):
                with self.assertRaises(factory.FactoryError):
                    factory.collect_codex(self.path, "qa", self.rex, record, assignment)
            self.assertIsNone(factory.load_task(self.path)["assignments"]["qa"]["result"])
        with patch.object(self.rex, "call", return_value={"child": {"pid": 1}}):
            with self.assertRaisesRegex(factory.FactoryError, "not exited"):
                factory.collect_codex(self.path, "qa", self.rex, record, assignment)

    def test_damaged_receipt_explains_how_to_recover_without_recording_a_result(self):
        record, assignment = self.launch_assignment()
        for data in (b'{"truncated":', b'\xff'):
            self.args.receipt.write_bytes(data)
            with patch.object(self.rex, "call", return_value={"last_exit": dict(exit_code=0)}):
                with self.subTest(data=data), self.assertRaisesRegex(factory.FactoryError,
                        "Cannot read native receipt .*inspect or restore the original artifact"):
                    factory.collect_codex(self.path, "qa", self.rex, record, assignment)
            self.assertIsNone(factory.load_task(self.path)["assignments"]["qa"]["result"])

    def test_completed_receipt_requires_valid_timing_and_error_fields(self):
        record, assignment = self.launch_assignment()
        original = json.loads(self.args.receipt.read_text())
        cases = []
        for key in ("created_at", "finished_at", "elapsed_seconds", "error", "usage", "worktree_git_dir"):
            missing = copy.deepcopy(original)
            del missing[key]
            cases.append(missing)
        for key in ("created_at", "finished_at", "elapsed_seconds"):
            for bad in (None, "1000", True, -1, float("nan"), float("inf"), 10 ** 400):
                cases.append({**original, key: bad})
        cases.extend(({**original, "error": "failed"}, {**original, "finished_at": 999}))
        before = self.path.read_bytes()
        for index, receipt in enumerate(cases):
            factory.save(self.args.receipt, receipt)
            with self.subTest(index=index), patch.object(self.rex, "call", return_value={"last_exit": dict(exit_code=0)}):
                with self.assertRaisesRegex(factory.FactoryError, "Native receipt does not match|Cannot read native receipt"):
                    factory.collect_codex(self.path, "qa", self.rex, record, assignment)
            self.assertEqual(self.path.read_bytes(), before)

    def test_duplicate_receipt_and_event_keys_cannot_register_a_result(self):
        record, assignment = self.launch_assignment()
        receipt = self.args.receipt.read_text()
        events_text = self.args.events.read_text()
        duplicate_receipt = receipt.replace('"schema_version": 4', '"schema_version": 99, "schema_version": 4', 1)
        duplicate_event = events_text.replace('"schema_version": 1', '"schema_version": 99, "schema_version": 1', 1)
        for damaged_receipt, damaged_events, error in (
                (duplicate_receipt, events_text, "Cannot read native receipt"),
                (receipt, duplicate_event, "Malformed native events")):
            self.args.receipt.write_text(damaged_receipt)
            self.args.events.write_text(damaged_events)
            with patch.object(self.rex, "call", return_value={"last_exit": dict(exit_code=0)}):
                with self.assertRaisesRegex((factory.FactoryError, worker.WorkerError), error):
                    factory.collect_codex(self.path, "qa", self.rex, record, assignment)
            self.assertIsNone(factory.load_task(self.path)["assignments"]["qa"]["result"])


if __name__ == "__main__":
    unittest.main()
