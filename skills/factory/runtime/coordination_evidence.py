"""A bounded coordination question in the existing frozen assignment brief."""
import json
import asyncio
from pathlib import Path
import re
import sys
import time

import factory
import jev

MARKER = "\n## Factory coordination question\n\n"
REPORT_GUIDANCE = ("Report outcome completed only for a supported non-uncertain/non-unresolved choice, with missing_input null. "
    "When missing evidence prevents that choice, report outcome blocked, choice null, and a concrete missing_input. "
    "These labels describe the original coordination question, not completion of this model turn.")


def owner(question):
    return "judge" if question == "investigation-resolution" else "standby-reasoner"


def judge_model(harness="codex"):
    try:
        policy = factory.decode_json((factory.ROOT / "config/coordination.json").read_text())
        model = policy["policies"]["judge_models"][harness]
        if not isinstance(model, str) or not model.strip():
            raise ValueError("Missing judge model")
        return model
    except (OSError, ValueError, KeyError, TypeError):
        raise factory.FactoryError(f"Configure judge_models.{harness} from eligible highly capable native models before judging") from None


def validate_claude_judge(evidence, model, effort):
    """A Claude judge runs the configured model at Claude Code's maximum effort; Claude publishes no quota."""
    configured = judge_model("claude")
    if (evidence.get("judge_harness") != "claude" or "judge_capacity" in evidence
            or evidence.get("judge_model") != configured or model != configured):
        raise factory.FactoryError("Select the configured judge_model; arbitrary model choices cannot adjudicate this case")
    if effort != "max":
        raise factory.FactoryError("Judge requires the selected model's maximum supported effort")


def validate_judge_capacity(capacity, binary, model, effort, configured_model, now):
    import ready_runner
    if model != configured_model or not isinstance(configured_model, str) or not configured_model.strip():
        raise factory.FactoryError("Select the configured judge_model; arbitrary model choices cannot adjudicate this case")
    try:
        reason = ready_runner.capacity_reason(capacity, binary, now, 0, dict(model=model, effort=effort))
        if reason:
            raise factory.FactoryError(reason)
        selected = [item for item in capacity["models"] if item["model"] == model]
        rank = factory.EFFORT_RANK
        efforts = selected[0]["effort_ids"] if len(selected) == 1 else None
        if (not isinstance(efforts, list) or not efforts or any(not isinstance(item, str) or item not in rank for item in efforts)
                or len(set(efforts)) != len(efforts)):
            raise factory.FactoryError("Judge effort ordering is unavailable; refresh the native catalog and update supported effort ranks")
        if effort != max(efforts, key=rank.index):
            raise factory.FactoryError("Judge requires the selected model's maximum supported effort from fresh native evidence")
    except (KeyError, TypeError, AttributeError, ValueError):
        raise factory.FactoryError("Judge capability evidence is malformed; refresh the curated native capacity observation before judging") from None


def dispute(request):
    state = request["payload"]["state"]
    case = state.get("dispute") if isinstance(state, dict) else None
    if (not isinstance(case, dict) or set(case) != {"question", "options"}
            or not isinstance(case["question"], str) or not case["question"].strip()
            or not isinstance(case["options"], dict) or not 2 <= len(case["options"]) <= 255
            or any(not key.strip() or not isinstance(value, str) or not value.strip() for key, value in case["options"].items())):
        raise factory.FactoryError("Supply the original dispute question and bounded ruling options in state.dispute before judging")
    return case


def report_guidance(value):
    if owner(value["question"]) == "judge":
        return ("Adjudicate the original state.dispute using its supplied ruling options; do not re-answer the investigation-resolution classifier. "
            "Report completed only with a supported ruling, concrete consequences, and missing_input null. "
            "If evidence or authority is missing, report blocked, ruling null, consequences [], and a concrete missing_input.")
    return REPORT_GUIDANCE


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
    dispute(request)


def render(store, decision, question, role, inputs, *, task_record=None, native=None, harness="codex"):
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
    value: dict = dict(schema_version=1, decision=decision, question=question, inputs=inputs,
                 request=record["request"], response=record["response"])
    if role == "judge" and harness == "claude":
        if native is None or native.model != judge_model("claude") or native.effort != "max":
            raise factory.FactoryError("Select the configured judge_model at maximum effort before freezing judge evidence")
        value.update(judge_model=judge_model("claude"), judge_harness="claude")
    elif role == "judge":
        import codex_capacity
        import native_codex
        if native is None or native.model != judge_model():
            raise factory.FactoryError("Select the configured judge_model before collecting native capability evidence")
        try:
            capacity = asyncio.run(codex_capacity.snapshot(Path(native.codex)))
        except (codex_capacity.CapacityError, native_codex.CodexProbeError, OSError, TimeoutError):
            raise factory.FactoryError("Judge native capability evidence is unavailable; refresh sign-in/catalog/capacity before judging") from None
        configured = judge_model()
        validate_judge_capacity(capacity, Path(native.codex), native.model, native.effort, configured, time.time())
        value.update(judge_capacity=capacity, judge_model=configured)
    return "\n" + report_guidance(value) + "\n" + MARKER + json.dumps(value, allow_nan=False, ensure_ascii=True) + "\n"


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
        if role == "judge":
            dispute(value["request"])
        return value
    except (ValueError, KeyError, jev.JevError):
        raise factory.FactoryError("Frozen coordination evidence changed; inspect the original brief, decision and question without relaunching") from None


