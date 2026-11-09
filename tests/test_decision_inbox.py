"""Fixed offline inbox/control regressions; no private runtime state or live effects."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import hashlib
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import coordination_evidence
import dashboard
import factory
import jev
import test_dashboard as view_fixtures
import test_coordination_evidence as native_fixtures


class Inbox(unittest.TestCase):
    def setUp(self):
        self.fixture = view_fixtures.OperatorView()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.store = self.root / "decisions"
        self.no_rex = patch.object(factory, "connect", side_effect=AssertionError("Offline only"))
        self.no_rex.start()
        self.addCleanup(self.no_rex.stop)
        self.no_provider = patch.object(jev, "Jev", side_effect=AssertionError("No provider"))
        self.no_provider.start()
        self.addCleanup(self.no_provider.stop)

    def evaluate(self, question="task-demand", choice="uncertain", state=None):
        spec = next(spec for spec in jev.read_catalog() if spec["id"] == question)
        response = dict(model="jev-1.14.0", usage=dict(input_tokens=1, output_tokens=1), answers={question:
            dict(type="choice", choice=choice, confidence=1,
                 probabilities={key: int(key == choice) for key in spec["options"]})})
        client = Mock(request=Mock(return_value=response))
        return jev.evaluate_stored(self.store, state=state if state is not None else dict(private="secret-state"),
            specs=[spec], model="jev-1.14.0", evidence_revision="original-evidence-revision",
            client_factory=lambda: client, clock=lambda: 1000, timer=lambda: 10)

    def view(self):
        return dashboard.snapshot(self.root, decision_store=self.store, clock=lambda: 1000, timer=lambda: 10)

    def test_not_configured_missing_and_failed_sources_are_distinct_and_read_only(self):
        self.assertEqual(dashboard.snapshot(self.root)["decision_inbox"]["status"], "not_configured")
        self.assertEqual(self.view()["decision_inbox"]["status"], "available")
        self.assertFalse(self.store.exists())
        self.store.symlink_to(self.root / "missing")
        view = self.view()
        self.assertEqual(view["decision_inbox"]["status"], "unavailable")
        self.assertIn("not an empty or resolved inbox", dashboard.render(view))
        self.store.unlink()
        self.store.write_text("not a directory")
        self.assertEqual(self.view()["decision_inbox"]["status"], "unavailable")
        self.store.unlink()
        with patch.object(Path, "iterdir", side_effect=PermissionError("private detail")):
            inbox = dashboard.decision_inbox(self.root, self.store, [])
        self.assertEqual(inbox["status"], "unavailable")
        self.assertNotIn("private detail", json.dumps(inbox))

    def test_original_question_options_revision_are_allowlisted_unbound_and_escaped(self):
        decision = self.evaluate(state=dict(task="fixture", source_task="../../outside", path="/etc/passwd", command="private-argv"))
        before = (self.store / (decision["id"] + ".json")).read_bytes()
        view = self.view()
        item, = view["decision_inbox"]["items"]
        self.assertEqual(item["proposal_owner"], "standby-reasoner")
        self.assertEqual(item["owner"], "operator")
        self.assertIsNone(item["task"])
        self.assertIn("uncertain", item["options"])
        self.assertEqual(item["question_version"], 1)
        self.assertEqual(item["evidence_freshness"], "unknown")
        for private in ("/etc/passwd", "../../outside", "private-argv", "instructions", "payload"):
            self.assertNotIn(private, json.dumps(view))
        page = dashboard.render(view)
        self.assertIn("Original shadow proposal:", page)
        self.assertIn("original-evidence-revision", page)
        self.assertNotIn('method="post"', page)
        item["options"] = {'<script>"': 'https://evil/ <img onerror="bad">'}
        page = dashboard.render(view)
        self.assertIn("&lt;script&gt;&quot;", page)
        self.assertNotIn('href="https://evil/', page)
        self.assertEqual((self.store / (decision["id"] + ".json")).read_bytes(), before)
        self.assertEqual(self.view()["decision_inbox"], self.view()["decision_inbox"])

    def test_special_sources_refuse_before_jev_reader_and_render_unavailable(self):
        self.store.mkdir()
        target = self.root / "target"
        target.write_text("private target contents")
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        for directory in (self.root, self.store):
            for kind in ("fifo", "symlink-file", "symlink-fifo", "directory"):
                with self.subTest(source=directory.name, kind=kind):
                    source = directory / "damaged.json"
                    if kind == "fifo":
                        os.mkfifo(source)
                    elif kind.startswith("symlink"):
                        source.symlink_to(target if kind == "symlink-file" else fifo)
                    else:
                        source.mkdir()
                    open_file = os.open
                    def checked_open(path, flags, *args, **kwargs):
                        if path == source:
                            self.assertTrue(flags & os.O_NONBLOCK)
                            self.assertTrue(flags & os.O_NOFOLLOW)
                        return open_file(path, flags, *args, **kwargs)
                    try:
                        with patch.object(os, "open", side_effect=checked_open), \
                             patch.object(Path, "read_bytes", side_effect=AssertionError("No path byte reader")), \
                             patch.object(jev, "read_decision", side_effect=AssertionError("Reject before Jev reader")):
                            view = self.view()
                        inbox = view["decision_inbox"]
                        self.assertEqual(inbox["status"], "unavailable")
                        self.assertEqual(inbox["items"], [])
                        self.assertEqual(inbox["resolutions"], [])
                        page = dashboard.render(view)
                        self.assertIn("not an empty or resolved inbox", page)
                        self.assertNotIn("private target contents", page)
                        if directory == self.root:
                            self.assertIn("task_unavailable", [item["condition"] for item in inbox["conditions"]])
                        else:
                            self.assertIn("Restore the selected private Jev store", page)
                    finally:
                        source.rmdir() if kind == "directory" else source.unlink()

    def test_owner_handoffs_reject_replacement_and_new_fifos_without_mixed_results(self):
        decision = self.evaluate()
        source = self.store / (decision["id"] + ".json")
        original = source.read_bytes()
        pipe = self.root / "private-pipe"
        os.mkfifo(pipe)
        for handoff in ("after-fingerprint", "between-owner-passes", "before-detail"):
            for change in ("replacement-fifo", "replacement-link", "new-fifo", "new-link"):
                with self.subTest(handoff=handoff, change=change):
                    source.write_bytes(original)
                    added = self.store / ("0" * 64 + ".json")
                    pending, read, open_file = jev.pending_decisions, jev.read_decision, os.open
                    read_text = Path.read_text
                    passes, reads, descriptors = [], [], []
                    def mutate():
                        target = added if change.startswith("new") else source
                        target.unlink(missing_ok=True)
                        if change.endswith("link"):
                            target.symlink_to(pipe)
                        else:
                            os.mkfifo(target)
                    def collecting(*args, **kwargs):
                        passes.append(kwargs)
                        if (handoff == "after-fingerprint" and len(passes) == 1
                                or handoff == "between-owner-passes" and len(passes) == 2):
                            mutate()
                        return pending(*args, **kwargs)
                    def reading(path):
                        reads.append(path)
                        if handoff == "before-detail" and len(reads) == 3:
                            mutate()
                        return read(path)
                    def pathname_read(path, *args, **kwargs):
                        if path in (source, added):
                            raise AssertionError("Unsafe owner pathname reopen")
                        return read_text(path, *args, **kwargs)
                    def opening(path, flags, *args, **kwargs):
                        if Path(path) in (source, added):
                            self.assertTrue(flags & os.O_NONBLOCK)
                            self.assertTrue(flags & os.O_NOFOLLOW)
                        descriptor = open_file(path, flags, *args, **kwargs)
                        descriptors.append(descriptor)
                        return descriptor
                    try:
                        with patch.object(jev, "pending_decisions", side_effect=collecting), \
                             patch.object(jev, "read_decision", side_effect=reading), \
                             patch.object(os, "open", side_effect=opening), \
                             patch.object(Path, "read_text", pathname_read):
                            view = self.view()
                        inbox = view["decision_inbox"]
                        self.assertEqual((inbox["status"], inbox["items"], inbox["resolutions"]),
                                         ("unavailable", [], []))
                        self.assertNotIn("private-pipe", json.dumps(view))
                        self.assertIn("not an empty or resolved inbox", dashboard.render(view))
                        self.assertTrue(descriptors)
                        for descriptor in descriptors:
                            with self.assertRaises(OSError):
                                os.fstat(descriptor)
                    finally:
                        source.unlink(missing_ok=True)
                        added.unlink(missing_ok=True)

    def test_fingerprint_valid_descriptor_bytes_and_missing_first_use(self):
        decision = self.evaluate()
        source = self.store / (decision["id"] + ".json")
        before = source.read_bytes()
        with patch.object(os, "open", wraps=os.open) as opened, \
             patch.object(Path, "read_bytes", side_effect=AssertionError("No reopening by pathname")):
            identity = dashboard.inbox_source_identity(self.store)
        self.assertEqual(opened.call_count, 1)
        assert identity is not None
        self.assertEqual(identity[2][0][0], source.name)
        self.assertEqual(identity[2][0][2], hashlib.sha256(before).hexdigest())
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(self.view()["decision_inbox"]["status"], "available")
        absent = self.root / "absent"
        self.assertIsNone(dashboard.inbox_source_identity(absent, tasks=True))
        view = dashboard.snapshot(absent, decision_store=absent / "decisions")
        self.assertEqual(view["decision_inbox"]["status"], "available")
        self.assertEqual(view["tasks"], [])
        self.assertFalse(absent.exists())
        self.assertIn("Decision inbox", dashboard.render(view))

    def test_fingerprint_detects_in_place_changes_replacement_removal_and_link_races(self):
        self.store.mkdir()
        source = self.store / "evidence.json"
        for change in ("in-place", "replace", "remove", "symlink"):
            with self.subTest(change=change):
                source.write_bytes(b"original")
                replacement = self.root / "replacement"
                replacement.write_bytes(b"original")
                fstat = os.fstat
                calls = []
                def changing(descriptor):
                    info = fstat(descriptor)
                    calls.append(info)
                    if len(calls) == 1:
                        if change == "in-place":
                            source.write_bytes(b"changed content")
                        elif change == "replace":
                            replacement.replace(source)
                        else:
                            source.unlink()
                            if change == "symlink":
                                source.symlink_to(replacement)
                    return info
                with patch.object(os, "fstat", side_effect=changing):
                    with self.assertRaises(OSError):
                        dashboard.inbox_source_identity(self.store)
                source.unlink(missing_ok=True)

    def test_fingerprint_detects_path_replacement_after_descriptor_read(self):
        self.store.mkdir()
        source = self.store / "evidence.json"
        source.write_bytes(b"original")
        replacement = self.root / "replacement"
        replacement.write_bytes(b"original")
        lstat = Path.lstat
        def replacing(path):
            if path == source:
                replacement.replace(source)
            return lstat(path)
        with patch.object(Path, "lstat", replacing):
            with self.assertRaisesRegex(OSError, "Source changed"):
                dashboard.inbox_source_identity(self.store)

    def test_failed_pending_damaged_and_unsupported_records_never_look_resolved(self):
        original = self.evaluate()
        path = self.store / (original["id"] + ".json")
        for state in ("pending", "failed"):
            record = copy.deepcopy(original)
            record.update(state=state, response=None, error=None if state == "pending" else "secret-error-body")
            if state == "pending":
                record.update(finished_at=None, request_seconds=None)
            factory.save(path, record)
            inbox = self.view()["decision_inbox"]
            item, = inbox["items"]
            self.assertEqual((item["owner"], item["condition"]), ("operator", state))
            self.assertIn("uncertain", item["evaluation_questions"]["task-demand"]["options"])
            self.assertIn("Original question task-demand", dashboard.render(self.view()))
            self.assertNotIn("secret-error-body", json.dumps(inbox))
        for data in ('{"schema_version":999}', '{damaged'):
            path.write_text(data)
            inbox = self.view()["decision_inbox"]
            self.assertEqual(inbox["status"], "unavailable")

    def test_task_conditions_are_current_facts_not_semantic_routing(self):
        record = self.fixture.record
        record["assignments"]["backend"]["result"]["outcome"] = "findings"
        factory.save(self.fixture.path, record)
        conditions = self.view()["decision_inbox"]["conditions"]
        by_condition = {item["condition"]: item for item in conditions}
        self.assertEqual(set(by_condition), {"operator_hold", "correction_exhausted", "current_findings"})
        self.assertEqual(by_condition["correction_exhausted"]["owner"], "investigator")
        self.assertEqual(by_condition["current_findings"]["owner"], "operator")
        self.assertEqual(by_condition["current_findings"]["artifacts"], ["evidence/original.txt"])
        record["inputs"]["requirements"] = "changed"
        factory.save(self.fixture.path, record)
        self.assertNotIn("current_findings", [item["condition"] for item in self.view()["decision_inbox"]["conditions"]])
        factory.save(self.fixture.path, factory.new_task("fixture"))
        self.assertIn("allowances_unknown", [item["condition"] for item in self.view()["decision_inbox"]["conditions"]])

    def test_judge_requires_exact_current_completed_investigation_without_rewriting_original(self):
        record = self.fixture.record
        assignment = record["assignments"]["backend"]
        assignment["request"].update(role="investigator", purpose="investigation")
        assignment["window_label"] = "investigator-backend-00000000"
        assignment["result"]["outcome"] = "blocked"
        factory.save(self.fixture.path, record)
        investigation = dict(task="fixture", assignment="backend", result=copy.deepcopy(assignment["result"]))
        case = dict(question="Which original alternative?", options={"keep": "Keep", "change": "Change"})
        decision = self.evaluate("investigation-resolution", "unresolved", dict(investigation=investigation, dispute=case))
        source = self.store / (decision["id"] + ".json")
        before = source.read_bytes()
        item, = self.view()["decision_inbox"]["items"]
        self.assertEqual((item["proposal_owner"], item["owner"]), ("judge", "operator"))
        self.assertEqual(item["condition"], "investigation_unavailable_or_stale")
        self.assertEqual(item["options"], case["options"])
        assignment["result"]["outcome"] = "completed"
        factory.save(self.fixture.path, record)
        # A later completed result cannot rewrite an original blocked investigation.
        self.assertEqual(self.view()["decision_inbox"]["items"][0]["condition"], "investigation_unavailable_or_stale")
        self.assertEqual(source.read_bytes(), before)
        decision = self.evaluate("investigation-resolution", "unresolved", dict(
            investigation=dict(task="fixture", assignment="backend", result=copy.deepcopy(assignment["result"])), dispute=case))
        valid = next(item for item in self.view()["decision_inbox"]["items"] if item["decision"] == decision["id"])
        self.assertEqual(valid["owner"], "judge")
        self.assertIn("Held", valid["availability"])
        record["inputs"]["requirements"] = "new"
        factory.save(self.fixture.path, record)
        self.assertEqual({item["condition"] for item in self.view()["decision_inbox"]["items"]}, {"investigation_unavailable_or_stale"})

    def test_zero_admission_limits_without_assignments_are_not_failed_loops(self):
        record = self.fixture.record
        record["assignments"] = {}
        record.update(rex=None, session_id=None)
        record["admission"]["limits"] = dict(correction=0, investigation=0)
        factory.save(self.fixture.path, record)
        before = self.fixture.path.read_bytes()
        view = self.view()
        conditions = view["decision_inbox"]["conditions"]
        self.assertEqual({item["condition"] for item in conditions},
                         {"operator_hold", "correction_not_authorized", "investigation_not_authorized"})
        self.assertTrue(all(item["owner"] == "operator" for item in conditions))
        page = dashboard.render(view)
        self.assertIn("no authorized attempts; no attempts made", page)
        self.assertNotIn("failed correction loop", page)
        self.assertIn("Investigator proposals (0)", page)
        self.assertEqual(self.fixture.path.read_bytes(), before)

    def test_failed_loop_requires_current_failures_and_actual_relevant_correction_work(self):
        base = copy.deepcopy(self.fixture.record)
        for outcome, stale, purpose, expected in (
                ("completed", False, "correction", "operator"),
                ("approved", False, "correction", "operator"),
                ("findings", True, "correction", "operator"),
                ("blocked", True, "correction", "operator"),
                ("findings", False, "work", "operator"),
                ("findings", False, "correction", "investigator"),
                ("blocked", False, "correction", "investigator")):
            with self.subTest(outcome=outcome, stale=stale, purpose=purpose):
                record = copy.deepcopy(base)
                assignment = record["assignments"]["backend"]
                assignment["request"]["purpose"] = purpose
                assignment["result"]["outcome"] = outcome
                if purpose == "work":
                    record["admission"]["limits"]["correction"] = 0
                if stale:
                    record["inputs"]["requirements"] = "new-requirements"
                factory.save(self.fixture.path, record)
                view = self.view()
                item = next(i for i in view["decision_inbox"]["conditions"] if i["condition"].startswith("correction_"))
                self.assertEqual(item["owner"], expected)
                self.assertEqual("failed correction loop" in dashboard.render(view), expected == "investigator")
                if expected == "investigator":
                    self.assertEqual(item["artifacts"], ["evidence/original.txt"])
                    self.assertIn("Held", item["availability"])
                if stale:
                    self.assertFalse(view["tasks"][0]["assignments"][0]["result_current"])

    def test_current_review_failure_after_completed_correction_preserves_successful_work(self):
        record = self.fixture.record
        review = copy.deepcopy(record["assignments"]["backend"])
        review["request"].update(role="qa", purpose="work")
        review["window_label"] = "qa-review-00000000"
        review.update(window_id="review-window", block_id="review-block")
        review["result"].update(outcome="findings", artifacts=["review/current.txt"])
        record["assignments"]["review"] = review
        factory.save(self.fixture.path, record)
        before = self.fixture.path.read_bytes()
        view = self.view()
        item = next(i for i in view["decision_inbox"]["conditions"] if i["condition"] == "correction_exhausted")
        self.assertEqual(item["owner"], "investigator")
        self.assertEqual(item["artifacts"], ["review/current.txt"])
        self.assertEqual(view["tasks"][0]["assignments"][0]["result"]["outcome"], "completed")
        self.assertEqual(self.fixture.path.read_bytes(), before)
        # Old corrections do not establish an attempted loop on unrelated current inputs.
        record["assignments"]["backend"]["result"]["candidate"] = "b" * 40
        factory.save(self.fixture.path, record)
        item = next(i for i in self.view()["decision_inbox"]["conditions"] if i["condition"] == "correction_exhausted")
        self.assertEqual(item["owner"], "operator")

    def test_investigation_prerequisites_prevent_another_assignment(self):
        base = copy.deepcopy(self.fixture.record)
        base["assignments"]["backend"]["result"]["outcome"] = "findings"
        for outcome, limit, expected in (("blocked", 1, "operator recovery"),
                ("blocked", 2, "operator recovery"), ("completed", 1, "completed investigation"),
                (None, 2, "already assigned"), ("stale", 1, "no investigation allowance"),
                ("absent", 0, "no investigation allowance")):
            with self.subTest(outcome=outcome, limit=limit):
                record = copy.deepcopy(base)
                record["admission"]["limits"]["investigation"] = limit
                if outcome != "absent":
                    investigation = copy.deepcopy(record["assignments"]["backend"])
                    investigation["request"].update(role="investigator", purpose="investigation")
                    investigation["window_label"] = "investigator-investigate-00000000"
                    investigation.update(window_id="investigation-window", block_id="investigation-block")
                    investigation["result"].update(outcome="completed" if outcome == "stale" else outcome,
                                                    artifacts=["investigation/original.txt"])
                    if outcome == "stale":
                        investigation["result"]["candidate"] = "b" * 40
                    if outcome is None:
                        investigation["result"] = None
                    record["assignments"]["investigate"] = investigation
                factory.save(self.fixture.path, record)
                before = self.fixture.path.read_bytes()
                view = self.view()
                item = next(i for i in view["decision_inbox"]["conditions"] if i["condition"] == "correction_exhausted")
                self.assertEqual(item["owner"], "operator")
                self.assertIn(expected, item["availability"])
                self.assertIn("Held", item["availability"])
                self.assertIn("Investigator proposals (0)", dashboard.render(view))
                self.assertEqual(self.fixture.path.read_bytes(), before)

    def test_new_uncertain_decision_during_initial_empty_collection_is_unavailable(self):
        pending = jev.pending_decisions
        calls = []
        def changing(*args, **kwargs):
            result = pending(*args, **kwargs)  # Actual owner enumeration and validation.
            calls.append(result)
            if len(calls) == 1:
                self.evaluate()
            return result
        with patch.object(jev, "pending_decisions", side_effect=changing):
            view = self.view()
        self.assertEqual(calls[0]["items"], [])
        self.assertEqual(len(calls[1]["items"]), 1)
        self.assertEqual(view["decision_inbox"]["status"], "unavailable")
        self.assertEqual(view["decision_inbox"]["resolutions"], [])
        self.assertIn("changed or became unavailable during collection", dashboard.render(view))
        self.assertEqual(self.view()["decision_inbox"]["items"][0]["proposal_reason"], "uncertain")

    def test_pending_to_completed_during_collection_is_not_available_pending(self):
        completed = self.evaluate()
        path = self.store / (completed["id"] + ".json")
        pending_record = dict(completed, state="pending", response=None, finished_at=None, request_seconds=None)
        for transition_after in (1, 2):
            with self.subTest(transition_after=transition_after):
                factory.save(path, pending_record)
                pending = jev.pending_decisions
                calls = []
                def changing(*args, **kwargs):
                    result = pending(*args, **kwargs)
                    calls.append(result)
                    if len(calls) == transition_after:
                        factory.save(path, completed)
                    return result
                with patch.object(jev, "pending_decisions", side_effect=changing):
                    view = self.view()
                self.assertEqual(view["decision_inbox"]["status"], "unavailable")
                self.assertEqual(view["decision_inbox"]["items"], [])
                self.assertIn("not an empty or resolved inbox", dashboard.render(view))
                self.assertEqual(self.view()["decision_inbox"]["items"][0]["question"], "task-demand")

    def test_source_becoming_unreadable_during_collection_is_unavailable(self):
        decision = self.evaluate()
        source = self.store / (decision["id"] + ".json")
        open_file = os.open
        reads = []
        def unreadable(path, flags, *args, **kwargs):
            if path == source:
                reads.append(path)
                if len(reads) > 1:
                    raise PermissionError("private source error")
            return open_file(path, flags, *args, **kwargs)
        with patch.object(os, "open", unreadable):
            inbox = self.view()["decision_inbox"]
        self.assertEqual(inbox["status"], "unavailable")
        self.assertNotIn("private source error", json.dumps(inbox))

    def test_broken_native_evidence_keeps_original_proposal_and_unavailable_status(self):
        self.evaluate()
        (self.root / "damaged.json").write_text("broken")
        view = self.view()
        self.assertEqual(view["decision_inbox"]["status"], "unavailable")
        item, = view["decision_inbox"]["items"]
        self.assertEqual(item["condition"], "native_evidence_unavailable")
        self.assertIn("task_unavailable", [item["condition"] for item in view["decision_inbox"]["conditions"]])

    def post(self, fields=None, *, body=None, headers=None, path="/tasks/fixture/allowance", **kwargs):
        body = urlencode(fields or {}).encode() if body is None else body
        return self.fixture.get_without_socket(path, method="POST", body=body, headers={
            "Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body)),
            "Origin": "http://127.0.0.1:8090", **(headers or {})}, **kwargs)

    def fields(self, **changes):
        return dict(csrf="fixed-csrf", request_id="one-correction", expected_limit="1", purpose="correction",
                    reason="Explicit operator approval", **changes)

    def test_explicit_one_attempt_control_replay_intervening_decisions_and_history(self):
        original = factory.load_task(self.fixture.path)
        fields = self.fields()
        with patch.object(dashboard.secrets, "token_urlsafe", return_value="fixed-csrf"):
            page = dashboard.render(self.view(), csrf_token="fixed-csrf")
            self.assertIn('action="/tasks/fixture/allowance"', page)
            self.assertIn('name="expected_limit" value="1"', page)
            self.assertNotIn('name="additional"', page)
            self.assertIn("Default counts are progress-review checkpoints", page)
            self.assertIn("without asking the operator again", page)
            self.assertIn("user budgets, scope and holds remain binding", page)
            self.post(fields).send_response.assert_called_with(303)
            second = dict(fields, request_id="second", expected_limit="2", reason="Another explicit approval")
            self.post(second).send_response.assert_called_with(303)
            with factory.task_lock(self.root, "fixture") as path:
                factory.hold_task(path, "Intervening hold", request_id="later-hold")
            before = self.fixture.path.read_bytes()
            self.post(fields).send_response.assert_called_with(303)
            self.assertEqual(self.fixture.path.read_bytes(), before)
            for changed in (dict(fields, reason="conflicting"), dict(fields, request_id="stale")):
                self.assertEqual(self.post(changed).send_error.call_args.args[0], 409)
            self.fixture.get_without_socket("/").send_response.assert_called_with(200)
            self.assertEqual(self.fixture.path.read_bytes(), before)
        saved = factory.load_task(self.fixture.path)
        for key in ("admission", "inputs", "assignments"):
            self.assertEqual(saved[key], original[key])
        self.assertEqual(saved["schema_version"], factory.new_task("fixture")["schema_version"])
        self.assertEqual(saved["hold"], "Intervening hold")
        self.assertEqual(factory.task_view(saved)["allowances"]["correction"], dict(used=1, limit=3, remaining=2))
        self.assertEqual([event["additional"] for event in saved["allowance_events"]], [1, 1])
        page = dashboard.render(self.view())
        self.assertIn("Original allowance decisions", page)
        self.assertIn("Original admission", page)
        self.assertIn("one-correction", page)
        self.assertNotIn('method="post"', page)

    def test_auth_host_csrf_paths_and_strict_fields_precede_any_state_access(self):
        fields = self.fields()
        with patch.object(dashboard.secrets, "token_urlsafe", return_value="fixed-csrf"), \
             patch.object(factory, "task_lock", side_effect=AssertionError("No access")):
            self.post(fields, auth=False).send_response.assert_called_with(401)
            self.assertEqual(self.post(fields, host="foreign:8090").send_error.call_args.args[0], 403)
            self.assertEqual(self.post(dict(fields, csrf="bad")).send_error.call_args.args[0], 403)
            self.assertEqual(self.post(fields, headers={"Origin": "http://evil"}).send_error.call_args.args[0], 403)
            self.assertEqual(self.post(fields, headers={"Origin": None}).send_error.call_args.args[0], 403)
            for path in ("/tasks/../allowance", "/tasks/fixture/allowance?x=1", "/tasks/%66ixture/allowance",
                         "http:/tasks/fixture/allowance", "/tasks/fixture/allowance#x"):
                self.assertEqual(self.post(fields, path=path).send_error.call_args.args[0], 404)
            bad = [dict(fields, additional="2"), dict(fields, purpose="work"), dict(fields, reason=" "),
                   dict(fields, reason="x" * 513), dict(fields, expected_limit="-1"), dict(fields, request_id="../x"),
                   dict(fields, expected_limit="1" * 5000)]
            for value in bad:
                self.assertEqual(self.post(value).send_error.call_args.args[0], 400)
            for body in (urlencode(fields).encode() + b'&purpose=investigation', b'\xff', b'x=' + b'a' * 8192):
                self.assertEqual(self.post(body=body).send_error.call_args.args[0], 400)
            for headers in ({"Transfer-Encoding": "chunked"}, {"Content-Type": "application/json"},
                            {"Content-Length": "8193"}, {"Content-Length": "0"}, {"Content-Length": "-1"}):
                self.assertEqual(self.post(fields, headers=headers).send_error.call_args.args[0], 400)
            self.fixture.get_without_socket("/api/state", auth=False, decision_store=self.store).send_response.assert_called_with(401)

    def test_concurrent_additions_serialize_and_stale_requests_cannot_add_twice(self):
        # Construct handlers sequentially; invoke only the lock/owner mutation concurrently.
        fields = self.fields()
        entered, release = threading.Event(), threading.Event()
        extend = factory.extend_allowance
        def slow(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return extend(*args, **kwargs)
        with patch.object(dashboard.secrets, "token_urlsafe", return_value="fixed-csrf"), \
             patch.object(factory, "extend_allowance", side_effect=slow), ThreadPoolExecutor() as pool:
            first = pool.submit(self.post, fields)
            self.assertTrue(entered.wait(5))
            busy = self.post(dict(fields, request_id="concurrent"))
            self.assertEqual(busy.send_error.call_args.args[0], 503)
            release.set()
            first.result(timeout=5).send_response.assert_called_with(303)
            self.assertEqual(self.post(dict(fields, request_id="concurrent")).send_error.call_args.args[0], 409)
            self.post(fields).send_response.assert_called_with(303)
        self.assertEqual(len(factory.load_task(self.fixture.path)["allowance_events"]), 1)

    def test_busy_damaged_and_unadmitted_controls_refuse_without_reset(self):
        fields = dict(self.fields(), purpose="investigation", reason="y" * 512)
        with patch.object(dashboard.secrets, "token_urlsafe", return_value="fixed-csrf"):
            with factory.task_lock(self.root, "fixture"):
                self.assertEqual(self.post(fields).send_error.call_args.args[0], 503)
            self.post(fields).send_response.assert_called_with(303)
            self.assertEqual(factory.task_view(factory.load_task(self.fixture.path))["allowances"]["investigation"]["limit"], 2)
            self.fixture.path.write_text("broken")
            self.assertEqual(self.post(fields).send_error.call_args.args[0], 503)
            self.assertEqual(self.fixture.path.read_text(), "broken")
            factory.save(self.fixture.path, factory.new_task("fixture"))
            self.assertEqual(self.post(fields).send_error.call_args.args[0], 503)

    def test_old_static_render_and_cli_store_selection_remain_compatible(self):
        view = self.view()
        view.pop("decision_inbox")
        for key in ("admission", "allowance_events"):
            view["tasks"][0].pop(key)
        self.assertNotIn('method="post"', dashboard.render(view))
        with patch.object(dashboard, "make_server") as make, redirect_stdout(StringIO()):
            make.return_value.__enter__.return_value.server_port = 8090
            make.return_value.__enter__.return_value.serve_forever.side_effect = KeyboardInterrupt
            self.assertEqual(dashboard.main(["--state-dir", str(self.root), "--decision-store", str(self.store)]), 0)
        self.assertEqual(make.call_args.kwargs["decision_store"], self.store.absolute())

    def test_lost_save_reply_reuses_original_request_without_another_addition(self):
        fields = self.fields()
        extend = factory.extend_allowance
        def lost_reply(*args, **kwargs):
            extend(*args, **kwargs)
            raise OSError("Lost reply after saved decision")
        with patch.object(dashboard.secrets, "token_urlsafe", return_value="fixed-csrf"):
            with patch.object(factory, "extend_allowance", side_effect=lost_reply):
                self.assertEqual(self.post(fields).send_error.call_args.args[0], 503)
            before = self.fixture.path.read_bytes()
            self.post(fields).send_response.assert_called_with(303)
            self.assertEqual(self.fixture.path.read_bytes(), before)
            self.assertEqual(len(factory.load_task(self.fixture.path)["allowance_events"]), 1)


class NativeInbox(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixtures.BoundedAnswers()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def view(self):
        with patch.object(factory, "connect", side_effect=AssertionError("No Rex reads")), \
             patch.object(jev, "Jev", side_effect=AssertionError("No provider reads")):
            return dashboard.snapshot(self.fixture.tasks, decision_store=self.fixture.store)

    def test_original_shadow_and_current_native_answer_stay_distinct_then_stale(self):
        f = self.fixture
        f.start()
        f.answer()
        f.collect()
        before = f.path.read_bytes()
        view = self.view()
        self.assertEqual(view["decision_inbox"]["items"], [])
        item, = view["decision_inbox"]["resolutions"]
        self.assertEqual(item["proposal_reason"], "uncertain")
        self.assertEqual(item["recorded_answer"]["choice"], "routine")
        self.assertEqual(item["task"], "resolve")
        self.assertIn("Recorded current native answers (1)", dashboard.render(view))
        self.assertEqual(f.path.read_bytes(), before)
        factory.set_inputs(f.path, candidate=f.inputs["candidate"], requirements="new")
        item, = self.view()["decision_inbox"]["items"]
        self.assertEqual((item["owner"], item["condition"]), ("operator", "stale"))
        self.assertIsNone(item["recorded_answer"])

    def test_fifo_before_detail_discards_collected_native_resolution(self):
        f = self.fixture
        f.start()
        f.answer()
        f.collect()
        read = jev.read_decision
        calls = []
        source = f.store / (f.decision["id"] + ".json")
        def replacing(path):
            calls.append(path)
            if len(calls) == 3:
                source.unlink()
                os.mkfifo(source)
            return read(path)
        read_text, open_file = Path.read_text, os.open
        def pathname_read(path, *args, **kwargs):
            if path == source and path.is_fifo():
                raise AssertionError("Unsafe owner FIFO pathname read")
            return read_text(path, *args, **kwargs)
        def opening(path, flags, *args, **kwargs):
            if Path(path) == source:
                self.assertTrue(flags & os.O_NONBLOCK)
                self.assertTrue(flags & os.O_NOFOLLOW)
            return open_file(path, flags, *args, **kwargs)
        with patch.object(jev, "read_decision", side_effect=replacing), \
             patch.object(Path, "read_text", pathname_read), \
             patch.object(os, "open", side_effect=opening):
            view = self.view()
        inbox = view["decision_inbox"]
        self.assertEqual((inbox["status"], inbox["items"], inbox["resolutions"]), ("unavailable", [], []))
        self.assertIn("Recorded current native answers (0)", dashboard.render(view))

    def test_task_change_during_collection_cannot_claim_current_native_resolution(self):
        f = self.fixture
        f.start()
        f.answer()
        f.collect()
        pending = jev.pending_decisions
        calls = []
        def changing(*args, **kwargs):
            result = pending(*args, **kwargs)
            calls.append(result)
            if len(calls) == 2:
                record = factory.load_task(f.path)
                record["inputs"]["requirements"] = "new-requirements"
                factory.save(f.path, record)
            return result
        with patch.object(jev, "pending_decisions", side_effect=changing):
            view = self.view()
        self.assertEqual(len(calls[1]["resolutions"]), 1)
        self.assertEqual(view["decision_inbox"]["status"], "unavailable")
        self.assertEqual(view["decision_inbox"]["resolutions"], [])
        self.assertIn("Recorded current native answers (0)", dashboard.render(view))
        self.assertEqual(self.view()["decision_inbox"]["items"][0]["condition"], "stale")

    def test_missing_input_engine_unavailable_and_conflicting_answers_stay_operator_items(self):
        f = self.fixture
        f.start()
        self.assertEqual(self.view()["decision_inbox"]["items"][0]["condition"], "awaiting_collection")
        f.answer(None, "Supply <original> scope", outcome="blocked")
        f.collect()
        item, = self.view()["decision_inbox"]["items"]
        self.assertEqual(item["condition"], "blocked")
        self.assertIn("Supply &lt;original&gt; scope", dashboard.render(self.view()))
        record = factory.load_task(f.path)
        record["assignments"]["reason"]["request"]["command"][3] = "/retired/codex_worker.py"
        factory.save(f.path, record)
        self.assertEqual(self.view()["decision_inbox"]["items"][0]["condition"], "engine_unavailable")
        f.start(assignment="second")
        self.assertEqual(self.view()["decision_inbox"]["items"][0]["condition"], "conflicting_attempts")

    def test_malformed_saved_native_arguments_never_echo_argv_and_missing_tasks_follow_first_use(self):
        f = self.fixture
        f.start()
        record = factory.load_task(f.path)
        record["assignments"]["reason"]["request"]["command"] += ["--private-secret-argument"]
        factory.save(f.path, record)
        output = StringIO()
        with redirect_stderr(output), redirect_stdout(output):
            view = self.view()
        self.assertEqual(view["decision_inbox"]["status"], "unavailable")
        self.assertNotIn("private-secret", output.getvalue() + json.dumps(view))
        self.assertEqual(coordination_evidence.collected_attempts(f.root / "absent"), {})
        alias = f.root / "dangling"
        alias.symlink_to(f.root / "absent")
        with self.assertRaises(factory.FactoryError):
            coordination_evidence.collected_attempts(alias)

    def test_recorded_judgment_cannot_clear_a_missing_or_blocked_current_investigation(self):
        f = self.fixture
        f.start(question="investigation-resolution", role="judge", choice="unresolved")
        f.answer("adopt-fix")
        f.collect()
        self.assertEqual(len(self.view()["decision_inbox"]["resolutions"]), 1)
        record = factory.load_task(f.path)
        record["assignments"]["investigate"]["result"]["outcome"] = "blocked"
        factory.save(f.path, record)
        view = self.view()
        self.assertEqual(view["decision_inbox"]["resolutions"], [])
        item, = view["decision_inbox"]["items"]
        self.assertEqual((item["owner"], item["condition"]), ("operator", "investigation_unavailable_or_stale"))
        # The native evidence owner also refuses the now-invalid binding. Keep it
        # unavailable rather than bypassing that owner to reconstruct a ruling.
        self.assertEqual(view["decision_inbox"]["status"], "unavailable")
        self.assertIsNone(item["recorded_answer"])
        self.assertEqual(factory.load_task(f.path)["assignments"]["reason"]["result"]["outcome"], "completed")
        self.assertIn("Blocked:", dashboard.render(view))
