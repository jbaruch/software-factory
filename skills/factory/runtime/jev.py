"""Bounded TypeSafe Choice requests. Shadow evaluation only; no execution authority."""
import argparse
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True  # Installed copies stay free of generated files.
import time
import urllib.error
import urllib.request

if __name__ == "__main__":
    sys.modules.setdefault("jev", sys.modules[__name__])

import factory
from factory import decode_json

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
    return type(value) in (int, float) and 0 <= value <= 1


def validate_response(response, questions):
    if not isinstance(response, dict) or not isinstance(response.get("model"), str) or not response["model"].strip():
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
    def __init__(self, api_key=None, *, opener=None):
        if api_key is None:
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
                return decode_json(response.read())
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
        except (ValueError, UnicodeDecodeError):
            raise JevError("TypeSafe returned invalid JSON; retain the evidence and use standby reasoning") from None

    def models(self):
        response = self.request("models")
        models = response.get("models") if isinstance(response, dict) else None
        if not isinstance(models, list) or any(not isinstance(item, dict)
                or not isinstance(item.get("name"), str) or not item["name"].strip() for item in models):
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


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def validate_request(request):
    """Validate stored and frozen requests through one contract."""
    if (not isinstance(request, dict) or set(request) != {"endpoint", "evidence_revision", "question_versions", "model_version", "payload"}
            or request["endpoint"] != API + "systemone"
            or not isinstance(request["evidence_revision"], str) or not request["evidence_revision"].strip()
            or not isinstance(request["question_versions"], dict)
            or not isinstance(request["payload"], dict)
            or set(request["payload"]) != {"model", "state", "questions"}
            or not isinstance(request["payload"]["model"], str)
            or not request["payload"]["model"].strip()
            or not isinstance(request["model_version"], str)
            or not re.fullmatch(r"jev-[0-9]+\.[0-9]+\.[0-9]+", request["model_version"])
            or not isinstance(request["payload"]["state"], (str, dict, list))
            or not isinstance(request["payload"]["questions"], dict)
            or not request["payload"]["questions"]
            or set(request["question_versions"]) != set(request["payload"]["questions"])
            or any(type(version) is not int or version < 1 for version in request["question_versions"].values())):
        raise JevError("Invalid retained request")
    for question in request["payload"]["questions"].values():
        if (not isinstance(question, dict) or question.get("type") != "choice"
                or not isinstance(question.get("instructions"), (str, dict, list))
                or not question["instructions"] or not isinstance(question.get("criteria"), dict)
                or not 2 <= len(question["criteria"]) <= 255
                or not question["criteria"].get("uncertain")
                or any(not key.strip() or not isinstance(value, str) or not value.strip()
                       for key, value in question["criteria"].items())):
            raise JevError("Invalid retained question")


def read_decision(path) -> dict:
    """Read the owner's version-1 decision record; unknown versions hold retries."""
    try:
        record = decode_json(factory.read_record(path))
        json.dumps(record, allow_nan=False)
        if (not isinstance(record, dict) or type(record.get("schema_version")) is not int
                or record["schema_version"] != 1 or record.get("kind") != "jev-decision"
                or set(record) != {"schema_version", "kind", "mode", "id", "request", "state", "created_at",
                                   "finished_at", "request_seconds", "response", "error"}
                or record.get("mode") != "shadow" or not isinstance(record.get("state"), str)
                or record["state"] not in {"pending", "completed", "failed"}
                or not isinstance(record.get("request"), dict) or digest(record["request"]) != path.stem
                or record.get("id") != path.stem
                or type(record.get("created_at")) not in (int, float) or not 0 <= record["created_at"] <= sys.float_info.max):
            raise ValueError("Unsupported decision record")
        request = record["request"]
        validate_request(request)
        if record["state"] == "pending":
            if any(record.get(key) is not None for key in ("finished_at", "request_seconds", "response", "error")):
                raise ValueError("Pending record has a result")
        else:
            if any(type(record.get(key)) not in (int, float) or not 0 <= record[key] <= sys.float_info.max
                   for key in ("finished_at", "request_seconds")):
                raise ValueError("Invalid decision timing")
            if record["state"] == "completed":
                validate_response(record.get("response"), request["payload"]["questions"])
                if record["response"]["model"] != request["model_version"] or record.get("error") is not None:
                    raise ValueError("Resolved model differs from pinned request")
            else:
                if not isinstance(record.get("error"), str) or not record["error"].strip():
                    raise ValueError("Failed record has no supported failure")
                if record["response"] is not None:
                    validate_response(record["response"], request["payload"]["questions"])
                    if record["response"]["model"] == request["model_version"]:
                        raise ValueError("Failed record has no model mismatch")
        return record
    except (OSError, ValueError, UnicodeError, JevError):
        raise JevError("Cannot read the retained decision; restore its version-1 record and inspect the original request before any retry") from None


