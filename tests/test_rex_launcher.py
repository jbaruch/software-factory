"""Failure/restart tests use a fake server; no live sessions or providers."""
import json
import copy
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import shutil
import shlex
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
                session = {"session_id": "session-test" if not self.sessions else f"session-test-{len(self.sessions) + 1}",
                           "label": args[1], "windows": []}
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

    def test_valid_task_and_assignment_names_cannot_overwrite_another_brief(self):
        arguments = {key: value for key, value in self.arguments.items()
                     if key not in ("task", "assignment_id", "goal")}
        _, first = factory.launch(self.root / "a-b.json", task="a-b", assignment_id="c",
                                  goal="First independent scope", **arguments)
        original = Path(first["brief"]).read_bytes()
        _, second = factory.launch(self.root / "a.json", task="a", assignment_id="b-c",
                                   goal="Second independent scope", **arguments)
        self.assertNotEqual(first["brief"], second["brief"])
        self.assertEqual(Path(first["brief"]).read_bytes(), original)
        self.assertIn("Second independent scope", Path(second["brief"]).read_text())

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

    def test_wrong_create_ids_keep_pending_intent_and_reconcile_without_relaunch(self):
        original_call = FakeRex.call
        def wrong_ids(rex, *args, **kwargs):
            response: dict = copy.deepcopy(original_call(rex, *args, **kwargs))
            if args[0] == "new":
                response["initial_windows"][0]["block_ids"] = ["wrong-block"]
            elif args[:2] == ("window", "new"):
                response["window_id"] = "wrong-window"
            return response
        for identity, role in (("qa", "qa"), ("security", "security")):
            with patch.object(FakeRex, "call", wrong_ids):
                with self.assertRaisesRegex(factory.FactoryError, "outcome uncertain.*pane IDs differ"):
                    self.start(assignment_id=identity, role=role)
            pending = factory.load_task(self.path)["assignments"][identity]
            self.assertEqual(pending["state"], "launching")
            self.assertNotIn("block_id", pending)
            self.assertNotIn("window_id", pending)
            _, assignment = self.start(assignment_id=identity, role=role)
            self.assertEqual(assignment["state"], "launched")
        self.assertEqual(len(self.creation_calls()), 2)

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

    def test_duplicate_task_keys_refuse_mutation_without_losing_history(self):
        self.start()
        text = self.path.read_text()
        self.path.write_text(text.replace('"assignments": {', '"assignments": {}, "assignments": {', 1))
        damaged = self.path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "restore the original task record"):
            factory.hold_task(self.path, "Review")
        self.assertEqual(self.path.read_bytes(), damaged)

    def test_pending_launch_cannot_retain_any_pane_ids(self):
        record, _ = self.start()
        record["assignments"]["qa"]["state"] = "launching"
        factory.save(self.path, record)
        with self.assertRaisesRegex(factory.FactoryError, "Pending launch has pane IDs"):
            self.start()
        self.assertEqual(len(self.creation_calls()), 1)

    def test_persisted_assignments_cannot_alias_a_pane_or_marker(self):
        self.start()
        original, _ = self.start(assignment_id="security", role="security")
        for key in ("window_label", "window_id", "block_id"):
            record = copy.deepcopy(original)
            record["assignments"]["security"][key] = record["assignments"]["qa"][key]
            factory.save(self.path, record)
            with self.subTest(key=key), self.assertRaisesRegex(factory.FactoryError,
                    "Duplicate assignment pane|window marker differs"):
                factory.load_task(self.path)

    def test_complete_pane_tuple_swaps_and_cross_task_session_identity_refuse_before_rex(self):
        self.start()
        original, _ = self.start(assignment_id="security", role="security")
        swapped = copy.deepcopy(original)
        keys = ("window_label", "window_id", "block_id")
        for key in keys:
            swapped["assignments"]["qa"][key], swapped["assignments"]["security"][key] = (
                swapped["assignments"]["security"][key], swapped["assignments"]["qa"][key])
        other, _ = factory.launch(self.root / "other.json", **{**self.arguments, "task": "other"})
        crossed = copy.deepcopy(original)
        for key in ("rex", "session_label", "session_id"):
            crossed[key] = other[key]
        for record in (swapped, crossed):
            factory.save(self.path, record)
            before = self.path.read_bytes()
            FakeRex.calls.clear()
            with self.assertRaises(factory.FactoryError):
                self.start()
            self.assertEqual(FakeRex.calls, [])
            self.assertEqual(self.path.read_bytes(), before)

    def test_lost_assignment_history_refuses_another_live_launch(self):
        self.start()
        record, _ = self.start(assignment_id="security", role="security")
        del record["assignments"]["security"]
        factory.save(self.path, record)
        with self.assertRaisesRegex(factory.FactoryError, "windows differ from saved history"):
            self.start(assignment_id="backend", role="backend")
        self.assertEqual(len(self.creation_calls()), 2)

    def test_malformed_rex_views_have_actionable_compatibility_diagnostics(self):
        record, _ = self.start()
        original = copy.deepcopy(FakeRex.sessions[0])
        damaged = [[], {}, {**original, "windows": None}, {**original, "windows": [None]},
                   {**original, "windows": [{**original["windows"][0], "layers": [None]}]}]
        for value in damaged:
            rex = FakeRex(sys.executable, "unix:///test.sock")
            with patch.object(rex, "call", return_value=value), self.subTest(value=value):
                with self.assertRaisesRegex(factory.FactoryError, "inspect the pinned binary/server API"):
                    factory.check_session(rex, record, record["session_id"])


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

    def creation_calls(self):
        return [args for args in FakeRex.calls
                if args[0] == "new" or args[:2] == ("window", "new")]

    def extend(self, **changes):
        return factory.extend_allowance(self.path, **{**dict(purpose="correction", additional=1,
            expected_limit=1, request_id="approved-one", reason="Operator approved one additional correction",
            clock=lambda: 1000), **changes})

    def test_allowance_addition_preserves_scope_history_and_hold(self):
        self.admit()
        self.start(purpose="correction")
        self.report()
        factory.hold_task(self.path, "Keep this operator hold", request_id="pause-one", clock=lambda: 1000)
        original, calls = factory.load_task(self.path), list(FakeRex.calls)
        extended = self.extend()
        self.assertEqual(extended["admission"], original["admission"])
        for field in ("assignments", "inputs", "hold", "control_events", "rex", "session_id"):
            self.assertEqual(extended[field], original[field])
        self.assertEqual(factory.task_view(extended)["allowances"]["correction"], dict(limit=2, used=1, remaining=1))
        self.assertEqual(extended["allowance_events"], [dict(id="approved-one", purpose="correction",
            previous_limit=1, additional=1, reason="Operator approved one additional correction", at=1000)])
        self.assertEqual(FakeRex.calls, calls)
        self.assertEqual(self.admit()["admission"], original["admission"])
        with self.assertRaisesRegex(factory.FactoryError, "Task is paused"):
            self.start(assignment_id="correction-two", purpose="correction")
        factory.hold_task(self.path, None, request_id="resume-one", clock=lambda: 1000)
        self.start(assignment_id="correction-two", purpose="correction")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "allowance exhausted"):
            self.start(assignment_id="correction-three", purpose="correction")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(factory.task_view(factory.load_task(self.path))["allowances"]["correction"],
                         dict(limit=2, used=2, remaining=0))

    def test_allowance_replay_after_lost_reply_and_later_decisions_never_adds_twice(self):
        self.admit()
        original_save = factory.save
        def lost_reply(*args, **kwargs):
            original_save(*args, **kwargs)
            raise OSError("Reply lost after the saved addition")
        with patch.object(factory, "save", side_effect=lost_reply), self.assertRaises(OSError):
            self.extend()
        self.extend(request_id="approved-two", expected_limit=2)
        self.extend(purpose="investigation", request_id="investigation-one")
        before = self.path.read_bytes()
        replay = self.extend()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(replay["allowance_events"]), 3)
        self.assertEqual(factory.task_view(replay)["allowances"],
                         dict(correction=dict(limit=3, used=0, remaining=3), investigation=dict(limit=2, used=0, remaining=2)))
        self.assertEqual(FakeRex.calls, [])

    def test_allowance_invalid_stale_and_conflicting_requests_leave_history_unchanged(self):
        self.admit()
        self.extend()
        before = self.path.read_bytes()
        for change in (dict(request_id="stale"), dict(additional=2), dict(reason="Other decision"),
                       dict(purpose="work"), dict(purpose=[]), dict(additional=True), dict(additional=0),
                       dict(additional=-1), dict(expected_limit=True), dict(expected_limit=-1),
                       dict(request_id="../other"), dict(reason=" "), dict(clock=lambda: float("inf"))):
            request: dict = dict(change)
            if "clock" in request:
                request.update(request_id="clock-test", expected_limit=2)
            with self.subTest(change=change), self.assertRaises(factory.FactoryError):
                self.extend(**request)
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, [])

    def test_allowance_damaged_or_downgraded_history_refuses_without_rewriting(self):
        self.admit()
        original = self.extend()
        variants = []
        for change in (dict(previous_limit=0), dict(additional=True), dict(additional=-1), dict(purpose="work"),
                       dict(id="../other"), dict(reason=""), dict(at=float("nan")), dict(extra=True)):
            record = copy.deepcopy(original)
            record["allowance_events"][0].update(change)
            variants.append(record)
        record = copy.deepcopy(original)
        record["allowance_events"].append(copy.deepcopy(record["allowance_events"][0]))
        variants.extend([record, {**original, "allowance_events": None}, {**original, "schema_version": 5},
                         {key: value for key, value in original.items() if key != "allowance_events"}])
        for record in variants:
            with self.subTest(record=record):
                factory.save(self.path, record)
                before = self.path.read_bytes()
                with self.assertRaises(factory.FactoryError):
                    factory.load_task(self.path)
                self.assertEqual(self.path.read_bytes(), before)

    def test_allowance_migration_preserves_exhaustion_across_earlier_owners(self):
        self.admit()
        self.start(purpose="correction")
        original = factory.load_task(self.path)
        legacy = {key: value for key, value in copy.deepcopy(original).items() if key not in {"allowance_events", "provisioning"}}
        for assignment in legacy["assignments"].values():
            del assignment["native_collected"]
        legacy["schema_version"] = 5
        factory.save(self.path, legacy)
        migrated = factory.load_task(self.path)
        self.assertEqual(migrated, original)
        self.assertEqual(factory.task_view(migrated)["allowances"]["correction"], dict(limit=1, used=1, remaining=0))
        with self.assertRaisesRegex(factory.FactoryError, "allowance exhausted"):
            self.start(assignment_id="another", purpose="correction")
        prototype = {**legacy, "schema_version": 6, "provisioning": None}
        factory.save(self.path, prototype)
        migrated = factory.load_task(self.path)
        self.assertEqual(migrated, original)
        self.assertEqual(factory.task_view(migrated)["allowances"]["correction"], dict(limit=1, used=1, remaining=0))

    def test_allowance_cli_returns_effective_limits_without_a_provider_or_rex(self):
        self.admit()
        code, output, errors = self.cli("extend-allowance", "--task", "task", "--purpose", "correction",
            "--additional", "1", "--expected-limit", "1", "--request-id", "operator-one", "--reason", "Approved by operator")
        self.assertEqual((code, errors), (0, ""))
        result = json.loads(output)
        self.assertEqual(result["admission"]["limits"]["correction"], 1)
        self.assertEqual(result["allowances"]["correction"], dict(limit=2, used=0, remaining=2))
        self.assertEqual(FakeRex.calls, [])

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
        marker = self.repo / ".git" / factory.REPOSITORY_MARKER
        value = json.loads(marker.read_text())
        self.assertEqual(value["schema_version"], 1)
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        self.assertEqual(view["admission"]["repository_identity"], dict(
            common_dir=str((self.repo / ".git").resolve()), repository_id=value["repository_id"]))
        self.assertEqual(view["allowances"]["correction"], dict(limit=1, used=0, remaining=1))
        self.assertIsNone(view["rex"])
        self.assertEqual(FakeRex.calls, [])

    def test_repository_replacement_cannot_rebind_inputs_or_completed_results(self):
        self.admit()
        self.start()
        before = self.path.read_bytes()
        self.repo.rename(self.root / "original-repo")
        subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(self.repo)], check=True)
        self.commit("An unrelated repository")
        for action in (lambda: self.admit(),
                       lambda: factory.set_inputs(self.path, candidate="HEAD", requirements="v2"),
                       lambda: self.report(candidate="HEAD", outcome="completed"),
                       lambda: self.start(assignment_id="security", role="security")):
            FakeRex.calls.clear()
            with self.assertRaisesRegex(factory.FactoryError, "admitted repository pin"):
                action()
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(FakeRex.calls, [])

    def test_history_sharing_clone_cannot_replace_the_admitted_repository(self):
        self.admit()
        self.start()
        original = factory.load_task(self.path)
        before = self.path.read_bytes()
        preserved = self.root / "original-repository"
        self.repo.rename(preserved)
        subprocess.run(["git", "clone", "--quiet", str(preserved), str(self.repo)], check=True)
        self.commit("Replacement-only change")
        initial = original["admission"]["initial_candidate"]
        self.assertEqual(factory.git_commit(str(self.repo), initial), initial)
        self.assertFalse((self.repo / ".git" / factory.REPOSITORY_MARKER).exists())
        for action in (lambda: factory.set_inputs(self.path, candidate="HEAD", requirements="v2"),
                       lambda: self.report(candidate="HEAD", outcome="completed"),
                       lambda: self.start(assignment_id="security", role="security"), lambda: self.admit()):
            FakeRex.calls.clear()
            with self.assertRaisesRegex(factory.FactoryError, "repository (pin|marker|identity)"):
                action()
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(FakeRex.calls, [])

    def test_git_process_failures_explain_recovery_before_mutation(self):
        self.admit()
        before = self.path.read_bytes()
        for error in (FileNotFoundError("git"), subprocess.TimeoutExpired("git", 10)):
            with patch.object(factory.subprocess, "run", side_effect=error):
                for action in (lambda: factory.git_commit(str(self.repo), "HEAD"),
                               lambda: factory.git_common_dir(str(self.repo)),
                               lambda: factory.check_checkout(str(self.repo), str(self.repo), "a" * 40)):
                    with self.assertRaisesRegex(factory.FactoryError, "restore Git availability and checkout access"):
                        action()
            self.assertEqual(self.path.read_bytes(), before)

    def test_replacement_clone_cannot_borrow_a_linked_repository_marker(self):
        self.admit()
        before = self.path.read_bytes()
        original = self.root / "preserved"
        self.repo.rename(original)
        subprocess.run(["git", "clone", "--quiet", str(original), str(self.repo)], check=True)
        source = original / ".git" / factory.REPOSITORY_MARKER
        marker = self.repo / ".git" / factory.REPOSITORY_MARKER
        contents = source.read_bytes()
        for kind in ("symlink", "hardlink"):
            if kind == "symlink":
                marker.symlink_to(source)
            else:
                os.link(source, marker)
            with self.subTest(kind=kind), self.assertRaisesRegex(factory.FactoryError, "repository marker"):
                factory.set_inputs(self.path, candidate="HEAD", requirements="v2")
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(source.read_bytes(), contents)
            self.assertEqual(FakeRex.calls, [])
            marker.unlink()

    def test_repository_marker_requires_private_owner_permissions(self):
        self.admit()
        marker = self.repo / ".git" / factory.REPOSITORY_MARKER
        before = self.path.read_bytes()
        marker.chmod(0o644)
        with self.assertRaisesRegex(factory.FactoryError, "repository marker"):
            self.start()
        marker.chmod(0o600)
        with patch.object(factory.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(factory.FactoryError, "repository marker"):
                self.start()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, [])

    def test_damaged_task_entries_cannot_reset_holds_or_attempt_history(self):
        self.admit()
        self.start(purpose="correction")
        factory.hold_task(self.path, "Operator hold")
        preserved = self.state / "preserved.json"
        self.path.rename(preserved)
        before = preserved.read_bytes()
        FakeRex.calls.clear()
        for kind in ("dangling", "symlink", "directory", "fifo"):
            if kind in {"dangling", "symlink"}:
                self.path.symlink_to(self.state / "missing.json" if kind == "dangling" else preserved)
            elif kind == "directory":
                self.path.mkdir()
            else:
                os.mkfifo(self.path)
            for action in (self.admit, lambda: self.start(purpose=None), lambda: factory.load_task(self.path)):
                with self.subTest(kind=kind), self.assertRaisesRegex(factory.FactoryError, "restore access to its original state file"):
                    action()
                self.assertEqual(preserved.read_bytes(), before)
                self.assertTrue(os.path.lexists(self.path))
                self.assertEqual(FakeRex.calls, [])
            if kind == "directory":
                self.path.rmdir()
            else:
                self.path.unlink()

    def test_candidate_local_git_cannot_run_before_parent_or_wrapper_refusal(self):
        self.admit()
        self.start()
        created = self.creation_calls()[0]
        command = created[created.index("--") + 1:]
        before = self.path.read_bytes()
        effect = self.root / "unexpected-git-effect"
        helper = self.repo / "git"
        helper.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(effect)!r}).write_text('ran')\n")
        helper.chmod(0o700)
        FakeRex.calls.clear()
        environment = {**os.environ, "PATH": "." + os.pathsep + os.environ.get("PATH", os.defpath)}
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(factory.FactoryError):
                self.start(assignment_id="second")
        result = subprocess.run(command, cwd=self.repo, env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("checkout-exec:", result.stderr)
        self.assertFalse(effect.exists())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, [])

    def test_python_startup_cannot_run_unverified_checkout_code(self):
        self.admit()
        self.start()
        created = self.creation_calls()[0]
        command = created[created.index("--") + 1:]
        before = self.path.read_bytes()
        effect = self.root / "unexpected-startup-effect"
        (self.repo / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(effect)!r}).write_text('ran')\n")
        result = subprocess.run(command, cwd=self.repo, env={**os.environ, "PYTHONPATH": ".",
            "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("checkout-exec:", result.stderr)
        self.assertFalse(effect.exists())
        self.assertEqual(self.path.read_bytes(), before)

    def test_recycled_filesystem_identity_cannot_authorize_a_replacement(self):
        original = self.admit()
        common = self.repo / ".git"
        old_stat = common.stat()
        self.repo.rename(self.root / "preserved")
        subprocess.run(["git", "clone", "--quiet", str(self.root / "preserved"), str(self.repo)], check=True)
        factory.save(common / factory.REPOSITORY_MARKER,
                     dict(schema_version=1, repository_id="b" * 32))
        real_stat = Path.stat

        def recycled_stat(path, *args, **kwargs):
            return old_stat if path == common else real_stat(path, *args, **kwargs)

        before = self.path.read_bytes()
        with patch.object(Path, "stat", recycled_stat):
            self.assertEqual(common.stat().st_ino, old_stat.st_ino)
            self.assertNotEqual(factory.repository_identity(str(self.repo)), original["admission"]["repository_identity"])
            with self.assertRaisesRegex(factory.FactoryError, "repository pin"):
                factory.set_inputs(self.path, candidate="HEAD", requirements="replacement")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, [])

    def test_replacement_between_repository_and_checkout_checks_cannot_launch(self):
        self.admit()
        before = self.path.read_bytes()
        check = factory.check_repository

        def replace_after_check(admission):
            check(admission)
            self.repo.rename(self.root / "preserved")
            subprocess.run(["git", "clone", "--quiet", str(self.root / "preserved"), str(self.repo)], check=True)
            factory.save(self.repo / ".git" / factory.REPOSITORY_MARKER,
                         dict(schema_version=1, repository_id="b" * 32))

        with patch.object(factory, "check_repository", side_effect=replace_after_check):
            with self.assertRaisesRegex(factory.FactoryError, "repository identity differs"):
                self.start(purpose="correction")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, [])
        self.assertFalse((self.state / "task.qa.md").exists())

    def test_missing_or_damaged_marker_never_heals_existing_admission(self):
        self.admit()
        marker = self.repo / ".git" / factory.REPOSITORY_MARKER
        before = self.path.read_bytes()
        for content in (None, "{}", '{"schema_version":true,"repository_id":"' + "a" * 32 + '"}',
                        '{"schema_version":2,"repository_id":"' + "a" * 32 + '"}',
                        '{"schema_version":1,"schema_version":1,"repository_id":"' + "a" * 32 + '"}'):
            if content is None:
                marker.unlink()
            else:
                marker.write_text(content)
            for action in (lambda: self.admit(), lambda: self.start()):
                with self.assertRaisesRegex(factory.FactoryError, "marker"):
                    action()
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(marker.read_text() if marker.exists() else None, content)
        self.assertEqual(FakeRex.calls, [])

    def test_marker_publication_uses_existing_concurrent_winner(self):
        marker = self.repo / ".git" / factory.REPOSITORY_MARKER
        winner = dict(schema_version=1, repository_id="a" * 32)
        link = os.link

        def publish_winner(source, target):
            marker.write_text(json.dumps(winner))
            marker.chmod(0o600)
            link(source, target)

        with patch.object(factory.os, "link", side_effect=publish_winner):
            record = self.admit()
        self.assertEqual(record["admission"]["repository_identity"]["repository_id"], "a" * 32)
        self.assertEqual(json.loads(marker.read_text()), winner)
        self.assertEqual(list(marker.parent.glob(".saving-*")), [])

    def test_replacement_during_revision_reads_cannot_admit_select_or_report(self):
        commit = factory.git_commit
        for action in ("admit", "inputs", "result"):
            with self.subTest(action=action):
                if action != "admit":
                    self.admit()
                    self.start()
                before = self.path.read_bytes() if self.path.exists() else None

                def replace_after_revision(repository, revision):
                    candidate = commit(repository, revision)
                    if revision == "HEAD":
                        self.repo.rename(self.root / "preserved")
                        subprocess.run(["git", "clone", "--quiet", str(self.root / "preserved"), str(self.repo)], check=True)
                    return candidate

                with patch.object(factory, "git_commit", side_effect=replace_after_revision):
                    with self.assertRaisesRegex(factory.FactoryError, "repository (marker|identity)"):
                        if action == "admit":
                            self.admit()
                        elif action == "inputs":
                            factory.set_inputs(self.path, candidate="HEAD", requirements="v2")
                        else:
                            self.report(candidate="HEAD", outcome="completed")
                self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)
                # Restore this fixed fixture for the next independent mutation.
                shutil.rmtree(self.repo)
                (self.root / "preserved").rename(self.repo)

    def test_spawn_consumer_refuses_replacement_after_checkout_check(self):
        self.admit()
        check = factory.check_checkout

        def replace_after_check(*args, **kwargs):
            check(*args, **kwargs)
            self.repo.rename(self.root / "preserved")
            subprocess.run(["git", "clone", "--quiet", str(self.root / "preserved"), str(self.repo)], check=True)

        with patch.object(factory, "check_checkout", side_effect=replace_after_check):
            self.start(command=[sys.executable, "-c", "print('UNAUTHORIZED')"])
        created = self.creation_calls()[0]
        argv = created[created.index("--") + 1:]
        result = subprocess.run(argv, cwd=self.repo, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("repository marker", result.stderr)
        self.assertEqual(len(self.creation_calls()), 1)

    def test_spawn_consumer_executes_in_the_verified_working_directory(self):
        self.admit()
        self.start(command=[sys.executable, "-c", "print('VERIFIED')"])
        created = self.creation_calls()[0]
        argv = created[created.index("--") + 1:]
        result = subprocess.run(argv, cwd=self.repo, capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "VERIFIED\n", ""))
        for identity in ("null", "{", '{"repository_id":"a"}'):
            damaged = list(argv)
            damaged[damaged.index("--identity") + 1] = identity
            refused = subprocess.run(damaged, cwd=self.repo, capture_output=True, text=True, check=False)
            self.assertEqual((refused.returncode, refused.stdout), (1, ""))
            self.assertIn("repository pin", refused.stderr)

    def test_ambient_git_overrides_cannot_authorize_a_different_checkout(self):
        record = self.admit()
        other = self.root / "copy"
        subprocess.run(["git", "clone", "--quiet", str(self.repo), str(other)], check=True)
        before = self.path.read_bytes()
        environment = {"GIT_DIR": str(self.repo / ".git"), "GIT_WORK_TREE": str(other),
                       "GIT_INDEX_FILE": str(self.repo / ".git" / "index"),
                       "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "status.showUntrackedFiles",
                       "GIT_CONFIG_VALUE_0": "no"}
        with patch.dict(os.environ, environment):
            factory.check_repository(record["admission"])
            with self.assertRaisesRegex(factory.FactoryError, "checkout differs"):
                self.start(cwd=str(other))
            result = subprocess.run([sys.executable, str(factory.ROOT / "checkout_exec.py"),
                "--identity", json.dumps(record["admission"]["repository_identity"]),
            "--git-dir", str(factory.git_dir(str(self.repo))),
                "--candidate", record["inputs"]["candidate"], "--", sys.executable,
                "-c", "print('UNAUTHORIZED')"], cwd=other, capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("repository marker", result.stderr)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, [])

    def test_ignored_overlay_refuses_parent_and_spawn_consumer(self):
        (self.repo / ".gitignore").write_text("local_config.py\n")
        subprocess.run(["git", "-C", str(self.repo), "add", ".gitignore"], check=True)
        self.commit("Declare an ignored local configuration")
        record = self.admit()
        (self.repo / "local_config.py").write_text("VALUE = 'uncommitted'\n")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "uncommitted inputs"):
            self.start()
        result = subprocess.run([sys.executable, str(factory.ROOT / "checkout_exec.py"),
            "--identity", json.dumps(record["admission"]["repository_identity"]),
            "--git-dir", str(factory.git_dir(str(self.repo))),
            "--candidate", record["inputs"]["candidate"], "--", sys.executable,
            "-c", "import local_config; print(local_config.VALUE)"],
            cwd=self.repo, capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("uncommitted inputs", result.stderr)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, [])

    def test_spawn_consumer_does_not_pass_git_overrides_to_the_command(self):
        record = self.admit()
        other = self.root / "copy"
        subprocess.run(["git", "clone", "--quiet", str(self.repo), str(other)], check=True)
        command = [sys.executable, str(factory.ROOT / "checkout_exec.py"),
            "--identity", json.dumps(record["admission"]["repository_identity"]),
            "--git-dir", str(factory.git_dir(str(self.repo))),
            "--candidate", record["inputs"]["candidate"], "--", sys.executable, "-c",
            "import subprocess; subprocess.run(['git','branch','from-wrapper'],check=True)"]
        environment = {**os.environ, "GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
                       "GIT_INDEX_FILE": str(other / ".git" / "index")}
        result = subprocess.run(command, cwd=self.repo, env=environment, capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        for path, expected in ((self.repo, 0), (other, 1)):
            observed = subprocess.run(["git", "-C", str(path), "show-ref", "--verify", "--quiet",
                                       "refs/heads/from-wrapper"], check=False)
            self.assertEqual(observed.returncode, expected)

    def test_spawn_consumer_preserves_environment_supplied_commit_identity(self):
        record = self.admit()
        environment = {**os.environ, "GIT_AUTHOR_NAME": "Fixture Author",
            "GIT_AUTHOR_EMAIL": "author@example.invalid", "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
            "GIT_COMMITTER_NAME": "Fixture Committer", "GIT_COMMITTER_EMAIL": "committer@example.invalid",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"}
        result = subprocess.run([sys.executable, str(factory.ROOT / "checkout_exec.py"),
            "--identity", json.dumps(record["admission"]["repository_identity"]),
            "--git-dir", str(factory.git_dir(str(self.repo))),
            "--candidate", record["inputs"]["candidate"], "--", sys.executable, "-c",
            "import subprocess; subprocess.run(['git','-c','commit.gpgsign=false','-c','core.hooksPath=/dev/null','commit','--quiet','--allow-empty','-m','Identity fixture'],check=True)"],
            cwd=self.repo, env=environment, capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        identity = subprocess.check_output(["git", "-C", str(self.repo), "log", "-1", "--format=%an|%ae|%cn|%ce"], text=True).strip()
        self.assertEqual(identity, "Fixture Author|author@example.invalid|Fixture Committer|committer@example.invalid")

    def test_spawn_consumer_preserves_git_transport_and_prompt_settings(self):
        record = self.admit()
        settings = {"GIT_SSH_COMMAND": "ssh -o BatchMode=yes", "GIT_ASKPASS": "/fixture/askpass",
                    "GIT_SSL_CAINFO": "/fixture/ca.pem", "GIT_TERMINAL_PROMPT": "0"}
        result = subprocess.run([sys.executable, str(factory.ROOT / "checkout_exec.py"),
            "--identity", json.dumps(record["admission"]["repository_identity"]),
            "--git-dir", str(factory.git_dir(str(self.repo))),
            "--candidate", record["inputs"]["candidate"], "--", sys.executable, "-c",
            "import json,os,sys; expected=json.loads(sys.argv[1]); sys.exit(0 if all(os.environ.get(k)==v for k,v in expected.items()) else 2)",
            json.dumps(settings)], cwd=self.repo, env={**os.environ, **settings},
            capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stderr), (0, ""))

    def test_spawn_consumer_preserves_configured_credential_helpers(self):
        record = self.admit()
        helper = self.root / "credential-fixture.py"
        helper.write_text("import sys\nsys.stdin.read()\nprint('username=fixture\\npassword=fixture')\n")
        value = "!" + shlex.quote(sys.executable) + " " + shlex.quote(str(helper))
        pairs = [("credential.helper", ""), ("credential.helper", value)]
        quote = lambda text: "'" + text.replace("'", "'\\''") + "'"
        auth_file = self.root / "auth-config"
        for key, item in pairs:
            subprocess.run(["git", "config", "--file", str(auth_file), "--add", key, item], check=True)
        settings = [dict(GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0=pairs[0][0], GIT_CONFIG_VALUE_0="",
                         GIT_CONFIG_KEY_1=pairs[1][0], GIT_CONFIG_VALUE_1=value),
                    dict(GIT_CONFIG_PARAMETERS=" ".join(quote(k) + "=" + quote(v) for k, v in pairs)),
                    dict(GIT_CONFIG_GLOBAL=str(auth_file))]
        command = [sys.executable, str(factory.ROOT / "checkout_exec.py"),
            "--identity", json.dumps(record["admission"]["repository_identity"]),
            "--git-dir", str(factory.git_dir(str(self.repo))),
            "--candidate", record["inputs"]["candidate"], "--", sys.executable, "-c",
            "import subprocess; p=subprocess.run(['git','-c','credential.interactive=never','credential','fill'],input='protocol=fixture\\nhost=example.invalid\\n\\n',capture_output=True,text=True,check=True); assert 'username=fixture\\n' in p.stdout and 'password=fixture\\n' in p.stdout"]
        for index, extra in enumerate(settings):
            environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
            environment.update(extra)
            result = subprocess.run(command, cwd=self.repo, env=environment, capture_output=True, text=True, check=False)
            with self.subTest(format=index):
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))

    def test_spawn_consumer_refuses_source_routing_configuration(self):
        record = self.admit()
        command = [sys.executable, str(factory.ROOT / "checkout_exec.py"),
            "--identity", json.dumps(record["admission"]["repository_identity"]),
            "--git-dir", str(factory.git_dir(str(self.repo))),
            "--candidate", record["inputs"]["candidate"], "--", sys.executable, "-c", "print('UNAUTHORIZED')"]
        environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
            GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.worktree", GIT_CONFIG_VALUE_0=str(self.repo))
        result = subprocess.run(command, cwd=self.repo, env=environment, capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("Inherited Git configuration contains source-routing options", result.stderr)

    def test_checkout_guards_do_not_execute_configured_clean_filters(self):
        source = self.repo / "sample.audit"
        source.write_text("committed\n")
        (self.repo / ".gitattributes").write_text("*.audit filter=audit\n")
        effect = self.root / "unexpected-filter-effect"
        (self.repo / "filter.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            f"Path({str(effect)!r}).write_text('called')\n"
            "sys.stdout.write(sys.stdin.read())\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "--all"], check=True)
        self.commit("Identity filter fixture")
        subprocess.run(["git", "-C", str(self.repo), "config", "filter.audit.clean",
                        shlex.quote(sys.executable) + " -B filter.py"], check=True)
        record = self.admit()
        os.utime(source, ns=(946684800000000000, 946684800000000000))
        self.start()
        result = subprocess.run([sys.executable, "-B", str(factory.ROOT / "checkout_exec.py"),
            "--identity", json.dumps(record["admission"]["repository_identity"]),
            "--git-dir", str(factory.git_dir(str(self.repo))),
            "--candidate", record["inputs"]["candidate"], "--", sys.executable,
            "-c", "print('verified')"], cwd=self.repo, capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stdout), (0, "verified\n"), result.stderr)
        self.assertFalse(effect.exists())
        # Establish that the fixture really executes on a stat-invalidated status.
        self.assertEqual(factory.git_run(str(self.repo), "status", "--porcelain").stdout, "")
        self.assertTrue(effect.exists())

    def test_stat_hints_cannot_hide_changed_source_bytes(self):
        source = self.repo / "source.txt"
        source.write_bytes(b"committed\n")
        os.utime(source, ns=(946684800000000000, 946684800000000000))
        subprocess.run(["git", "-C", str(self.repo), "add", "source.txt"], check=True)
        self.commit("Tracked source")
        record = self.admit()
        for key, value in (("core.trustctime", "false"), ("core.checkStat", "minimal")):
            subprocess.run(["git", "-C", str(self.repo), "config", key, value], check=True)
        source.write_bytes(b"concealed\n")
        os.utime(source, ns=(946684800000000000, 946684800000000000))
        self.assertEqual(subprocess.check_output(["git", "-C", str(self.repo), "status", "--porcelain"]), b"")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "source bytes or modes differ"):
            self.start()
        result = subprocess.run([sys.executable, str(factory.ROOT / "checkout_exec.py"),
            "--identity", json.dumps(record["admission"]["repository_identity"]),
            "--git-dir", str(factory.git_dir(str(self.repo))),
            "--candidate", record["inputs"]["candidate"], "--", sys.executable,
            "-c", "print('must not launch')"], cwd=self.repo, capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("source bytes or modes differ", result.stderr)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, [])

    def test_filemode_config_cannot_hide_changed_executable_mode(self):
        source = self.repo / "source.txt"
        source.write_bytes(b"committed\n")
        source.chmod(0o755)
        subprocess.run(["git", "-C", str(self.repo), "add", "source.txt"], check=True)
        self.commit("Executable source")
        self.admit()
        subprocess.run(["git", "-C", str(self.repo), "config", "core.filemode", "false"], check=True)
        source.chmod(0o644)
        self.assertEqual(subprocess.check_output(["git", "-C", str(self.repo), "status", "--porcelain"]), b"")
        with self.assertRaisesRegex(factory.FactoryError, "source bytes or modes differ"):
            self.start()
        self.assertEqual(FakeRex.calls, [])

    def assert_source_refuses_at_both_launch_boundaries(self, record,
            error="uncommitted inputs|source bytes or modes differ"):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, error):
            self.start()
        result = subprocess.run([sys.executable, str(factory.ROOT / "checkout_exec.py"),
            "--identity", json.dumps(record["admission"]["repository_identity"]),
            "--git-dir", str(factory.git_dir(str(self.repo))),
            "--candidate", record["inputs"]["candidate"], "--", sys.executable,
            "-c", "print('must not launch')"], cwd=self.repo, capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertRegex(result.stderr, error)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, [])

    def test_replacement_commit_cannot_substitute_selected_source(self):
        source = self.repo / "source.txt"
        source.write_text("original\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "source.txt"], check=True)
        self.commit("Original source")
        original = factory.git_commit(str(self.repo), "HEAD")
        record = self.admit()
        source.write_text("substitute\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "source.txt"], check=True)
        self.commit("Substitute source")
        substitute = factory.git_commit(str(self.repo), "HEAD")
        subprocess.run(["git", "-C", str(self.repo), "reset", "--soft", original], check=True)
        subprocess.run(["git", "-C", str(self.repo), "replace", original, substitute], check=True)
        self.assertEqual(subprocess.check_output(["git", "-C", str(self.repo), "status", "--porcelain"]), b"")
        self.assert_source_refuses_at_both_launch_boundaries(record)

    def test_child_git_reads_unreplaced_candidate_in_a_clean_checkout(self):
        source = self.repo / "source.txt"
        source.write_text("original\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "source.txt"], check=True)
        self.commit("Original source")
        original = factory.git_commit(str(self.repo), "HEAD")
        source.write_text("substitute\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "source.txt"], check=True)
        self.commit("Substitute source")
        substitute = factory.git_commit(str(self.repo), "HEAD")
        subprocess.run(["git", "-C", str(self.repo), "reset", "--quiet", "--hard", original], check=True)
        subprocess.run(["git", "-C", str(self.repo), "replace", original, substitute], check=True)
        self.assertEqual(subprocess.check_output(["git", "-C", str(self.repo), "show", "HEAD:source.txt"]), b"substitute\n")
        self.admit()
        self.start(command=[sys.executable, "-c", "import subprocess; subprocess.run(['git','show','HEAD:source.txt'],check=True)"])
        call = self.creation_calls()[0]
        result = subprocess.run(call[call.index("--") + 1:], cwd=self.repo, capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "original\n", ""))

    def test_spawn_consumer_cannot_substitute_another_linked_worktree(self):
        self.admit()
        other = self.root / "other-worktree"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "--quiet", "--detach", str(other), "HEAD"], check=True)
        self.assertEqual(factory.repository_identity(str(self.repo)), factory.repository_identity(str(other)))
        self.assertNotEqual(factory.git_dir(str(self.repo)), factory.git_dir(str(other)))
        self.start(command=[sys.executable, "-c", "print('UNAUTHORIZED')"])
        call = self.creation_calls()[0]
        result = subprocess.run(call[call.index("--") + 1:], cwd=other, capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("Assignment worktree differs", result.stderr)

    def test_warmed_untracked_cache_cannot_hide_added_input(self):
        package = self.repo / "package"
        package.mkdir()
        (package / "source.py").write_text("VALUE = 'committed'\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "package"], check=True)
        self.commit("Package source")
        record = self.admit()
        for key, value in (("core.untrackedCache", "true"), ("core.trustctime", "false"),
                           ("core.checkStat", "minimal")):
            subprocess.run(["git", "-C", str(self.repo), "config", key, value], check=True)
        subprocess.run(["git", "-C", str(self.repo), "update-index", "--untracked-cache"], check=True)
        fixed = 946684800000000000
        for path in (package, self.repo):
            os.utime(path, ns=(fixed, fixed))
        for _ in range(2):
            self.assertEqual(subprocess.check_output(["git", "-C", str(self.repo), "status", "--porcelain"]), b"")
        (package / "local_config.py").write_text("VALUE = 'uncommitted'\n")
        for path in (package, self.repo):
            os.utime(path, ns=(fixed, fixed))
        self.assert_source_refuses_at_both_launch_boundaries(record)

    def test_empty_untracked_directory_cannot_change_revision_bound_inputs(self):
        package = self.repo / "package"
        package.mkdir()
        (package / "source.py").write_text("VALUE = 'committed'\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "package"], check=True)
        self.commit("Package source")
        record = self.admit()
        (package / "feature.enabled").mkdir()
        self.assertEqual(subprocess.check_output(["git", "-C", str(self.repo), "status", "--porcelain"]), b"")
        self.assert_source_refuses_at_both_launch_boundaries(record)

    def test_source_symlinks_cannot_import_external_or_git_metadata_inputs(self):
        external = self.root / "external-config"
        external.write_text("unversioned\n")
        (self.repo / "source.py").write_text("VALUE = 'committed'\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "source.py"], check=True)
        self.commit("Regular source")
        self.admit()
        link = self.repo / "input"
        for index, target in enumerate((str(external), ".git/config", "source.py")):
            if link.is_symlink():
                link.unlink()
            link.symlink_to(target)
            subprocess.run(["git", "-C", str(self.repo), "add", "input"], check=True)
            self.commit("Symlink fixture " + str(index))
            record = factory.set_inputs(self.path, candidate=factory.git_commit(str(self.repo), "HEAD"),
                                        requirements="link-fixture-" + str(index))
            self.assertEqual(subprocess.check_output(["git", "-C", str(self.repo), "status", "--porcelain"]), b"")
            with self.subTest(target=target):
                self.assert_source_refuses_at_both_launch_boundaries(record, "source contains symlinks")

    def test_shared_hard_link_cannot_alias_another_checkouts_source(self):
        source = self.repo / "source.py"
        source.write_text("VALUE = 'committed'\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "source.py"], check=True)
        self.commit("Regular source")
        self.admit()
        outside = self.root / "another-checkouts-source.py"
        os.link(source, outside)
        self.assertEqual(subprocess.check_output(["git", "-C", str(self.repo), "status", "--porcelain"]), b"")
        self.start(command=[sys.executable, "-c", "from pathlib import Path; Path('source.py').write_text('owned change\\n')"])
        self.assertNotEqual(source.stat().st_ino, outside.stat().st_ino)
        source.unlink()
        os.link(outside, source)  # Exercise the executable consumer boundary too.
        call = self.creation_calls()[0]
        result = subprocess.run(call[call.index("--") + 1:], cwd=self.repo,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(source.read_text(), "owned change\n")
        self.assertEqual(outside.read_text(), "VALUE = 'committed'\n")
        self.assertNotEqual(source.stat().st_ino, outside.stat().st_ino)

    def test_configured_worktree_cannot_hide_changes_in_the_actual_checkout(self):
        source = self.repo / "source.txt"
        source.write_text("committed\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "source.txt"], check=True)
        self.commit("Tracked source")
        record = self.admit()
        other = self.root / "repo "  # A real distinct path whose trailing space must survive config reads.
        subprocess.run(["git", "clone", "--quiet", str(self.repo), str(other)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "core.worktree", str(other)], check=True)
        source.write_text("not committed\n")
        before = self.path.read_bytes()
        self.assertEqual(factory.git_run(str(self.repo), "rev-parse", "--show-toplevel").stdout.strip(), str(self.repo.resolve()))
        with self.assertRaisesRegex(factory.FactoryError, "source bytes or modes differ"):
            self.start()
        result = subprocess.run([sys.executable, str(factory.ROOT / "checkout_exec.py"),
            "--identity", json.dumps(record["admission"]["repository_identity"]),
            "--git-dir", str(factory.git_dir(str(self.repo))),
            "--candidate", record["inputs"]["candidate"], "--", sys.executable,
            "-c", "print('UNAUTHORIZED')"], cwd=self.repo, capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("source bytes or modes differ", result.stderr)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, [])

        source.write_text("committed\n")
        with self.assertRaisesRegex(factory.FactoryError, "restore core.worktree"):
            self.start()

    def test_hidden_index_flags_refuse_even_from_a_checkout_subdirectory(self):
        source = self.repo / "source.txt"
        source.write_text("committed\n")
        (self.repo / "subdir").mkdir()
        subprocess.run(["git", "-C", str(self.repo), "add", "source.txt"], check=True)
        self.commit("Tracked source")
        self.admit()
        before = self.path.read_bytes()
        for flag in ("assume-unchanged", "skip-worktree"):
            subprocess.run(["git", "-C", str(self.repo), "update-index", "--" + flag, "source.txt"], check=True)
            source.write_text("hidden edit\n")
            with self.assertRaisesRegex(factory.FactoryError, "clear assume-unchanged/skip-worktree"):
                self.start(cwd=str(self.repo / "subdir"))
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(FakeRex.calls, [])
            subprocess.run(["git", "-C", str(self.repo), "update-index", "--no-" + flag, "source.txt"], check=True)

    def test_submodules_refuse_even_when_ignored_content_is_hidden(self):
        module_source = self.root / "module-source"
        subprocess.run(["git", "clone", "--quiet", str(self.repo), str(module_source)], check=True)
        (module_source / "source.txt").write_text("committed\n")
        (module_source / ".gitignore").write_text("local_config.py\n")
        subprocess.run(["git", "-C", str(module_source), "add", "source.txt", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(module_source), "-c", "user.name=Fixture",
            "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
            "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Module source"],
            check=True, env={**os.environ, "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
                            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"})
        subprocess.run(["git", "-C", str(self.repo), "-c", "protocol.file.allow=always",
                         "submodule", "add", "--quiet", str(module_source), "module"], check=True)
        self.commit("Include local fixture submodule")
        self.admit()
        subprocess.run(["git", "-C", str(self.repo), "config", "submodule.module.ignore", "all"], check=True)
        before = self.path.read_bytes()
        for overlay in ("clean", "dirty", "ignored"):
            if overlay == "dirty":
                (self.repo / "module" / "source.txt").write_text("dirty module\n")
            elif overlay == "ignored":
                (self.repo / "module" / "source.txt").write_text("committed\n")
                (self.repo / "module" / "local_config.py").write_text("VALUE = 'ignored'\n")
            with self.subTest(overlay=overlay), self.assertRaisesRegex(factory.FactoryError, "checkout without submodules"):
                self.start()
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(FakeRex.calls, [])

    def test_fsmonitor_cannot_hide_an_edited_tracked_file(self):
        source = self.repo / "source.txt"
        source.write_text("committed\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "source.txt"], check=True)
        self.commit("Tracked source")
        self.admit()
        helper = self.root / "fsmonitor-helper"
        helper.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.buffer.write(b'fixture-token\\0')\n")
        helper.chmod(0o700)
        subprocess.run(["git", "-C", str(self.repo), "config", "core.fsmonitor", str(helper)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "update-index", "--fsmonitor"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "update-index", "--fsmonitor-valid", "source.txt"], check=True)
        source.write_text("hidden edit\n")
        flags = subprocess.check_output(["git", "-C", str(self.repo), "ls-files", "-f"], text=True)
        self.assertTrue(flags.startswith("h "))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "disable core.fsmonitor"):
            self.start()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, [])

    def test_malformed_git_process_output_explains_encoding_recovery(self):
        self.admit()
        before = self.path.read_bytes()
        error = UnicodeDecodeError("utf-8", b"?? bad-\xff\n", 7, 8, "invalid start byte")
        with patch.object(factory.subprocess, "run", side_effect=error):
            with self.assertRaisesRegex(factory.FactoryError, "Cannot decode Git output; inspect Git/checkout filename encoding"):
                self.start()
        self.assertEqual(self.path.read_bytes(), before)
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

    def test_explicit_limits_work_without_defaults_and_bad_defaults_explain_recovery(self):
        config_root = self.root / "config-fixture"
        (config_root / "config").mkdir(parents=True)
        config = config_root / "config/coordination.json"
        for data in (None, b'{"broken":', b'\xff', b'[]', b'{}', b'{"policies": {}}',
                     b'{"policies": {"max_investigations": true}}'):
            self.path.unlink(missing_ok=True)
            if data is None:
                config.unlink(missing_ok=True)
            else:
                config.write_bytes(data)
            with patch.object(factory, "ROOT", config_root):
                with self.subTest(data=data), self.assertRaisesRegex(factory.FactoryError,
                        "repair its policies or supply both --corrections and --investigations"):
                    self.admit(corrections=1, investigations=None)
                self.assertFalse(self.path.exists())
                record = self.admit(corrections=1, investigations=2)
                self.assertEqual(record["admission"]["limits"], dict(correction=1, investigation=2))
        self.path.unlink()
        config.write_text('{"policies": {"max_investigations": 3}}')
        with patch.object(factory, "ROOT", config_root):
            record = self.admit(corrections=1, investigations=None)
        self.assertEqual(record["admission"]["limits"], dict(correction=1, investigation=3))

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
        for key in ("schema_version", "admission", "hold", "inputs", "control_events", "allowance_events", "provisioning"):
            del record[key]
        for assignment in record["assignments"].values():
            del assignment["result"]
            del assignment["native_collected"]
        self.path.write_text(json.dumps(record))
        migrated = factory.load_task(self.path)
        self.assertEqual(migrated["schema_version"], 9)
        self.assertIsNone(factory.task_view(migrated)["allowances"])
        self.assertEqual(migrated["assignments"],
                         {key: {**item, "result": None, "native_collected": False} for key, item in record["assignments"].items()})
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

    def report(self, **changes):
        record = json.loads(self.path.read_text())
        arguments = dict(assignment_id="qa", candidate=record["admission"]["initial_candidate"],
                         requirements=record["admission"]["initial_candidate"], outcome="approved",
                         artifacts=["evidence/qa.txt"])
        return factory.record_result(self.path, **{**arguments, **changes})

    def test_result_records_exact_inputs_and_does_not_claim_task_acceptance(self):
        initial = self.admit()["inputs"]
        _, assignment = self.start()
        self.assertEqual(assignment["request"]["inputs"], initial)
        self.assertIn(initial["candidate"], Path(assignment["brief"]).read_text())
        FakeRex.calls.clear()
        record = self.report()
        self.assertEqual(record["assignments"]["qa"]["result"],
                         dict(inputs=initial, candidate=initial["candidate"], outcome="approved",
                              artifacts=["evidence/qa.txt"]))
        self.assertTrue(factory.task_view(record)["assignments"]["qa"]["result_current"])
        self.assertNotIn("accepted", factory.task_view(record))
        self.assertEqual(FakeRex.calls, [])

    def test_candidate_or_requirements_change_invalidates_result_without_erasing_it(self):
        initial = self.admit()["inputs"]
        self.start()
        self.report()
        self.commit("Changed candidate")
        changed = factory.set_inputs(self.path, candidate="HEAD", requirements=initial["requirements"])
        self.assertNotEqual(changed["inputs"]["candidate"], initial["candidate"])
        self.assertFalse(factory.task_view(changed)["assignments"]["qa"]["result_current"])
        self.assertEqual(changed["assignments"]["qa"]["result"]["inputs"], initial)
        # Reconnecting preserves the original assignment and brief, even after inputs advance.
        _, reconnected = self.start()
        self.assertEqual(reconnected["request"]["inputs"], initial)
        requirements_changed = factory.set_inputs(self.path, candidate=initial["candidate"],
                                                   requirements="requirements-v2")
        self.assertFalse(factory.task_view(requirements_changed)["assignments"]["qa"]["result_current"])

    def test_late_result_stays_historical_and_new_assignment_binds_current_inputs(self):
        initial = self.admit()["inputs"]
        self.start()
        factory.set_inputs(self.path, candidate="HEAD", requirements="requirements-v2")
        late = self.report()
        self.assertFalse(factory.task_view(late)["assignments"]["qa"]["result_current"])
        self.assertEqual(late["assignments"]["qa"]["result"]["inputs"], initial)
        _, current = self.start(assignment_id="qa-v2")
        self.assertEqual(current["request"]["inputs"]["requirements"], "requirements-v2")
        completed = self.report(assignment_id="qa-v2", requirements="requirements-v2")
        self.assertTrue(factory.task_view(completed)["assignments"]["qa-v2"]["result_current"])

    def test_mismatched_or_empty_result_is_refused_without_changing_state(self):
        self.admit()
        self.start()
        self.commit("Different candidate")
        before = self.path.read_bytes()
        for changes in ({"candidate": "HEAD"}, {"requirements": "different"},
                        {"candidate": "not-a-commit"}, {"artifacts": []},
                        {"artifacts": [" "]}, {"outcome": "looks-fine"}):
            with self.subTest(changes=changes), self.assertRaises(factory.FactoryError):
                self.report(**changes)
            self.assertEqual(self.path.read_bytes(), before)

    def test_result_resubmission_is_idempotent_and_conflicting_report_is_refused(self):
        self.admit()
        self.start()
        first = self.report()
        self.assertEqual(self.report(), first)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "different result"):
            self.report(outcome="findings")
        self.assertEqual(self.path.read_bytes(), before)

    def test_uncertain_launch_cannot_gain_a_result_before_reconciliation(self):
        self.admit()
        FakeRex.fail_after_create = True
        with self.assertRaises(factory.FactoryError):
            self.start()
        with self.assertRaisesRegex(factory.FactoryError, "Reconcile the pending launch"):
            self.report()
        FakeRex.fail_after_create = False
        self.start()
        self.report()

    def test_version_one_upgrade_never_invents_assignment_revision_evidence(self):
        self.admit()
        self.start()
        record = json.loads(self.path.read_text())
        record["schema_version"] = 1
        record.pop("control_events", None)
        record.pop("provisioning", None)
        record.pop("allowance_events", None)
        del record["admission"]["repository_identity"]
        del record["inputs"]
        del record["assignments"]["qa"]["request"]["inputs"]
        del record["assignments"]["qa"]["result"]
        del record["assignments"]["qa"]["native_collected"]
        self.path.write_text(json.dumps(record))
        upgraded = factory.load_task(self.path)
        self.assertEqual(upgraded["schema_version"], 9)
        self.assertIsNone(upgraded["assignments"]["qa"]["result"])
        self.assertNotIn("inputs", upgraded["assignments"]["qa"]["request"])
        with self.assertRaisesRegex(factory.FactoryError, "predates input binding"):
            self.report()

    def test_version_two_upgrade_preserves_history_without_inventing_repository_identity(self):
        self.admit()
        self.start(purpose="correction")
        self.report()
        factory.hold_task(self.path, "Operator hold")
        original = factory.load_task(self.path)
        legacy = copy.deepcopy(original)
        legacy["schema_version"] = 2
        for assignment in legacy["assignments"].values():
            del assignment["native_collected"]
        legacy.pop("control_events", None)
        legacy.pop("provisioning", None)
        legacy.pop("allowance_events", None)
        del legacy["admission"]["repository_identity"]
        factory.save(self.path, legacy)
        with patch.object(factory, "repository_identity", side_effect=AssertionError("No historical pin can be inferred")):
            upgraded = factory.load_task(self.path)
        self.assertEqual(upgraded["schema_version"], 9)
        self.assertIsNone(upgraded["admission"]["repository_identity"])
        self.assertEqual(upgraded["assignments"], original["assignments"])
        self.assertEqual(upgraded["hold"], original["hold"])
        self.assertEqual(factory.task_view(upgraded)["allowances"]["correction"]["remaining"], 0)
        factory.hold_task(self.path, None)
        before = self.path.read_bytes()
        for action in (lambda: factory.set_inputs(self.path, candidate="HEAD", requirements="v2"),
                       lambda: self.report(),
                       lambda: self.start(assignment_id="security", role="security")):
            FakeRex.calls.clear()
            with self.assertRaisesRegex(factory.FactoryError, "operator authorize a new admission"):
                action()
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(FakeRex.calls, [])

    def test_damaged_repository_identity_and_version_downgrade_refuse_without_rewriting(self):
        original = self.admit()
        missing = copy.deepcopy(original)
        del missing["admission"]["repository_identity"]
        mistyped = copy.deepcopy(original)
        mistyped["admission"]["repository_identity"]["repository_id"] = True
        downgraded = {**copy.deepcopy(original), "schema_version": 2}
        version_three = {**copy.deepcopy(original), "schema_version": 3}
        for record in (missing, mistyped, downgraded, version_three):
            factory.save(self.path, record)
            before = self.path.read_bytes()
            with self.assertRaises(factory.FactoryError):
                factory.load_task(self.path)
            self.assertEqual(self.path.read_bytes(), before)

    def test_version_three_filesystem_pin_is_preserved_only_for_inspection(self):
        self.admit()
        self.start(purpose="correction")
        self.report()
        factory.hold_task(self.path, "Keep original hold")
        legacy = factory.load_task(self.path)
        legacy["schema_version"] = 3
        for assignment in legacy["assignments"].values():
            del assignment["native_collected"]
        legacy.pop("control_events", None)
        legacy.pop("provisioning", None)
        legacy.pop("allowance_events", None)
        pin = dict(common_dir=str(self.repo / ".git"), device=42, inode=17)
        legacy["admission"]["repository_identity"] = pin
        factory.save(self.path, legacy)
        with patch.object(factory, "repository_identity", side_effect=AssertionError("No inferred historical identity")):
            upgraded = factory.load_task(self.path)
        self.assertEqual(upgraded["schema_version"], 9)
        self.assertIsNone(upgraded["admission"]["repository_identity"])
        self.assertEqual(upgraded["admission"]["legacy_repository_identity"], pin)
        self.assertEqual(upgraded["assignments"],
                         {key: {**item, "native_collected": False} for key, item in legacy["assignments"].items()})
        self.assertEqual(upgraded["hold"], legacy["hold"])
        self.assertEqual(factory.task_view(upgraded)["allowances"]["correction"]["remaining"], 0)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "operator authorize a new admission"):
            factory.set_inputs(self.path, candidate="HEAD", requirements="v2")
        self.assertEqual(self.path.read_bytes(), before)

    def test_damaged_version_cannot_downgrade_or_erase_current_evidence(self):
        self.admit()
        self.start()
        self.report()
        factory.set_inputs(self.path, candidate="HEAD", requirements="changed-requirements")
        original = json.loads(self.path.read_text())
        for remove in ((), ("inputs",)):
            record = copy.deepcopy(original)
            record["schema_version"] = 1
            for key in remove:
                del record[key]
            self.path.write_text(json.dumps(record))
            before = self.path.read_bytes()
            result, output, errors = self.cli("show", "--task", "task")
            self.assertEqual((result, output), (1, ""))
            self.assertIn("restore its schema version", errors)
            self.assertEqual(self.path.read_bytes(), before)

    def test_damaged_request_or_result_cannot_authorize_downstream_launch(self):
        self.admit()
        self.start()
        self.report()
        original = json.loads(self.path.read_text())
        variants = []
        for field in ("role", "goal", "cwd", "command", "instructions_sha256"):
            record = copy.deepcopy(original)
            del record["assignments"]["qa"]["request"][field]
            variants.append(record)
        for refs in ([None], [""], [" "]):
            record = copy.deepcopy(original)
            record["assignments"]["qa"]["result"]["artifacts"] = refs
            variants.append(record)
        FakeRex.calls.clear()
        for record in variants:
            self.path.write_text(json.dumps(record))
            before = self.path.read_bytes()
            with self.subTest(record=record), self.assertRaises(factory.FactoryError):
                self.start(assignment_id="security", role="security", dependencies=["qa"])
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, [])

    def test_cli_input_selection_and_result_submission(self):
        self.admit()
        result, output, errors = self.cli("inputs", "--task", "task", "--candidate", "HEAD",
                                          "--requirements", "requirements-v1")
        self.assertEqual((result, errors), (0, ""))
        candidate = json.loads(output)["inputs"]["candidate"]
        self.start()
        result, output, errors = self.cli("result", "--task", "task", "--id", "qa",
                                          "--candidate", candidate, "--requirements", "requirements-v1",
                                          "--outcome", "findings", "--artifact", "evidence/finding.md")
        self.assertEqual((result, errors), (0, ""))
        self.assertEqual(json.loads(output)["assignments"]["qa"]["result"]["outcome"], "findings")

    def test_dependency_requires_current_successful_result_before_any_external_call(self):
        self.admit()
        self.start()
        FakeRex.calls.clear()
        with self.assertRaisesRegex(factory.FactoryError, "successful result on current inputs"):
            self.start(assignment_id="security", role="security", dependencies=["qa"])
        self.assertEqual(FakeRex.calls, [])
        self.report()
        self.start(assignment_id="security", role="security", dependencies=["qa"])
        self.assertEqual(len(FakeRex.sessions[0]["windows"]), 2)
        factory.set_inputs(self.path, candidate="HEAD", requirements="requirements-v2")
        FakeRex.calls.clear()
        with self.assertRaises(factory.FactoryError):
            self.start(assignment_id="other", dependencies=["qa"])
        self.assertEqual(FakeRex.calls, [])

    def test_findings_and_unknown_dependencies_do_not_advance_work(self):
        self.admit()
        self.start()
        self.report(outcome="findings")
        FakeRex.calls.clear()
        for dependencies in (["qa"], ["missing"], ["security"]):
            with self.subTest(dependencies=dependencies), self.assertRaises(factory.FactoryError):
                self.start(assignment_id="security", role="security", dependencies=dependencies)
        self.assertEqual(FakeRex.calls, [])

    def test_completed_implementation_can_report_produced_candidate_for_downstream_review(self):
        initial = self.admit()["inputs"]
        self.start(assignment_id="backend", role="backend")
        self.commit("Produced implementation")
        completed = self.report(assignment_id="backend", candidate="HEAD", outcome="completed",
                                artifacts=["git:HEAD"])
        output = completed["assignments"]["backend"]["result"]["candidate"]
        self.assertNotEqual(output, initial["candidate"])
        self.assertFalse(factory.task_view(completed)["assignments"]["backend"]["result_current"])
        factory.set_inputs(self.path, candidate=output, requirements=initial["requirements"])
        _, reviewer = self.start(dependencies=["backend"])
        self.assertEqual(reviewer["request"]["inputs"]["candidate"], output)
        reviewed = self.report(candidate=output)
        self.assertTrue(factory.task_view(reviewed)["assignments"]["qa"]["result_current"])

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
        self.assertIn("Unknown task 'missing'", errors)
        self.assertIn("create it with admit/start", errors)
        self.assertFalse((self.state / "missing.json").exists())
        self.assertEqual(FakeRex.calls, [])

    def test_damaged_admission_scope_cannot_launch_or_rewrite_state(self):
        original = self.admit()
        for key in ("goal", "repository", "acceptance", "initial_candidate"):
            admission = {name: value for name, value in original["admission"].items() if name != key}
            self.path.write_text(json.dumps({**original, "admission": admission}))
            before = self.path.read_bytes()
            with self.subTest(missing=key), self.assertRaisesRegex(factory.FactoryError, "Invalid task admission"):
                self.start()
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, [])

    def test_replaced_checkout_refuses_before_reserving_an_attempt_or_contacting_rex(self):
        self.admit()
        before = self.path.read_bytes()
        self.repo.rename(self.root / "original-repository")
        subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(self.repo)], check=True)
        self.commit("Replacement checkout")
        with self.assertRaisesRegex(factory.FactoryError, "repository (pin|marker|identity)"):
            self.start(purpose="correction")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, [])

    def test_unknown_result_assignment_explains_how_to_find_the_right_id(self):
        initial = self.admit()["inputs"]
        before = self.path.read_bytes()
        result, output, errors = self.cli("result", "--task", "task", "--id", "typo",
                                          "--candidate", initial["candidate"],
                                          "--requirements", initial["requirements"],
                                          "--outcome", "approved", "--artifact", "evidence.txt")
        self.assertEqual((result, output), (1, ""))
        self.assertIn("Unknown assignment 'typo'", errors)
        self.assertIn("show --task task", errors)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, [])

    def test_unknown_status_or_capture_assignment_explains_recovery_without_rex(self):
        self.admit()
        before = self.path.read_bytes()
        for action in ("status", "capture"):
            result, output, errors = self.cli(action, "--task", "task", "--id", "typo")
            self.assertEqual((result, output), (1, ""))
            self.assertIn("Unknown assignment 'typo'", errors)
            self.assertIn("show --task task", errors)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, [])

    def test_damaged_rex_pin_cannot_repin_a_launched_task(self):
        self.admit()
        self.start()
        original = json.loads(self.path.read_text())
        for connection in (None, {}, {**original["rex"], "host_id": ""},
                           {**original["rex"], "binary": "relative-rex"}):
            self.path.write_text(json.dumps({**original, "rex": connection}))
            before = self.path.read_bytes()
            FakeRex.calls.clear()
            with self.subTest(connection=connection), self.assertRaises(factory.FactoryError):
                self.start(assignment_id="other", binary=sys.executable, server="unix:///other.sock")
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(FakeRex.calls, [])

    def test_missing_assignment_history_cannot_reset_allowance(self):
        self.admit()
        self.start(purpose="correction")
        record = json.loads(self.path.read_text())
        record["assignments"] = {}
        self.path.write_text(json.dumps(record))
        before = self.path.read_bytes()
        FakeRex.calls.clear()
        with self.assertRaisesRegex(factory.FactoryError, "restore the original task record"):
            self.start(assignment_id="another-correction", purpose="correction")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, [])

    def test_changed_live_session_identity_cannot_receive_more_work(self):
        self.admit()
        self.start()
        record = json.loads(self.path.read_text())
        FakeRex.sessions.append(dict(session_id="another-session", label="unrelated-session", windows=[]))
        record["session_id"] = "another-session"
        self.path.write_text(json.dumps(record))
        before = self.path.read_bytes()
        FakeRex.calls.clear()
        with self.assertRaisesRegex(factory.FactoryError, "session identity differs"):
            self.start(assignment_id="security", role="security")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(any(call[:2] == ("window", "new") for call in FakeRex.calls))

    def test_malformed_state_and_missing_repository_explain_recovery(self):
        self.path.write_text('{"truncated":')
        before = self.path.read_bytes()
        result, output, errors = self.cli("show", "--task", "task")
        self.assertEqual((result, output), (1, ""))
        self.assertIn("restore the original task record", errors)
        self.assertEqual(self.path.read_bytes(), before)
        result, output, errors = self.cli("admit", "--task", "new-task", "--goal", "Fixture",
            "--repo", str(self.root / "missing-checkout"), "--acceptance", "requirements.md")
        self.assertEqual((result, output), (1, ""))
        self.assertIn("--repo", errors)
        self.assertIn("existing accessible Git checkout", errors)
        self.assertFalse((self.state / "new-task.json").exists())
        self.assertEqual(FakeRex.calls, [])

    def test_other_checkout_and_uncommitted_inputs_refuse_before_rex(self):
        self.admit()
        other = self.root / "other"
        subprocess.run(["git", "clone", "--quiet", str(self.repo), str(other)], check=True)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "checkout differs"):
            self.start(cwd=str(other))
        with self.assertRaisesRegex(factory.FactoryError, "not a Git checkout"):
            self.start(cwd=str(self.state))
        (self.repo / "uncommitted.txt").write_text("Input outside the selected commit\n")
        with self.assertRaisesRegex(factory.FactoryError, "uncommitted inputs"):
            self.start()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, [])

    def test_linked_worktree_must_match_the_selected_candidate(self):
        initial = self.admit()["inputs"]["candidate"]
        other = self.root / "worktree"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "--quiet", "--detach",
                        str(other), initial], check=True)
        self.addCleanup(subprocess.run, ["git", "-C", str(self.repo), "worktree", "remove", str(other)], check=True)
        self.commit("A newer selected candidate")
        factory.set_inputs(self.path, candidate="HEAD", requirements="requirements-v2")
        with self.assertRaisesRegex(factory.FactoryError, "checkout differs"):
            self.start(cwd=str(other))
        self.assertEqual(FakeRex.calls, [])
        factory.set_inputs(self.path, candidate=initial, requirements="requirements-v1")
        _, assignment = self.start(cwd=str(other))
        self.assertEqual(assignment["block_id"], "block-1")


    def test_inconsistent_admission_purpose_and_dependency_history_refuse(self):
        empty = self.admit()
        lost_scope = {**copy.deepcopy(empty), "admission": None}
        self.start(role="investigator", purpose="investigation")
        wrong_purpose = json.loads(self.path.read_text())
        wrong_purpose["assignments"]["qa"]["request"]["purpose"] = "work"
        lost_legacy_scope = copy.deepcopy(wrong_purpose)
        lost_legacy_scope.update(admission=None, inputs=None)
        del lost_legacy_scope["assignments"]["qa"]["request"]["inputs"]
        factory.save(self.path, empty)
        FakeRex.sessions.clear()
        self.start()
        self.report()
        self.start(assignment_id="security", role="security", dependencies=["qa"])
        self.report(assignment_id="security")
        cycle = json.loads(self.path.read_text())
        cycle["assignments"]["qa"]["request"]["dependencies"] = ["security"]
        for record, purpose, diagnostic in ((lost_scope, None, "Invalid task inputs"),
            (wrong_purpose, "work", "Invalid investigation purpose"),
            (lost_legacy_scope, None, "Invalid unadmitted assignment"),
            (cycle, "work", "Invalid assignment dependencies")):
            factory.save(self.path, record)
            before = self.path.read_bytes()
            FakeRex.calls.clear()
            with self.subTest(diagnostic=diagnostic), self.assertRaisesRegex(factory.FactoryError, diagnostic):
                self.start(assignment_id="next", role="backend", purpose=purpose)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(FakeRex.calls, [])

    def test_crossed_pane_ids_cannot_expose_another_assignments_output(self):
        self.admit()
        self.start()
        self.start(assignment_id="security", role="security")
        original = json.loads(self.path.read_text())
        for field in ("window_id", "block_id"):
            changed = copy.deepcopy(original)
            changed["assignments"]["qa"][field] = changed["assignments"]["security"][field]
            factory.save(self.path, changed)
            before = self.path.read_bytes()
            for action in ("status", "capture"):
                FakeRex.calls.clear()
                result, output, errors = self.cli(action, "--task", "task", "--id", "qa")
                self.assertEqual((result, output), (1, ""))
                self.assertIn("pane identity", errors)
                self.assertFalse(any(call[0] in ("block", "capture") for call in FakeRex.calls))
                self.assertEqual(self.path.read_bytes(), before)

    def test_brief_contains_admitted_scope_and_reconnect_keeps_frozen_instructions(self):
        self.admit()
        _, assignment = self.start()
        instructions = Path(assignment["brief"]).read_text()
        self.assertIn("Task goal: Verify the example", instructions)
        self.assertIn("Acceptance references:\n- docs/example.md#acceptance", instructions)
        self.assertIn("Inspect only", instructions)
        before = self.path.read_bytes()
        with patch.object(factory, "brief", side_effect=AssertionError("Do not rebuild existing instructions")):
            _, reconnected = self.start()
        self.assertEqual(reconnected["block_id"], assignment["block_id"])
        self.assertEqual(Path(assignment["brief"]).read_text(), instructions)
        self.assertEqual(self.path.read_bytes(), before)


class RexTransport(unittest.TestCase):
    def test_process_availability_failures_explain_recovery(self):
        rex = factory.Rex(sys.executable, "unix:///fixture")
        for error in (FileNotFoundError("fixture"), PermissionError("fixture"), subprocess.TimeoutExpired("fixture", 20)):
            with self.subTest(error=type(error).__name__), patch.object(factory.subprocess, "run", side_effect=error):
                with self.assertRaisesRegex(factory.FactoryError, "restore its availability and reconcile"):
                    rex.call("whoami", "--json")

    def test_malformed_process_bytes_report_compatibility_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "rex-fixture"
            for destination in ("stdout", "stderr"):
                binary.write_text(f"#!{sys.executable}\nimport sys\nsys.{destination}.buffer.write(bytes([255]))\n")
                binary.chmod(0o700)
                rex = factory.Rex(str(binary), "unix:///fixture")
                for raw in (False, True):
                    with self.assertRaisesRegex(factory.FactoryError, "inspect the pinned binary/server API compatibility"):
                        rex.call("whoami", "--json", raw=raw)


if __name__ == "__main__":
    unittest.main()
