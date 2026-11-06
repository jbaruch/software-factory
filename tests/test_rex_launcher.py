"""Failure/restart tests use a fake server; no live sessions or providers."""
import json
import copy
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
        for key in ("schema_version", "admission", "hold", "inputs"):
            del record[key]
        for assignment in record["assignments"].values():
            del assignment["result"]
        self.path.write_text(json.dumps(record))
        migrated = factory.load_task(self.path)
        self.assertEqual(migrated["schema_version"], 2)
        self.assertIsNone(factory.task_view(migrated)["allowances"])
        self.assertEqual(migrated["assignments"],
                         {key: {**item, "result": None} for key, item in record["assignments"].items()})
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
        del record["inputs"]
        del record["assignments"]["qa"]["request"]["inputs"]
        del record["assignments"]["qa"]["result"]
        self.path.write_text(json.dumps(record))
        upgraded = factory.load_task(self.path)
        self.assertEqual(upgraded["schema_version"], 2)
        self.assertIsNone(upgraded["assignments"]["qa"]["result"])
        self.assertNotIn("inputs", upgraded["assignments"]["qa"]["request"])
        with self.assertRaisesRegex(factory.FactoryError, "predates input binding"):
            self.report()

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
        with self.assertRaisesRegex(factory.FactoryError, "not an existing Git commit"):
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


if __name__ == "__main__":
    unittest.main()
