"""A bounded coordination question in the existing frozen assignment brief."""
import json
from pathlib import Path
import re
import sys

import factory
import jev

MARKER = "\n## Factory coordination question\n\n"
REPORT_GUIDANCE = ("Report outcome completed only for a supported non-uncertain/non-unresolved choice, with missing_input null. "
    "When missing evidence prevents that choice, report outcome blocked, choice null, and a concrete missing_input. "
    "These labels describe the original coordination question, not completion of this model turn.")


def owner(question):
    return "judge" if question == "investigation-resolution" else "standby-reasoner"


def binding(decision, question, role, *, write=False):
    if decision is None and question is None:
        return False
    if (not isinstance(decision, str) or not re.fullmatch(r"[0-9a-f]{64}", decision)
            or not isinstance(question, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", question)
            or role != owner(question) or write):
        raise factory.FactoryError("Bind a read-only standby/judge assignment to one exact decision and question; post-investigation cases require the judge")
    return True


def check_investigation(request, record):
    state = request["payload"]["state"]
    source = state.get("investigation") if isinstance(state, dict) else None
    assignment = record["assignments"].get(source.get("assignment")) if isinstance(source, dict) and isinstance(source.get("assignment"), str) else None
    if (not isinstance(source, dict) or assignment is None or source != dict(task=record["task"], assignment=source["assignment"], result=assignment["result"])
            or assignment["request"]["role"] != "investigator" or assignment["result"] is None
            or not factory.result_current(record, assignment["result"]) or assignment["result"]["outcome"] != "completed"):
        raise factory.FactoryError("Judge reassessment must include the exact current investigator result in state.investigation before launch")


def render(store, decision, question, role, inputs, *, task_record=None):
    if not binding(decision, question, role):
        raise factory.FactoryError("Supply the original decision and question before freezing coordination evidence")
    try:
        record = jev.read_decision(Path(store) / (decision + ".json"))
    except jev.JevError as exc:
        raise factory.FactoryError(str(exc)) from None
    answer = (record.get("response") or {}).get("answers", {}).get(question, {})
    if (record["state"] != "completed" or question not in record["request"]["payload"]["questions"]
            or answer.get("choice") not in ({"uncertain", "unresolved"} if role == "judge" else {"uncertain"})):
        raise factory.FactoryError("Select an unresolved completed Jev question; service failures require operational recovery, not a reasoner")
    if role == "judge":
        if task_record is None:
            raise factory.FactoryError("Supply the original admitted task for post-investigation adjudication")
        check_investigation(record["request"], task_record)
    value = dict(schema_version=1, decision=decision, question=question, inputs=inputs,
                 request=record["request"], response=record["response"])
    return "\n" + REPORT_GUIDANCE + "\n" + MARKER + json.dumps(value, allow_nan=False, ensure_ascii=True) + "\n"


def read(instructions, decision, question, role, inputs, *, write=False):
    if not binding(decision, question, role, write=write):
        return None
    _, marker, body = instructions.rpartition(MARKER)
    try:
        value = factory.decode_json(body)
        if (not marker or not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or value["schema_version"] != 1 or value.get("decision") != decision
                or value.get("question") != question or value.get("inputs") != inputs):
            raise ValueError("Changed coordination binding")
        jev.validate_request(value.get("request"))
        if jev.digest(value["request"]) != decision:
            raise ValueError("Changed original decision")
        jev.validate_response(value.get("response"), value["request"]["payload"]["questions"])
        if value["response"]["model"] != value["request"]["model_version"]:
            raise ValueError("Changed classifier version")
        answer = value["response"]["answers"][question]
        if answer["choice"] not in ({"uncertain", "unresolved"} if role == "judge" else {"uncertain"}):
            raise ValueError("Not an unresolved question")
        return value
    except (ValueError, KeyError, jev.JevError):
        raise factory.FactoryError("Frozen coordination evidence changed; inspect the original brief, decision and question without relaunching") from None


def answer_schema(evidence):
    return dict(type="object", additionalProperties=False, required=["decision", "question", "choice", "missing_input"],
        properties=dict(decision=dict(type="string", enum=[evidence["decision"]]),
            question=dict(type="string", enum=[evidence["question"]]),
            choice=dict(type=["string", "null"], enum=[*evidence["request"]["payload"]["questions"][evidence["question"]]["criteria"], None]),
            missing_input=dict(type=["string", "null"])))


def validate_answer(answer, evidence, outcome):
    if (not isinstance(answer, dict) or set(answer) != {"decision", "question", "choice", "missing_input"}
            or answer["decision"] != evidence["decision"] or answer["question"] != evidence["question"]
            or outcome not in {"completed", "blocked"}):
        raise factory.FactoryError("Reasoner answer differs from its bounded question; inspect its original event stream")
    choices = evidence["request"]["payload"]["questions"][evidence["question"]]["criteria"]
    choice, missing = answer["choice"], answer["missing_input"]
    if outcome == "completed":
        valid = isinstance(choice, str) and choice in choices and choice not in {"uncertain", "unresolved"} and missing is None
    else:
        valid = (choice is None or isinstance(choice, str) and choice in choices and choice in {"uncertain", "unresolved"}) and isinstance(missing, str) and bool(missing.strip())
    if not valid:
        raise factory.FactoryError("Return a supported selected answer or a concrete missing input; uncertainty cannot silently resolve the queue")


def collected_attempts(directory):
    """Generate answer views from task/receipt owners, without contacting Rex."""
    import codex_worker
    attempts = {}
    if not directory.exists():
        return attempts
    try:
        paths = sorted(path for path in directory.iterdir() if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}\.json", path.name))
        for path in paths:
            with factory.task_lock(directory, path.stem):
                record = factory.load_task(path)
            for identity, assignment in record["assignments"].items():
                command = assignment["request"]["command"]
                if (command[:4] != [sys.executable, "-E", "-s", str(factory.ROOT / "codex_worker.py")]
                        or "--decision" not in command):
                    continue
                try:
                    args = codex_worker.parser().parse_args(command[4:])
                except SystemExit:
                    raise factory.FactoryError("Restore the original bounded reasoner command before reading its answer") from None
                binding(args.decision, args.question, assignment["request"]["role"], write=args.write)
                item = dict(task=record["task"], assignment=identity, evidence=str(path), state="awaiting_collection", answer=None)
                if assignment["request"].get("inputs") != record["inputs"]:
                    item["state"] = "stale"
                elif assignment["result"] is not None:
                    _, receipt, native = factory.codex_evidence(identity, record, assignment)
                    item.update(state=native["report"]["outcome"], answer=native["report"]["answer"],
                                evidence=str(args.receipt), inputs=receipt["inputs"])
                attempts.setdefault((args.decision, args.question), []).append(item)
        return attempts
    except (OSError, ValueError) as exc:
        raise factory.FactoryError("Cannot read reasoner evidence; restore the original task, brief, receipt and event stream. " + str(exc)) from None
