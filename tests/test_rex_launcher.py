"""Failure/restart tests use a fake server; no live sessions or providers."""
import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import factory


class FakeRex:
    sessions = []
    calls = []
    fail_after_create = False
    host = "test-host"

    def __init__(self, binary, server):
        self.binary, self.server = str(binary), server

    def call(self, *args, **kwargs):
        type(self).calls.append(args)
        if args[0] == "whoami":
            return {"host_id": type(self).host}
        if args[0] == "ls":
            return {"sessions": self.sessions}
        if args[:2] == ("session", "inspect"):
            return next(item for item in self.sessions if item["session_id"] == args[2])
        if args[0] == "new" or args[:2] == ("window", "new"):
            if args[0] == "new":
                session = {"session_id": "session-test", "label": args[1], "windows": []}
                self.sessions.append(session)
                label = args[args.index("--window") + 1]
            else:
                session = next(item for item in self.sessions if item["session_id"] == args[3])
                label = args[4]
            number = len(session["windows"]) + 1
            window = dict(window_id=f"window-{number}", label=label,
                          layers=[dict(blocks=[dict(block_id=f"block-{number}")])])
            session["windows"].append(window)
            if self.fail_after_create:
                raise subprocess.TimeoutExpired("rex", 20)
            created = dict(window_id=window["window_id"], block_ids=[f"block-{number}"])
            return dict(session_id=session["session_id"], initial_windows=[created]) if args[0] == "new" else created
        raise AssertionError(args)


