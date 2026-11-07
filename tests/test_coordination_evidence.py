"""Bounded answers use fixed classifier replies, fake native events and fake Rex."""
import hashlib
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import codex_worker
import coordination_evidence as evidence
import factory
import jev
import test_codex_worker as fixtures
from test_rex_launcher import FakeRex


class BoundedAnswers(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.NativeResults()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.store = self.root / "decisions"
        self.tasks = self.root / "tasks"
        self.tasks.mkdir()
        self.path = self.tasks / "resolve.json"
        self.inputs = dict(candidate=self.fixture.candidate, requirements="requirements-1")
        factory.admit(self.path, task="resolve", goal="Answer one bounded coordination question",
                      repository=str(self.fixture.repo), acceptance=["original-question"], corrections=1, investigations=1)
        factory.set_inputs(self.path, **self.inputs)
        FakeRex.sessions, FakeRex.calls = [], []
        FakeRex.fail_after_create, FakeRex.host = False, "test-host"

    def start(self, *, question="task-demand", role="standby-reasoner", choice="uncertain", assignment="reason", purpose="work"):
        state: dict = {"assignment": "Missing acceptance evidence"}
        if role == "judge":
            with patch.object(factory, "Rex", FakeRex):
                factory.launch(self.path, task="resolve", assignment_id="investigate", role="investigator",
                    goal="Gather the exact disputed evidence", cwd=str(self.fixture.repo), command=[sys.executable, "-c", "pass"],
                    purpose="investigation", binary=sys.executable, server="unix:///fixture.sock")
            record = factory.record_result(self.path, assignment_id="investigate", candidate=self.inputs["candidate"],
                requirements=self.inputs["requirements"], outcome="completed", artifacts=["evidence/recorded-investigation.json"])
            state["investigation"] = dict(task="resolve", assignment="investigate", result=record["assignments"]["investigate"]["result"])
        spec = next(spec for spec in jev.read_catalog() if spec["id"] == question)
        client = Mock()
        client.request.return_value = dict(model="jev-1.14.0", usage=dict(input_tokens=12, output_tokens=3), answers={
            question: dict(type="choice", choice=choice, confidence=1,
                           probabilities={option: int(option == choice) for option in spec["options"]})})
        self.decision = jev.evaluate_stored(self.store, state=state,
            specs=[spec], model="jev-1.14.0", evidence_revision="source-1", client_factory=lambda: client,
            clock=lambda: 1000, timer=lambda: 10)
        self.question = question
        self.assignment_id = assignment
        self.command = factory.codex_command(self.path, task="resolve", assignment_id=assignment, codex=sys.executable,
            model="fixture-model", effort="high", cwd=str(self.fixture.repo), role=role,
            decision_store=self.store, decision=self.decision["id"], question=question)
        with patch.object(factory, "Rex", FakeRex):
            self.record, self.assignment = factory.launch(self.path, task="resolve", assignment_id=assignment, role=role,
                goal="Answer the original bounded question", cwd=str(self.fixture.repo), command=self.command,
                purpose=purpose, binary=sys.executable, server="unix:///fixture.sock")
        actual = [call for call in FakeRex.calls if call[0] == "new" or call[:2] == ("window", "new")][-1]
        argv = actual[actual.index("--") + 1:]
        argv = argv[argv.index("--") + 1:]
        self.fixture.args = codex_worker.parser().parse_args(argv[4:])

    def answer(self, choice: str | None ="routine", missing=None, *, outcome="completed"):
        self.report = dict(outcome=outcome, artifacts=[str(self.fixture.args.brief)], answer=dict(
            decision=self.decision["id"], question=self.question, choice=choice, missing_input=missing))
        events = fixtures.events(outcome)
        events[-2]["item"]["text"] = json.dumps(self.report)
        self.fixture.process = fixtures.FakeProcess(events)
        return self.fixture.execute()

    def collect(self):
        with patch.object(factory, "status", return_value=dict(state="exited")):
            self.record = factory.collect_codex(self.path, self.assignment_id, Mock(), self.record, self.assignment)
        self.record = factory.load_task(self.path)
        self.assignment = self.record["assignments"][self.assignment_id]

    def test_selected_answer_is_retained_once_and_closes_only_the_bound_queue_item(self):
        self.start()
        original = (self.store / (self.decision["id"] + ".json")).read_bytes()
        receipt = self.answer()
        self.assertEqual((receipt["schema_version"], receipt["state"]), (6, "completed"))
        self.assertEqual(receipt["report"], self.report)
        self.collect()
        view = jev.pending_decisions(self.store, task_state_dir=self.tasks)
        self.assertEqual(view["schema_version"], 2)
        self.assertEqual(view["items"], [])
        self.assertEqual(view["resolutions"][0]["answer"]["choice"], "routine")
        self.assertEqual((self.store / (self.decision["id"] + ".json")).read_bytes(), original)
        before = self.path.read_bytes()
        self.assertEqual(jev.pending_decisions(self.store, task_state_dir=self.tasks), view)
        self.assertEqual(self.path.read_bytes(), before)

    def test_missing_input_remains_an_operator_request_without_another_reasoner_cycle(self):
        self.start()
        self.assertEqual(self.answer(None, "Supply the assignment acceptance criteria", outcome="blocked")["state"], "completed")
        self.collect()
        view = jev.pending_decisions(self.store, task_state_dir=self.tasks)
        item, = view["items"]
        self.assertEqual((item["owner"], item["reason"]), ("operator", "blocked"))
        self.assertEqual(item["attempts"][0]["answer"]["missing_input"], "Supply the assignment acceptance criteria")
        self.assertEqual(view["resolutions"], [])

    def test_uncollected_attempt_is_visible_without_inspecting_or_relaunching_rex(self):
        self.start()
        with patch.object(factory, "connect", side_effect=AssertionError("Inbox is offline")):
            item, = jev.pending_decisions(self.store, task_state_dir=self.tasks)["items"]
        self.assertEqual((item["owner"], item["reason"]), ("operator", "awaiting_collection"))
        self.assertEqual(item["attempts"][0]["assignment"], "reason")

    def test_changed_task_inputs_cannot_reuse_a_collected_answer(self):
        self.start()
        self.answer()
        self.collect()
        factory.set_inputs(self.path, candidate=self.inputs["candidate"], requirements="requirements-2")
        item, = jev.pending_decisions(self.store, task_state_dir=self.tasks)["items"]
        self.assertEqual((item["owner"], item["reason"]), ("operator", "stale"))

    def test_judge_gets_post_investigation_uncertainty_and_a_bounded_answer(self):
        self.start(question="investigation-resolution", role="judge", choice="unresolved")
        self.assertEqual(self.answer("resolved")["state"], "completed")
        self.collect()
        self.assertEqual(jev.pending_decisions(self.store, task_state_dir=self.tasks)["items"], [])

    def test_out_of_set_answer_refuses_completion(self):
        self.start()
        receipt = self.answer("invented")
        self.assertEqual(receipt["state"], "failed")
        self.assertIsNotNone(receipt["error"])

    def test_uncertainty_without_missing_input_refuses_completion(self):
        self.start()
        receipt = self.answer("uncertain")
        self.assertEqual(receipt["state"], "failed")
        self.assertIsNotNone(receipt["error"])

    def test_report_substitution_cannot_close_the_question(self):
        self.start()
        self.answer()
        self.collect()
        path = self.fixture.args.receipt
        changed = json.loads(path.read_text())
        changed["report"]["answer"]["choice"] = "expert"
        factory.save(path, changed)
        with self.assertRaisesRegex(jev.JevError, "reasoner evidence"):
            jev.pending_decisions(self.store, task_state_dir=self.tasks)

    def test_frozen_question_tampering_refuses_before_native_effect(self):
        self.start()
        args = self.fixture.args
        raw = args.brief.read_text().replace('"evidence_revision": "source-1"', '"evidence_revision": "source-2"')
        args.brief.write_text(raw)
        args.brief_sha256 = hashlib.sha256(raw.encode()).hexdigest()
        with self.assertRaisesRegex(factory.FactoryError, "coordination evidence"):
            self.fixture.execute()
        self.assertEqual(self.fixture.spawns, [])
        self.assertFalse(args.events.exists())

    def test_wrong_owner_or_writer_permission_cannot_bind_a_coordination_assignment(self):
        for role, write in (("qa", False), ("standby-reasoner", True), ("judge", False)):
            with self.subTest(role=role, write=write), self.assertRaises(factory.FactoryError):
                evidence.binding("a" * 64, "task-demand", role, write=write)

    def test_missing_or_asserted_investigation_cannot_activate_the_judge(self):
        for state in ({}, {"investigation": {"assignment": "invented", "result": {"outcome": "completed"}}}):
            with self.subTest(state=state), self.assertRaisesRegex(factory.FactoryError, "exact current investigator"):
                evidence.check_investigation(dict(payload=dict(state=state)), factory.load_task(self.path))

    def test_collected_answer_survives_its_isolated_worktree_removal(self):
        base = self.fixture.repo
        linked = self.root / "linked"
        added = factory.git_run(str(base), "-c", "core.hooksPath=" + os.devnull,
                                "worktree", "add", "--detach", str(linked), self.inputs["candidate"])
        self.assertEqual(added.returncode, 0, added.stderr)
        self.fixture.repo = linked
        os.chdir(linked)
        self.start()
        self.answer()
        self.collect()
        os.chdir(base)
        removed = factory.git_run(str(base), "worktree", "remove", str(linked))
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(linked.exists())
        self.assertEqual(jev.pending_decisions(self.store, task_state_dir=self.tasks)["items"], [])

    def test_cli_binding_error_is_actionable_without_a_traceback_or_launch(self):
        before = self.path.read_bytes()
        command = [sys.executable, str(factory.ROOT / "factory.py"), "--state-dir", str(self.tasks),
            "codex-start", "--task", "resolve", "--id", "reason", "--role", "qa", "--goal", "Invalid role fixture",
            "--cwd", str(self.fixture.repo), "--purpose", "work", "--codex", sys.executable,
            "--model", "fixture-model", "--effort", "high", "--decision-store", str(self.store),
            "--decision", "a" * 64, "--question", "task-demand"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Bind a read-only standby/judge", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.path.read_bytes(), before)

    def test_current_correction_does_not_conflict_with_retained_stale_failed_attempt(self):
        self.start()
        self.assertEqual(self.answer("uncertain", "Supply the original assignment")["state"], "failed")
        failed_receipt = self.fixture.args.receipt
        before = failed_receipt.read_bytes()
        self.inputs["requirements"] = "requirements-2"
        factory.set_inputs(self.path, **self.inputs)
        self.start(assignment="reason-correction", purpose="correction")
        self.answer(None, "Supply the original assignment", outcome="blocked")
        self.collect()
        item, = jev.pending_decisions(self.store, task_state_dir=self.tasks)["items"]
        self.assertEqual((item["owner"], item["reason"]), ("operator", "blocked"))
        self.assertEqual(item["attempts"][0]["assignment"], "reason-correction")
        self.assertEqual(failed_receipt.read_bytes(), before)
        self.assertEqual(len(self.record["assignments"]), 2)
        self.assertEqual(factory.task_view(self.record)["allowances"]["correction"]["remaining"], 0)

    def test_relocated_engine_does_not_erase_an_attempt_or_propose_duplicate_work(self):
        self.start()
        self.answer()
        self.collect()
        self.record["assignments"]["reason"]["request"]["command"][3] = "/retired-engine/codex_worker.py"
        factory.save(self.path, self.record)
        item, = jev.pending_decisions(self.store, task_state_dir=self.tasks)["items"]
        self.assertEqual((item["owner"], item["reason"]), ("operator", "engine_unavailable"))
        self.assertEqual(item["attempts"][0]["assignment"], "reason")
