"""Bounded TypeSafe Choice requests. Shadow evaluation only; no execution authority."""
import argparse
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
API = "https://api.typesafe.ai/v1/"


class JevError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Bearer credentials must stay on the selected API origin.
        return None


def compile_questions(specs, candidates=None):
    """Expand the factory catalog into the documented HTTP Choice contract."""
    questions = {}
    candidates = candidates or {}
    for spec in specs:
        identity = spec["id"]
        if identity in questions or not identity or spec["primitive"] != "choice":
            raise JevError("Supply unique named Choice questions")
        if type(spec["version"]) is not int or spec["version"] < 1:
            raise JevError("Each question needs a positive version")
        criteria = dict(spec["options"])
        if not criteria.get("uncertain"):
            raise JevError("Every question must include an explicit uncertain option")
        if "candidate" in criteria:
            if identity not in candidates or not isinstance(candidates[identity], dict):
                raise JevError("Supply eligible candidate IDs and descriptions for " + identity)
            del criteria["candidate"]
            for candidate, description in candidates[identity].items():
                if (not isinstance(candidate, str) or not candidate or candidate == "candidate"
                        or candidate in criteria or not isinstance(description, str) or not description.strip()):
                    raise JevError("Candidate IDs must be unique and distinct from reserved outcomes")
                criteria[candidate] = description
        elif identity in candidates:
            raise JevError("This question does not accept candidate options: " + identity)
        if not 2 <= len(criteria) <= 255:
            raise JevError("Choice questions require between 2 and 255 options")
        questions[identity] = dict(type="choice", instructions=spec["instructions"], criteria=criteria)
    if not questions or set(candidates) - set(questions):
        raise JevError("Select at least one question and supply candidates only for selected questions")
    return questions


def probability(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def validate_response(response, questions):
    if not isinstance(response, dict) or not isinstance(response.get("model"), str) or not response["model"]:
        raise JevError("TypeSafe returned no model identity; retain the evidence and use standby reasoning")
    answers = response.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise JevError("TypeSafe returned mismatched questions; retain the evidence and use standby reasoning")
    for identity, question in questions.items():
        answer = answers[identity]
        if not isinstance(answer, dict):
            raise JevError("TypeSafe returned an invalid Choice answer; use standby reasoning")
        distribution = answer.get("probabilities")
        if (answer.get("type") != "choice" or not isinstance(answer.get("choice"), str)
                or answer["choice"] not in question["criteria"] or not probability(answer.get("confidence"))
                or not isinstance(distribution, dict) or set(distribution) != set(question["criteria"])
                or not all(probability(value) for value in distribution.values())
                or distribution[answer["choice"]] < max(distribution.values())
                or not math.isclose(sum(distribution.values()), 1, abs_tol=1e-6)):
            raise JevError("TypeSafe returned an invalid Choice answer; use standby reasoning")
    usage = response.get("usage")
    if not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0
                                          for key in ("input_tokens", "output_tokens")):
        raise JevError("TypeSafe usage is unavailable; do not invent consumption")
    return response


class Jev:
    def __init__(self, api_key=None, *, key_file=None, opener=None):
        if key_file is not None:
            if api_key is not None:
                raise JevError("Choose one credential source: an API key or a private key file")
            api_key = Path(key_file).read_text().strip()
        elif api_key is None:
            api_key = os.environ.get("TYPESAFE_API_KEY")
        if not api_key or not api_key.strip() or "\r" in api_key or "\n" in api_key:
            raise JevError("Set TYPESAFE_API_KEY in the execution environment before making a live request")
        self.api_key = api_key
        self.opener = opener if opener is not None else urllib.request.build_opener(NoRedirect())

    def request(self, path, payload=None):
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        request = urllib.request.Request(API + path, data=body, headers={
            "Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            # API bodies and exception strings may echo credentials or private state.
            status = exc.code
            exc.close()
            if status in (400, 413, 415, 422):
                action = "repair the request state/questions/model or size before another evaluation"
            elif status in (401, 403):
                action = "check TYPESAFE_API_KEY and account permissions before another request"
            elif status == 429:
                action = "retain pending work and wait for account capacity to recover"
            elif status >= 500:
                action = "retain pending work and inspect service availability before another request"
            else:
                action = "inspect the request endpoint and service status before another request"
            raise JevError(f"TypeSafe HTTP {status}; {action}") from None
        except (OSError, http.client.HTTPException):
            raise JevError("TypeSafe transport unavailable; retain pending work instead of retrying into a decision") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise JevError("TypeSafe returned invalid JSON; retain the evidence and use standby reasoning") from None

    def models(self):
        response = self.request("models")
        models = response.get("models") if isinstance(response, dict) else None
        if not isinstance(models, list) or any(not isinstance(item, dict)
                or not isinstance(item.get("name"), str) or not item["name"] for item in models):
            raise JevError("TypeSafe model discovery failed; do not invent a model fallback")
        return models

    def evaluate(self, *, state, specs, model, evidence_revision, candidates=None) -> dict:
        if not model.strip() or not evidence_revision.strip():
            raise JevError("Supply a discovered model name and an explicit evidence revision")
        questions = compile_questions(specs, candidates)
        payload = dict(model=model, state=state, questions=questions)
        request_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
        response = validate_response(self.request("systemone", payload), questions)
        return dict(schema_version=1, mode="shadow", evidence_revision=evidence_revision,
                    question_versions={spec["id"]: spec["version"] for spec in specs},
                    request_sha256=request_hash, response=response)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-file", type=Path, help="Private file containing only the API key; overrides the environment")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("models", help="Discover models on the authenticated TypeSafe account")
    for action in ("compile", "evaluate"):
        command = sub.add_parser(action)
        command.add_argument("--question", action="append", required=True)
        command.add_argument("--candidates", type=Path, help="JSON map of question IDs to eligible ID/description maps")
        if action == "evaluate":
            command.add_argument("--state", type=Path, required=True)
            command.add_argument("--model", required=True)
            command.add_argument("--evidence-revision", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "models":
            result = {"models": Jev(key_file=args.key_file).models()}
        else:
            catalog = json.loads((ROOT / "config/coordination.json").read_text())["questions"]
            selected = set(args.question)
            specs = [spec for spec in catalog if spec["id"] in selected]
            if len(specs) != len(args.question):
                raise JevError("Select unique question IDs from config/coordination.json")
            candidates = json.loads(args.candidates.read_text()) if args.candidates else None
            if args.action == "compile":
                result = {"questions": compile_questions(specs, candidates)}
            else:
                result = Jev(key_file=args.key_file).evaluate(
                    state=json.loads(args.state.read_text()), specs=specs, model=args.model,
                    evidence_revision=args.evidence_revision, candidates=candidates)
        print(json.dumps(result, indent=2, allow_nan=False))
    except (JevError, OSError, ValueError, KeyError, TypeError) as exc:
        print("jev: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