class NativeLauncher(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "task.json"
        FakeRex.sessions, FakeRex.calls = [], []
        FakeRex.fail_after_create, FakeRex.host = False, "test-host"
        self.patcher = patch.object(factory, "Rex", FakeRex)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.arguments = dict(task="task", assignment_id="qa", role="qa", goal="Inspect only",
                              cwd=str(self.root), command=[sys.executable, "{instructions}"],
                              binary=sys.executable, server="unix:///test.sock")

    def start(self, **changes):
        return factory.launch(self.path, **{**self.arguments, **changes})

    def creation_calls(self):
        return [args for args in FakeRex.calls
                if args[0] == "new" or args[:2] == ("window", "new")]

    def test_repeated_assignment_reuses_exact_block(self):
        first = self.start()
        second = self.start()
        self.assertEqual(first[1]["block_id"], second[1]["block_id"])
        self.assertEqual(len(self.creation_calls()), 1)

    def test_role_instructions_are_passed_as_one_argument_without_shell(self):
        self.start()
        command = self.creation_calls()[0]
        self.assertIn("--shell=none", command)
        self.assertIn("# Assignment: qa", command[-1])
        self.assertIn("Inspect only", command[-1])

    def test_timeout_after_create_recovers_without_relaunch(self):
        FakeRex.fail_after_create = True
        with self.assertRaises(factory.FactoryError):
            self.start()
        pending = json.loads(self.path.read_text())
        self.assertEqual(pending["assignments"]["qa"]["state"], "launching")
        FakeRex.fail_after_create = False
        record, assignment = self.start()
        self.assertEqual(assignment["block_id"], "block-1")
        self.assertEqual(record["session_id"], "session-test")
        self.assertEqual(len(self.creation_calls()), 1)

    def test_missing_timed_out_launch_is_not_blindly_retried(self):
        FakeRex.fail_after_create = True
        with self.assertRaises(factory.FactoryError):
            self.start()
        FakeRex.sessions.clear()
        FakeRex.fail_after_create = False
        with self.assertRaisesRegex(factory.FactoryError, "outcome unknown"):
            self.start()
        self.assertEqual(len(self.creation_calls()), 1)

    def test_another_specialist_gets_same_session_and_separate_window(self):
        first = self.start()
        second = self.start(assignment_id="security", role="security")
        self.assertEqual(first[0]["session_id"], second[0]["session_id"])
        self.assertNotEqual(first[1]["block_id"], second[1]["block_id"])
        self.assertIn("--focus=false", self.creation_calls()[-1])

    def test_timed_out_second_window_recovers_by_exact_marker(self):
        self.start()
        FakeRex.fail_after_create = True
        with self.assertRaises(factory.FactoryError):
            self.start(assignment_id="security", role="security")
        FakeRex.fail_after_create = False
        _, assignment = self.start(assignment_id="security", role="security")
        self.assertEqual(assignment["block_id"], "block-2")
        self.assertEqual(len(self.creation_calls()), 2)

    def test_changed_input_does_not_reuse_an_old_assignment(self):
        self.start()
        with self.assertRaisesRegex(factory.FactoryError, "different inputs"):
            self.start(goal="A changed task")
        self.assertEqual(len(self.creation_calls()), 1)

    def test_changed_endpoint_or_host_cannot_launch_more_work(self):
        self.start()
        with self.assertRaisesRegex(factory.FactoryError, "another Rex endpoint"):
            self.start(server="unix:///other.sock")
        FakeRex.host = "another-host"
        with self.assertRaisesRegex(factory.FactoryError, "differs"):
            self.start(assignment_id="security", role="security")
        self.assertEqual(len(self.creation_calls()), 1)

    def test_pending_effect_prevents_unrelated_new_launch(self):
        FakeRex.fail_after_create = True
        with self.assertRaises(factory.FactoryError):
            self.start()
        with self.assertRaisesRegex(factory.FactoryError, "pending assignment"):
            self.start(assignment_id="security", role="security")

    def test_concurrent_task_mutations_and_path_traversal_are_rejected(self):
        with factory.task_lock(self.root, "task"):
            with self.assertRaisesRegex(factory.FactoryError, "Another command"):
                with factory.task_lock(self.root, "task"):
                    self.fail("Second mutation acquired lock")
        with self.assertRaises(factory.FactoryError):
            with factory.task_lock(self.root, "../escape"):
                self.fail("Invalid path accepted")

    def test_process_exit_is_not_task_acceptance(self):
        record, assignment = self.start()
        rex = FakeRex(sys.executable, "unix:///test.sock")
        with patch.object(rex, "call", return_value={"last_exit": {"exit_code": 0}}):
            result = factory.status(rex, record, assignment)
        self.assertEqual(result["state"], "exited")
        self.assertNotIn("accepted", result)


class TaskAdmission(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.path = self.state / "task.json"
        self.repo = self.root / "repo"
        subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(self.repo)], check=True)
        self.commit("Fixture")
        FakeRex.sessions, FakeRex.calls = [], []
        FakeRex.fail_after_create, FakeRex.host = False, "test-host"
        self.patcher = patch.object(factory, "Rex", FakeRex)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def commit(self, message):
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                        "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--allow-empty", "-m", message],
                       check=True, env={**os.environ, "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
                                        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"})

    def admit(self, **changes):
        arguments = dict(task="task", goal="Verify the example", repository=str(self.repo),
                         acceptance=["docs/example.md#acceptance"], corrections=1, investigations=1)
        return factory.admit(self.path, **{**arguments, **changes})

    def start(self, **changes):
        arguments = dict(task="task", assignment_id="qa", role="qa", goal="Inspect only",
                         cwd=str(self.repo), command=[sys.executable, "{instructions}"],
                         binary=sys.executable, server="unix:///test.sock", purpose="work")
        return factory.launch(self.path, **{**arguments, **changes})

    def cli(self, *arguments):
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = factory.main(["--state-dir", str(self.state), *arguments])
        return result, output.getvalue(), errors.getvalue()

    def test_admission_is_idempotent_and_inspection_is_offline(self):
        first = self.admit()
        self.assertEqual(self.admit(), first)
        result, output, errors = self.cli("show", "--task", "task")
        self.assertEqual((result, errors), (0, ""))
        view = json.loads(output)
        self.assertEqual(view["admission"]["goal"], "Verify the example")
        self.assertEqual(view["admission"]["acceptance"], ["docs/example.md#acceptance"])
        expected_head = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        self.assertEqual(view["admission"]["initial_candidate"], expected_head)
        self.assertEqual(view["allowances"]["correction"], dict(limit=1, used=0, remaining=1))
        self.assertIsNone(view["rex"])
        self.assertEqual(FakeRex.calls, [])

    def test_invalid_admission_and_changed_limits_cannot_reset_task(self):
        for changes in ({"goal": " "}, {"acceptance": []}, {"acceptance": [""]}, {"corrections": -1}):
            with self.subTest(changes=changes), self.assertRaises(factory.FactoryError):
                self.admit(**changes)
        self.assertFalse(self.path.exists())
        self.admit()
        self.start(purpose="correction")
        before = self.path.read_bytes()
        self.commit("Implementation advanced the head")
        with self.assertRaisesRegex(factory.FactoryError, "different admission inputs"):
            self.admit(corrections=10)
        self.admit()
        self.assertEqual(self.path.read_bytes(), before)

    def test_allowance_survives_new_worker_id_and_controller_restart(self):
        self.admit()
        self.start(purpose="correction")
        FakeRex.calls.clear()
        with self.assertRaisesRegex(factory.FactoryError, "exhausted; route to investigator"):
            self.start(assignment_id="security", role="security", purpose="correction")
        view = factory.task_view(factory.load_task(self.path))
        self.assertEqual(view["allowances"]["correction"], dict(limit=1, used=1, remaining=0))
        self.assertEqual(FakeRex.calls, [])

    def test_unknown_launch_reserves_once_and_reconnect_does_not_charge_again(self):
        self.admit()
        FakeRex.fail_after_create = True
        with self.assertRaisesRegex(factory.FactoryError, "outcome uncertain"):
            self.start(purpose="correction")
        view = factory.task_view(factory.load_task(self.path))
        self.assertEqual(view["allowances"]["correction"]["remaining"], 0)
        FakeRex.fail_after_create = False
        record, assignment = self.start(purpose="correction")
        self.assertEqual(assignment["block_id"], "block-1")
        self.assertEqual(factory.task_view(record)["allowances"]["correction"]["used"], 1)
        self.assertEqual(len(FakeRex.sessions[0]["windows"]), 1)

    def test_pause_blocks_new_effects_but_allows_reconciliation(self):
        self.admit()
        FakeRex.fail_after_create = True
        with self.assertRaises(factory.FactoryError):
            self.start(purpose="correction")
        factory.hold_task(self.path, "Operator hold")
        FakeRex.calls.clear()
        with self.assertRaisesRegex(factory.FactoryError, "Task is paused"):
            self.start(assignment_id="security", role="security")
        self.assertEqual(FakeRex.calls, [])
        FakeRex.fail_after_create = False
        _, assignment = self.start(purpose="correction")
        self.assertEqual(assignment["block_id"], "block-1")
        factory.hold_task(self.path, None)
        with self.assertRaisesRegex(factory.FactoryError, "allowance exhausted"):
            self.start(assignment_id="correction-two", purpose="correction")
        _, independent = self.start(assignment_id="security", role="security")
        self.assertEqual(independent["block_id"], "block-2")

    def test_investigation_is_bounded_and_requires_investigator(self):
        self.admit(corrections=0)
        with self.assertRaisesRegex(factory.FactoryError, "allowance exhausted"):
            self.start(purpose="correction")
        for changes in ({"purpose": None}, {"role": "investigator"}, {"purpose": "investigation"}):
            with self.subTest(changes=changes), self.assertRaises(factory.FactoryError):
                self.start(**changes)
        self.assertEqual(FakeRex.calls, [])
        self.start(role="investigator", purpose="investigation")
        with self.assertRaisesRegex(factory.FactoryError, "exhausted; route to operator"):
            self.start(assignment_id="another-investigator", role="investigator", purpose="investigation")

    def test_legacy_record_migrates_without_relaunch_or_inferred_allowances(self):
        _, original = self.start(purpose=None)
        record = json.loads(self.path.read_text())
        for key in ("schema_version", "admission", "hold"):
            del record[key]
        self.path.write_text(json.dumps(record))
        migrated = factory.load_task(self.path)
        self.assertEqual(migrated["schema_version"], 1)
        self.assertIsNone(factory.task_view(migrated)["allowances"])
        self.assertEqual(migrated["assignments"], record["assignments"])
        _, reconnected = self.start(purpose=None)
        self.assertEqual(reconnected["block_id"], original["block_id"])
        self.assertEqual(len(FakeRex.sessions[0]["windows"]), 1)
        with self.assertRaisesRegex(factory.FactoryError, "unclassified attempts"):
            self.admit()

    def test_unknown_or_damaged_schema_refuses_without_rex_or_state_changes(self):
        original = self.admit()
        variants = [{**original, "schema_version": 99},
                    {**original, "admission": {"limits": {"correction": -1, "investigation": 1}}},
                    {key: value for key, value in original.items() if key != "schema_version"}]
        for variant in variants:
            self.path.write_text(json.dumps(variant))
            before = self.path.read_bytes()
            with self.subTest(variant=variant), self.assertRaises(factory.FactoryError):
                self.start()
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, [])

    def test_cli_admit_pause_resume_and_missing_task(self):
        result, output, errors = self.cli("admit", "--task", "task", "--goal", "Inspect",
                                          "--repo", str(self.repo), "--acceptance", "criteria.md")
        self.assertEqual((result, errors), (0, ""))
        self.assertEqual(json.loads(output)["allowances"]["correction"]["limit"], 2)
        result, output, errors = self.cli("pause", "--task", "task", "--reason", "Operator hold")
        self.assertEqual((result, errors), (0, ""))
        self.assertEqual(json.loads(output)["hold"], "Operator hold")
        result, output, errors = self.cli("resume", "--task", "task")
        self.assertEqual((result, errors), (0, ""))
        self.assertIsNone(json.loads(output)["hold"])
        result, output, errors = self.cli("pause", "--task", "task", "--reason", " ")
        self.assertEqual((result, output), (1, ""))
        self.assertIn("nonempty pause reason", errors)
        result, output, errors = self.cli("show", "--task", "missing")
        self.assertEqual((result, output), (1, ""))
        self.assertIn("factory:", errors)
        self.assertFalse((self.state / "missing.json").exists())
        self.assertEqual(FakeRex.calls, [])


if __name__ == "__main__":
    unittest.main()
