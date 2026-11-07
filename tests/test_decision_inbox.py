"""Fixed offline inbox/control regressions; no private runtime state or live effects."""
import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
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
        self.assertEqual(saved["schema_version"], 7)
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
