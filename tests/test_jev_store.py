"""Durable decisions use fixed fake clients; no provider or native sessions."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import copy
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
from email.message import Message
from io import BytesIO

import factory
import jev
from test_jev import FakeOpener


SPEC: dict = dict(id="finding", primitive="choice", version=1, instructions="Is ownership established?",
            options={"yes": "The supplied evidence establishes ownership.", "uncertain": "Evidence is missing."})
RESPONSE: dict = dict(model="jev-1.14.0", usage=dict(input_tokens=11, output_tokens=3), answers={
    "finding": dict(type="choice", choice="yes", confidence=0.9, probabilities=dict(yes=0.9, uncertain=0.1))})


class DecisionStore(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "decisions"
        self.client = Mock()
        self.client.request.return_value = copy.deepcopy(RESPONSE)
        self.client_factory = Mock(return_value=self.client)
        self.options: dict = dict(state={"finding": "Original exact evidence"}, specs=[copy.deepcopy(SPEC)],
                            model="jev-1.14.0", evidence_revision="fixture-revision-1",
                            client_factory=self.client_factory, clock=lambda: 1000,
                            timer=Mock(side_effect=[10, 10.25] * 20))

    def evaluate(self, **changes):
        return jev.evaluate_stored(self.root, **{**self.options, **changes})

    def path(self, record):
        return self.root / (record["id"] + ".json")

    def test_exact_replay_needs_no_credentials_and_preserves_original_usage_and_time(self):
        record = self.evaluate()
        path = self.path(record)
        original = path.read_bytes()
        self.assertEqual(record["state"], "completed")
        self.assertEqual(record["response"]["usage"], dict(input_tokens=11, output_tokens=3))
        self.assertEqual(record["request_seconds"], 0.25)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        no_client = Mock(side_effect=AssertionError("A replay must not instantiate a client"))
        self.assertEqual(self.evaluate(client_factory=no_client), record)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.client.request.call_count, 1)
        no_client.assert_not_called()
        self.assertEqual(jev.pending_decisions(self.root)["items"], [])

    def test_regular_decision_states_and_rejections_close_owner_descriptors(self):
        completed = self.evaluate()
        path = self.path(completed)
        for state in ("pending", "failed", "completed", "malformed", "fifo", "fifo-link"):
            with self.subTest(state=state):
                path.unlink(missing_ok=True)
                if state in ("fifo", "fifo-link"):
                    pipe = self.root / "pipe"
                    if not pipe.exists():
                        os.mkfifo(pipe)
                    if state == "fifo":
                        os.mkfifo(path)
                    else:
                        path.symlink_to(pipe)
                elif state == "malformed":
                    path.write_text("private malformed contents")
                else:
                    record = copy.deepcopy(completed)
                    record["state"] = state
                    if state != "completed":
                        record.update(response=None, error="private failure" if state == "failed" else None)
                    if state == "pending":
                        record.update(finished_at=None, request_seconds=None)
                    factory.save(path, record)
                opened = []
                open_file = os.open
                def opening(source, flags, *args, **kwargs):
                    self.assertTrue(flags & os.O_NONBLOCK)
                    self.assertTrue(flags & os.O_NOFOLLOW)
                    descriptor = open_file(source, flags, *args, **kwargs)
                    opened.append(descriptor)
                    return descriptor
                with patch.object(os, "open", side_effect=opening), \
                     patch.object(Path, "read_text", side_effect=AssertionError("Unsafe pathname reader")):
                    if state in ("malformed", "fifo", "fifo-link"):
                        with self.assertRaises(jev.JevError) as error:
                            jev.read_decision(path)
                        self.assertNotIn("private", str(error.exception))
                        self.assertIn("restore", str(error.exception))
                    else:
                        self.assertEqual(jev.read_decision(path), record)
                self.assertEqual(len(opened), 0 if state == "fifo-link" else 1)
                for descriptor in opened:
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)

    def test_relevant_input_changes_do_not_reuse_old_judgment(self):
        original = self.evaluate()
        for changes in (dict(evidence_revision="fixture-revision-2"), dict(state="New evidence"),
                        dict(specs=[{**SPEC, "version": 2}]),
                        dict(specs=[{**SPEC, "instructions": "Has the new question been settled?"}]),
                        dict(specs=[{**SPEC, "options": {**SPEC["options"], "yes": "New criterion"}}]),
                        dict(model="jev-1.14.1")):
            with self.subTest(changes=changes):
                self.client.request.return_value = {**copy.deepcopy(RESPONSE), "model": changes.get("model", "jev-1.14.0")}
                changed = self.evaluate(**changes)
                self.assertNotEqual(changed["id"], original["id"])
        self.assertEqual(self.client.request.call_count, 7)

    def test_alias_request_is_bound_to_explicit_resolved_version(self):
        record = self.evaluate(model="jev-latest", model_version="jev-1.14.0")
        self.assertEqual(record["request"]["payload"]["model"], "jev-latest")
        self.assertEqual(record["request"]["model_version"], "jev-1.14.0")
        no_client = Mock(side_effect=AssertionError("Cached version requires no client"))
        self.assertEqual(self.evaluate(model="jev-latest", model_version="jev-1.14.0", client_factory=no_client), record)
        self.client.request.return_value = {**copy.deepcopy(RESPONSE), "model": "jev-1.14.1"}
        updated = self.evaluate(model="jev-latest", model_version="jev-1.14.1")
        self.assertNotEqual(updated["id"], record["id"])
        self.assertEqual(self.client.request.call_count, 2)

    def test_candidate_changes_invalidate_the_cached_request(self):
        spec = {**SPEC, "options": {"candidate": "Supplied owners", "uncertain": "Missing evidence"}}
        options = dict(specs=[spec], candidates={"finding": {"yes": "Coverage owner"}})
        first = self.evaluate(**options)
        second = self.evaluate(**{**options, "candidates": {"finding": {"yes": "Changed coverage owner"}}})
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.client.request.call_count, 2)

    def test_saved_intent_precedes_http_and_owns_a_snapshot_of_caller_state(self):
        original = copy.deepcopy(self.options["state"])
        def respond(*args):
            path, = self.root.glob("*.json")
            self.assertEqual(jev.read_decision(path)["state"], "pending")
            self.options["state"]["finding"] = "Caller changed after request started"
            return copy.deepcopy(RESPONSE)
        self.client.request.side_effect = respond
        record = self.evaluate()
        self.assertEqual(record["request"]["payload"]["state"], original)
        self.assertEqual(jev.read_decision(self.path(record)), record)
        self.assertEqual(self.evaluate(state=original), record)
        self.assertEqual(self.client.request.call_count, 1)

    def test_uncertainty_is_durable_and_repeated_inbox_reads_do_not_repeat_requests(self):
        response = copy.deepcopy(RESPONSE)
        response["answers"]["finding"].update(choice="uncertain", confidence=1,
                                                probabilities=dict(yes=0, uncertain=1))
        self.client.request.return_value = response
        record = self.evaluate()
        before = self.path(record).read_bytes()
        first = jev.pending_decisions(self.root)
        self.assertEqual(first["mode"], "shadow")
        self.assertEqual(first["items"], [dict(decision=record["id"], question="finding", owner="standby-reasoner",
            reason="uncertain", evidence=str(self.path(record)), evidence_revision="fixture-revision-1")])
        self.assertEqual(jev.pending_decisions(self.root), first)
        self.assertEqual(self.path(record).read_bytes(), before)
        self.assertEqual(self.client.request.call_count, 1)

    def test_investigation_uncertainty_and_unresolved_go_to_judge_without_standby_cycle(self):
        spec = next(spec for spec in jev.read_catalog() if spec["id"] == "investigation-resolution")
        for choice in ("uncertain", "unresolved"):
            self.client.request.return_value = dict(model="jev-1.14.0", usage=dict(input_tokens=11, output_tokens=3), answers={
                spec["id"]: dict(type="choice", choice=choice, confidence=1,
                    probabilities={key: int(key == choice) for key in spec["options"]})})
            self.evaluate(specs=[spec], evidence_revision="fixture-" + choice)
        items = jev.pending_decisions(self.root)["items"]
        self.assertEqual(len(items), 2)
        self.assertEqual({item["owner"] for item in items}, {"judge"})

    def test_interrupted_request_retains_intent_and_restart_never_repeats_unknown_outcome(self):
        self.client.request.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.evaluate()
        path, = self.root.glob("*.json")
        record = jev.read_decision(path)
        self.assertEqual(record["state"], "pending")
        no_client = Mock(side_effect=AssertionError("Do not repeat a pending request"))
        before = path.read_bytes()
        with self.assertRaisesRegex(jev.JevError, "No request was repeated"):
            self.evaluate(client_factory=no_client)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.client.request.call_count, 1)
        self.assertEqual(jev.pending_decisions(self.root)["items"][0]["owner"], "operator")
        no_client.assert_not_called()

    def test_failed_request_remains_visible_and_is_not_retried_by_refresh(self):
        self.client.request.side_effect = jev.JevError("TypeSafe HTTP 429; wait for account capacity")
        with self.assertRaisesRegex(jev.JevError, "429"):
            self.evaluate()
        item, = jev.pending_decisions(self.root)["items"]
        self.assertEqual((item["owner"], item["reason"]), ("operator", "failed"))
        self.assertIn("wait for account capacity", item["error"])
        with self.assertRaisesRegex(jev.JevError, "No request was repeated"):
            self.evaluate()
        self.assertEqual(self.client.request.call_count, 1)

    def test_provider_error_bodies_and_credentials_are_never_retained(self):
        opener = FakeOpener(urllib.error.HTTPError(jev.API, 403, "fixture-secret-key", Message(),
                                                  BytesIO(b"fixture-private-response")))
        client = jev.Jev("fixture-secret-key", opener=opener)
        with self.assertRaises(jev.JevError):
            self.evaluate(client_factory=lambda: client)
        path, = self.root.glob("*.json")
        for hidden in ("fixture-secret-key", "fixture-private-response"):
            self.assertNotIn(hidden, path.read_text())
        self.assertIn("TypeSafe HTTP 403", jev.read_decision(path)["error"])

    def test_pending_intent_survives_lost_completion_save(self):
        save = factory.save
        def fail_completion(path, value, **options):
            if value["state"] == "completed":
                raise OSError("Fixture disk unavailable")
            save(path, value, **options)
        with patch.object(factory, "save", side_effect=fail_completion), self.assertRaises(OSError):
            self.evaluate()
        path, = self.root.glob("*.json")
        self.assertEqual(jev.read_decision(path)["state"], "pending")
        with self.assertRaisesRegex(jev.JevError, "No request was repeated"):
            self.evaluate()
        self.assertEqual(self.client.request.call_count, 1)

    def test_alias_missing_revision_and_invalid_state_refuse_before_intent(self):
        for changes in (dict(model="jev-latest"), dict(evidence_revision=" "), dict(state=42)):
            with self.subTest(changes=changes), self.assertRaises(jev.JevError):
                self.evaluate(**changes)
        self.assertFalse(self.root.exists())
        self.client_factory.assert_not_called()

    def test_wrong_resolved_model_and_invalid_answers_remain_failed(self):
        self.client.request.return_value = {**RESPONSE, "model": "jev-1.14.1"}
        with self.assertRaisesRegex(jev.JevError, "different model"):
            self.evaluate()
        self.client.request.return_value = {**RESPONSE, "answers": {}}
        with self.assertRaisesRegex(jev.JevError, "mismatched questions"):
            self.evaluate(evidence_revision="fixture-revision-2")
        self.assertEqual({item["reason"] for item in jev.pending_decisions(self.root)["items"]}, {"failed"})

    def test_unknown_and_malformed_records_refuse_instead_of_becoming_cache_misses(self):
        original = self.evaluate()
        path = self.path(original)
        for changes in (dict(schema_version=2), dict(schema_version=True), dict(state=[]),
                        dict(request_seconds=-1), dict(response=None), dict(created_at=True),
                        dict(request={}), dict(error="unexpected error")):
            with self.subTest(changes=changes):
                factory.save(path, {**copy.deepcopy(original), **changes})
                with self.assertRaisesRegex(jev.JevError, "retained decision"):
                    self.evaluate()
        self.assertEqual(self.client.request.call_count, 1)

    def test_concurrent_same_request_never_starts_another_provider_call(self):
        record = self.evaluate()
        with factory.task_lock(self.root, record["id"]), self.assertRaisesRegex(jev.JevError, "Another command"):
            self.evaluate()
        self.assertEqual(self.client.request.call_count, 1)

    def test_pending_cli_is_offline_and_missing_store_is_empty_but_invalid_path_refuses(self):
        output, error = StringIO(), StringIO()
        with patch.dict(jev.os.environ, {}, clear=True), redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(jev.main(["pending", "--store", str(self.root)]), 0)
        self.assertEqual(json.loads(output.getvalue())["items"], [])
        self.assertFalse(self.root.exists())
        self.root.write_text("Not a directory")
        with self.assertRaisesRegex(jev.JevError, "restore access"):
            jev.pending_decisions(self.root)

    def test_evaluate_cli_retains_one_request_and_replays_without_api_credentials(self):
        state = self.root.parent / "state.json"
        state.write_text('{"assignment":"Bounded familiar work"}')
        response = dict(model="jev-1.14.0", usage=dict(input_tokens=11, output_tokens=3), answers={
            "task-demand": dict(type="choice", choice="routine", confidence=1,
                                probabilities=dict(routine=1, demanding=0, expert=0, uncertain=0))})
        argv = ["evaluate", "--state", str(state), "--store", str(self.root), "--model", "jev-1.14.0",
                "--evidence-revision", "fixture-1", "--question", "task-demand"]
        first, second, error = StringIO(), StringIO(), StringIO()
        with patch.object(jev, "Jev", return_value=self.client), redirect_stdout(first), redirect_stderr(error):
            self.client.request.return_value = response
            self.assertEqual(jev.main(argv), 0, error.getvalue())
        with patch.dict(jev.os.environ, {}, clear=True), patch.object(jev, "Jev") as client, redirect_stdout(second):
            self.assertEqual(jev.main(argv), 0)
            client.assert_not_called()
        self.assertEqual(json.loads(first.getvalue()), json.loads(second.getvalue()))
        self.assertEqual(self.client.request.call_count, 1)
