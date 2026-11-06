"""Failure/restart tests use a fake server; no live sessions or providers."""
import json
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


if __name__ == "__main__":
    unittest.main()
