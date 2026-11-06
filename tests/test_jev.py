"""Fixed transport fixtures only: no credentials, network or live model judgments."""
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import jev

SPEC: dict = dict(id="test-route", primitive="choice", version=1, instructions="Who owns this finding?",
            options={"qa": "Acceptance coverage", "backend": "Service behavior", "uncertain": "Insufficient evidence"})
RESPONSE: dict = dict(model="fixture-model-v1", usage=dict(input_tokens=17, output_tokens=4), answers={
    "test-route": dict(type="choice", choice="qa", confidence=0.9,
                       probabilities={"qa": 0.9, "backend": 0.05, "uncertain": 0.05})})


class FakeOpener:
    def __init__(self, response):
        self.response, self.requests = response, []

    def open(self, request, *, timeout):
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return BytesIO(json.dumps(self.response).encode())


class JevAdapter(unittest.TestCase):
    def test_damaged_question_catalog_names_repair_before_provider_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            path = root / "config/coordination.json"
            for payload in (None, b"\xff", b"{", b"[]", b'{"schema_version":1,"questions":{}}',
                            b'{"schema_version":1,"questions":[{"id":"task-demand"}]}',
                            *[json.dumps(dict(schema_version=1, questions=[{**SPEC, **change}])).encode()
                              for change in (dict(options={"yes": {}, "uncertain": 1}),
                                             dict(options={"yes": " ", "uncertain": "Unsure"}),
                                             dict(instructions={"nested": float("nan")}))]):
                if payload is None:
                    if path.exists():
                        path.unlink()
                else:
                    path.write_bytes(payload)
                errors = StringIO()
                with self.subTest(payload=payload), patch.object(jev, "ROOT", root), patch.object(
                        jev, "Jev", side_effect=AssertionError("No provider access")), redirect_stderr(errors):
                    result = jev.main(["evaluate", "--question", "task-demand", "--state", str(root / "state.json"),
                                       "--model", "fixture-model", "--evidence-revision", "fixture"])
                self.assertEqual(result, 1)
                self.assertIn("config/coordination.json", errors.getvalue())
                self.assertIn("restore access and repair its version-1 questions", errors.getvalue())

    def evaluate(self, response=None, **changes):
        opener = FakeOpener(RESPONSE if response is None else response)
        client = jev.Jev("not-a-real-key", opener=opener)
        arguments = dict(state={"finding": "Coverage is missing"}, specs=[SPEC],
                         model="fixture-model-v1", evidence_revision="evidence-v1")
        return client.evaluate(**{**arguments, **changes}), opener

    def test_compiler_matches_documented_choice_wire_shape(self):
        self.assertEqual(jev.compile_questions([SPEC]), {"test-route": {
            "type": "choice", "instructions": "Who owns this finding?",
            "criteria": {"qa": "Acceptance coverage", "backend": "Service behavior",
                         "uncertain": "Insufficient evidence"}}})

    def test_candidate_ids_expand_without_generic_placeholder_or_guessed_fallback(self):
        spec = {**SPEC, "options": {"candidate": "Supplied owners", "no_match": "No owner", "uncertain": "Unclear"}}
        questions = jev.compile_questions([spec], {"test-route": {"qa-worker": "Coverage owner"}})
        self.assertEqual(questions["test-route"]["criteria"],
                         {"qa-worker": "Coverage owner", "no_match": "No owner", "uncertain": "Unclear"})
        empty = jev.compile_questions([spec], {"test-route": {}})
        self.assertEqual(set(empty["test-route"]["criteria"]), {"no_match", "uncertain"})
        for candidates in (None, {"test-route": {"uncertain": "Collision"}},
                           {"test-route": {"candidate": "Placeholder"}}):
            with self.subTest(candidates=candidates), self.assertRaises(jev.JevError):
                jev.compile_questions([spec], candidates)

    def test_uncertainty_and_version_are_required_before_inference(self):
        for spec in ({**SPEC, "options": {"qa": "Only owner"}}, {**SPEC, "version": 0}):
            with self.subTest(spec=spec), self.assertRaises(jev.JevError):
                jev.compile_questions([spec])
        with self.assertRaises(jev.JevError):
            jev.compile_questions([SPEC, SPEC])

    def test_evaluation_is_bound_shadow_output_and_uses_one_http_request(self):
        result, opener = self.evaluate()
        self.assertEqual(result["mode"], "shadow")
        self.assertEqual(result["evidence_revision"], "evidence-v1")
        self.assertEqual(result["question_versions"], {"test-route": 1})
        self.assertEqual(result["response"], RESPONSE)
        self.assertEqual(len(opener.requests), 1)
        request = opener.requests[0]
        self.assertEqual(request.full_url, "https://api.typesafe.ai/v1/systemone")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data)["state"], {"finding": "Coverage is missing"})
        changed, _ = self.evaluate(state={"finding": "Different evidence"})
        self.assertNotEqual(result["request_sha256"], changed["request_sha256"])

    def test_independent_questions_are_batched_and_explicit_uncertainty_is_preserved(self):
        second = {**SPEC, "id": "other-route"}
        response = {**RESPONSE, "answers": {**RESPONSE["answers"], "other-route": {
            "type": "choice", "choice": "uncertain", "confidence": 1.0,
            "probabilities": {"qa": 0, "backend": 0, "uncertain": 1}}}}
        result, opener = self.evaluate(response, specs=[SPEC, second])
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(result["response"]["answers"]["other-route"]["choice"], "uncertain")

    def test_unknown_or_malformed_answers_never_become_clear_routes(self):
        original = RESPONSE["answers"]["test-route"]
        variants = [{**original, "choice": "invented-owner"}, {**original, "confidence": float("nan")},
                    {**original, "probabilities": {"qa": 1}}, {**original, "confidence": True},
                    {**original, "probabilities": {"qa": 1, "backend": 1, "uncertain": 1}},
                    {**original, "probabilities": {"qa": 0.1, "backend": 0.8, "uncertain": 0.1}}]
        for answer in variants:
            with self.subTest(answer=answer), self.assertRaises(jev.JevError):
                self.evaluate({**RESPONSE, "answers": {"test-route": answer}})
        for response in ({**RESPONSE, "answers": {}}, {**RESPONSE, "usage": None}, {**RESPONSE, "model": ""}):
            with self.subTest(response=response), self.assertRaises(jev.JevError):
                self.evaluate(response)

    def test_tied_maximum_choice_is_valid(self):
        answer = {**RESPONSE["answers"]["test-route"],
                  "probabilities": {"qa": 0.5, "backend": 0.5, "uncertain": 0}}
        result, _ = self.evaluate({**RESPONSE, "answers": {"test-route": answer}})
        self.assertEqual(result["response"]["answers"]["test-route"]["choice"], "qa")

    def test_http_diagnostics_distinguish_request_repairs_from_account_or_service_waits(self):
        for status, action in ((400, "repair the request"), (422, "repair the request"),
                               (401, "TYPESAFE_API_KEY"), (429, "wait for account capacity"),
                               (503, "service availability"), (404, "request endpoint")):
            opener = FakeOpener(urllib.error.HTTPError(jev.API, status, "private data", Message(),
                                                     BytesIO(b"private data")))
            client = jev.Jev("not-a-real-key", opener=opener)
            with self.subTest(status=status), self.assertRaises(jev.JevError) as caught:
                client.models()
            self.assertIn(action, str(caught.exception))
            self.assertNotIn("private data", str(caught.exception))
            self.assertEqual(len(opener.requests), 1)

    def test_service_errors_do_not_retry_or_expose_api_body(self):
        for error in (urllib.error.HTTPError(jev.API, 429, "not-a-real-key", Message(), BytesIO(b"private state")),
                      urllib.error.URLError("not-a-real-key"), TimeoutError("not-a-real-key")):
            opener = FakeOpener(error)
            client = jev.Jev("not-a-real-key", opener=opener)
            with self.subTest(error=type(error).__name__), self.assertRaises(jev.JevError) as caught:
                client.evaluate(state="fixture", specs=[SPEC], model="fixture-model-v1", evidence_revision="v1")
            self.assertEqual(len(opener.requests), 1)
            self.assertNotIn("not-a-real-key", str(caught.exception))
            self.assertNotIn("private state", str(caught.exception))

    def test_model_discovery_preserves_an_empty_catalog_and_rejects_missing_data(self):
        self.assertEqual(jev.Jev("not-a-real-key", opener=FakeOpener({"models": []})).models(), [])
        models = [{"name": "fixture-model-v1", "description": "Fixture", "release_date": "2000-01-01"}]
        opener = FakeOpener({"models": models})
        self.assertEqual(jev.Jev("not-a-real-key", opener=opener).models(), models)
        self.assertEqual(opener.requests[0].full_url, "https://api.typesafe.ai/v1/models")
        self.assertEqual(opener.requests[0].get_method(), "GET")
        with self.assertRaises(jev.JevError):
            jev.Jev("not-a-real-key", opener=FakeOpener({})).models()

    def test_cli_compile_is_offline_and_missing_credentials_fail_actionably(self):
        output, errors = StringIO(), StringIO()
        with patch.dict(jev.os.environ, {}, clear=True), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(jev.main(["compile", "--question", "task-demand"]), 0)
            self.assertEqual(jev.main(["models"]), 1)
        self.assertIn("uncertain", json.loads(output.getvalue())["questions"]["task-demand"]["criteria"])
        self.assertIn("Set TYPESAFE_API_KEY", errors.getvalue())

    def test_environment_supplies_credentials_without_echoing_them(self):
        opener = FakeOpener({"models": []})
        with patch.dict(jev.os.environ, {"TYPESAFE_API_KEY": "not-a-real-key"}):
            client = jev.Jev(opener=opener)
        self.assertEqual(client.models(), [])
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer not-a-real-key")

    def test_damaged_cli_inputs_identify_the_file_and_repair_before_network_access(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            for option in ("--state", "--candidates"):
                for data in (None, b'{"truncated":', b'\xff', b'1', b'null', b'true',
                             b'NaN', b'{"invalid": Infinity}', b'{"invalid": 1e309}',
                             b'{"duplicate": {}, "duplicate": {}}'):
                    if data is not None:
                        path.write_bytes(data)
                    elif path.exists():
                        path.unlink()
                    argv = ["evaluate", "--question", "task-demand", "--model", "fixture-model",
                            "--evidence-revision", "v1", "--state", str(path)]
                    if option == "--candidates":
                        argv.extend(["--candidates", str(path)])
                    errors = StringIO()
                    with patch.object(jev, "Jev") as client, redirect_stderr(errors):
                        self.assertEqual(jev.main(argv), 1)
                    self.assertIn(option + " file", errors.getvalue())
                    self.assertIn(str(path), errors.getvalue())
                    self.assertIn("repair its UTF-8 JSON", errors.getvalue())
                    client.assert_not_called()

    def test_state_file_preserves_all_documented_top_level_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            for state in ("Relevant evidence", {"count": 1}, ["first", "second"]):
                path.write_text(json.dumps(state))
                self.assertEqual(jev.read_input(path, "--state"), state)


if __name__ == "__main__":
    unittest.main()