def answer_schema(evidence):
    if owner(evidence["question"]) == "judge":
        return dict(type="object", additionalProperties=False,
            required=["decision", "question", "ruling", "consequences", "missing_input"], properties=dict(
                decision=dict(type="string", enum=[evidence["decision"]]),
                question=dict(type="string", enum=[evidence["question"]]),
                ruling=dict(type=["string", "null"], enum=[*dispute(evidence["request"])["options"], None]),
                consequences=dict(type="array", items=dict(type="string")), missing_input=dict(type=["string", "null"])))
    return dict(type="object", additionalProperties=False, required=["decision", "question", "choice", "missing_input"],
        properties=dict(decision=dict(type="string", enum=[evidence["decision"]]),
            question=dict(type="string", enum=[evidence["question"]]),
            choice=dict(type=["string", "null"], enum=[*evidence["request"]["payload"]["questions"][evidence["question"]]["criteria"], None]),
            missing_input=dict(type=["string", "null"])))


def validate_answer(answer, evidence, outcome):
    judge = owner(evidence["question"]) == "judge"
    fields = {"decision", "question", "ruling", "consequences", "missing_input"} if judge else {"decision", "question", "choice", "missing_input"}
    if (not isinstance(answer, dict) or set(answer) != fields
            or answer["decision"] != evidence["decision"] or answer["question"] != evidence["question"]
            or outcome not in {"completed", "blocked"}):
        raise factory.FactoryError("Reasoner answer differs from its bounded question; inspect its original event stream")
    if judge:
        ruling, consequences, missing = answer["ruling"], answer["consequences"], answer["missing_input"]
        if outcome == "completed":
            valid = (isinstance(ruling, str) and ruling in dispute(evidence["request"])["options"] and ruling != "uncertain"
                and isinstance(consequences, list) and bool(consequences)
                and all(isinstance(item, str) and item.strip() for item in consequences) and missing is None)
        else:
            valid = ruling is None and consequences == [] and isinstance(missing, str) and bool(missing.strip())
        if not valid:
            raise factory.FactoryError("Return a supported dispute ruling and consequences, or the concrete missing input; a classifier label is not adjudication")
        return
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
    import claude_worker
    import codex_worker
    attempts = {}
    try:
        try:
            paths = sorted(path for path in directory.iterdir() if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}\.json", path.name))
        except FileNotFoundError:
            # Only an absent directory has the owner's first-use meaning.
            try:
                directory.lstat()  # A dangling link is damaged, not first use.
            except FileNotFoundError:
                return attempts
            raise factory.FactoryError("Restore the original task directory before reading bounded answers")
        for path in paths:
            with factory.task_lock(directory, path.stem):
                record = factory.load_task(path)
            for identity, assignment in record["assignments"].items():
                command = assignment["request"]["command"]
                if (len(command) < 4 or command[1:3] != ["-E", "-s"]
                        or Path(command[3]).name not in ("codex_worker.py", "claude_worker.py") or "--decision" not in command):
                    continue
                harness = "claude" if Path(command[3]).name == "claude_worker.py" else "codex"
                parser = claude_worker.parser() if harness == "claude" else codex_worker.parser()
                parser.allow_abbrev = False
                def invalid(message=None):
                    raise factory.FactoryError("Restore the original bounded reasoner command before reading its answer")
                parser.error = invalid
                parser.exit = lambda status=0, message=None: invalid(message)
                if "-h" in command[4:] or "--help" in command[4:]:
                    invalid()
                args = parser.parse_args(command[4:])
                binding(args.decision, args.question, assignment["request"]["role"], write=args.write)
                item = dict(task=record["task"], assignment=identity, evidence=str(path), state="awaiting_collection", answer=None)
                if command[:4] != [sys.executable, "-E", "-s", str(factory.ROOT / (harness + "_worker.py"))]:
                    item["state"] = "engine_unavailable"
                elif assignment["request"].get("inputs") != record["inputs"]:
                    item["state"] = "stale"
                elif assignment["result"] is not None:
                    if harness == "claude":
                        _, receipt, native = factory.claude_evidence(identity, record, assignment, collected=True)
                    else:
                        _, receipt, native = factory.codex_evidence(identity, record, assignment)
                    item.update(state=native["report"]["outcome"], answer=native["report"]["answer"],
                                evidence=str(args.receipt), inputs=receipt["inputs"])
                attempts.setdefault((args.decision, args.question), []).append(item)
        return attempts
    except (OSError, ValueError) as exc:
        raise factory.FactoryError("Cannot read reasoner evidence; restore the original task, brief, receipt and event stream. " + str(exc)) from None