def evaluate_stored(directory, *, state, specs, model, evidence_revision, candidates=None,
                    model_version=None, client_factory=None, clock=time.time, timer=time.monotonic) -> dict:
    """Persist one intent per exact evidence/question/model input; never auto-retry."""
    model_version = model if model_version is None else model_version
    if (not model.strip() or not re.fullmatch(r"jev-[0-9]+\.[0-9]+\.[0-9]+", model_version)
            or not evidence_revision.strip()):
        raise JevError("Select a discovered model and explicit evidence revision; supply --model-version with the expected versioned Jev identity when requesting an alias")
    if not isinstance(state, (str, dict, list)):
        raise JevError("Supply state as a JSON string, object or array before storing a decision")
    request: dict = dict(endpoint=API + "systemone", evidence_revision=evidence_revision, model_version=model_version,
                   question_versions={spec["id"]: spec["version"] for spec in specs},
                   payload=dict(model=model, state=state, questions=compile_questions(specs, candidates)))
    request = decode_json(json.dumps(request, sort_keys=True, allow_nan=False))
    identity = digest(request)
    try:
        with factory.task_lock(directory, identity) as path:
            if path.exists():
                record = read_decision(path)
                if record["request"] != request:
                    raise JevError("Decision identity differs from its retained inputs; inspect the store before execution")
                if record["state"] != "completed":
                    raise JevError("This decision has an unfinished or failed request; inspect its original evidence and service/account status before authorizing recovery. No request was repeated")
                return record
            # Resolve credentials locally before intent. No provider call has happened yet.
            client = (client_factory or Jev)()
            record = dict(schema_version=1, kind="jev-decision", mode="shadow", id=identity,
                          request=request, state="pending", created_at=clock(), finished_at=None,
                          request_seconds=None, response=None, error=None)
            factory.save(path, record, exclusive=True)
            started = timer()
            response = None
            try:
                response = validate_response(client.request("systemone", request["payload"]), request["payload"]["questions"])
                if response["model"] != model_version:
                    raise JevError("TypeSafe returned a different model; inspect its identity and select the discovered pinned version before a new evaluation")
            except JevError as exc:
                record.update(state="failed", finished_at=clock(), request_seconds=timer() - started,
                              response=response, error=str(exc))
                factory.save(path, record)
                raise
            record.update(state="completed", finished_at=clock(), request_seconds=timer() - started, response=response)
            factory.save(path, record)
            return record
    except factory.FactoryError as exc:
        raise JevError(str(exc)) from None


def pending_decisions(directory, *, task_state_dir=None) -> dict:
    """Derive the shadow inbox from original records; this never dispatches work."""
    try:
        try:
            paths = sorted(path for path in directory.iterdir() if path.suffix == ".json")
        except FileNotFoundError:
            try:
                directory.lstat()
            except FileNotFoundError:
                paths = []  # The owner's first-use semantics; never create a store here.
            else:
                raise JevError("Selected decision store is a dangling link; restore the original directory") from None
        attempts, resolutions = {}, []
        if task_state_dir is not None:
            import coordination_evidence
            attempts = coordination_evidence.collected_attempts(task_state_dir)
        items = []
        for path in paths:
            record = read_decision(path)
            if record["state"] != "completed":
                items.append(dict(decision=record["id"], question=None, owner="operator", reason=record["state"],
                                  error=record["error"],
                                  evidence=str(path), evidence_revision=record["request"]["evidence_revision"]))
                continue
            for question, answer in record["response"]["answers"].items():
                choice = answer["choice"]
                if choice == "uncertain" or question == "investigation-resolution" and choice == "unresolved":
                    item: dict = dict(decision=record["id"], question=question,
                        owner="judge" if question == "investigation-resolution" else "standby-reasoner",
                        reason=choice, evidence=str(path), evidence_revision=record["request"]["evidence_revision"])
                    sources = attempts.get((record["id"], question), [])
                    current = [source for source in sources if source["state"] != "stale"]
                    sources = current or sources
                    if len(sources) == 1 and sources[0]["state"] == "completed":
                        resolutions.append(dict(decision=record["id"], question=question, **sources[0]))
                        continue
                    if sources:
                        item.update(owner="operator", reason="conflicting_attempts" if len(sources) > 1 else sources[0]["state"],
                                    attempts=sources)
                    items.append(item)
        result = dict(schema_version=2 if task_state_dir is not None else 1, kind="jev-inbox", mode="shadow", items=items)
        if task_state_dir is not None:
            result["resolutions"] = resolutions
        return result
    except factory.FactoryError as exc:
        raise JevError("Cannot read bounded reasoner evidence; inspect its original task and artifacts. " + str(exc)) from None
    except OSError:
        raise JevError("Cannot list decision records; restore access to the selected --store before reading the inbox") from None


def read_input(path, option):
    try:
        value = decode_json(path.read_text())
        json.dumps(value, allow_nan=False)  # Refuse non-standard constants and numeric overflow.
        if option == "--candidates":
            if not isinstance(value, dict) or any(not isinstance(items, dict) or any(
                    not key.strip() or not isinstance(description, str) or not description.strip()
                    for key, description in items.items()) for items in value.values()):
                raise ValueError("Candidates require question-to-ID/description maps")
        elif not isinstance(value, (str, dict, list)):
            raise ValueError("State requires a string, object or array")
        return value
    except (OSError, ValueError, UnicodeDecodeError):
        shape = "question-to-ID/description maps" if option == "--candidates" else "a string, object or array"
        raise JevError(f"Cannot read {option} file {str(path)!r}; restore access and repair its UTF-8 JSON as {shape} before retrying") from None


def read_catalog():
    path = ROOT / "config/coordination.json"
    try:
        catalog = decode_json(path.read_text())
        json.dumps(catalog, allow_nan=False)
        if (not isinstance(catalog, dict) or type(catalog.get("schema_version")) is not int
                or catalog["schema_version"] != 1 or not isinstance(catalog.get("questions"), list)):
            raise ValueError("Invalid question catalog")
        specs = catalog["questions"]
        if any(not isinstance(spec, dict) or not isinstance(spec.get("id"), str)
               or not isinstance(spec.get("options"), dict)
               or any(not key.strip() or not isinstance(description, str) or not description.strip()
                      for key, description in spec["options"].items())
               or not isinstance(spec.get("instructions"), (str, dict, list)) or not spec["instructions"]
               for spec in specs):
            raise ValueError("Invalid question specification")
        # Check source structure without supplying or choosing real routing candidates.
        placeholders = {spec["id"]: {} for spec in specs if "candidate" in spec["options"]}
        compile_questions(specs, placeholders)
        return specs
    except (OSError, ValueError, UnicodeDecodeError, KeyError, TypeError, JevError):
        raise JevError(f"Cannot read question catalog {str(path)!r}; restore access and repair its version-1 questions before compiling or evaluating") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("models", help="Discover models on the authenticated TypeSafe account")
    pending = sub.add_parser("pending", help="Read retained uncertain/failed shadow decisions without calling providers")
    pending.add_argument("--store", type=Path, required=True)
    pending.add_argument("--task-state-dir", type=Path, help="Include already collected bounded native answers; no Rex queries")
    for action in ("compile", "evaluate"):
        command = sub.add_parser(action)
        command.add_argument("--question", action="append", required=True)
        command.add_argument("--candidates", type=Path, help="JSON map of question IDs to eligible ID/description maps")
        if action == "evaluate":
            command.add_argument("--state", type=Path, required=True)
            command.add_argument("--model", required=True)
            command.add_argument("--evidence-revision", required=True)
            command.add_argument("--store", type=Path, help="Retain decisions and reuse exact versioned inputs")
            command.add_argument("--model-version", help="Expected resolved version for stored requests made through an alias")
    args = parser.parse_args(argv)
    try:
        if args.action == "pending":
            result = pending_decisions(args.store, task_state_dir=args.task_state_dir)
        elif args.action == "models":
            result = {"models": Jev().models()}
        else:
            catalog = read_catalog()
            selected = set(args.question)
            specs = [spec for spec in catalog if spec["id"] in selected]
            if len(specs) != len(args.question):
                raise JevError("Select unique question IDs from config/coordination.json")
            candidates = read_input(args.candidates, "--candidates") if args.candidates else None
            if args.action == "compile":
                result = {"questions": compile_questions(specs, candidates)}
            else:
                state = read_input(args.state, "--state")
                options = dict(state=state, specs=specs, model=args.model,
                               evidence_revision=args.evidence_revision, candidates=candidates)
                if args.model_version and not args.store:
                    raise JevError("Use --model-version with --store so the resolved identity is verified and retained")
                result = evaluate_stored(args.store, model_version=args.model_version, **options) if args.store else Jev().evaluate(**options)
        print(json.dumps(result, indent=2, allow_nan=False))
    except (JevError, OSError, ValueError, KeyError, TypeError) as exc:
        print("jev: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
