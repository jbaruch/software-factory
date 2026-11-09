"""Mechanical repository integration. Original assignment intent precedes mutation."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from contextlib import contextmanager
import fcntl
import os

if __name__ == "__main__":
    sys.modules.setdefault("integration", sys.modules[__name__])

import factory
import release_evidence as evidence

PREFIX = [sys.executable, "-E", "-s", str(factory.ROOT / "integration.py"), "--intent"]


def mechanical(assignment):
    request = assignment.get("request", {})
    return request.get("role") == "integration" and request.get("command", [])[:5] == PREFIX


def intent_of(assignment):
    command = assignment["request"]["command"]
    if not mechanical(assignment) or len(command) != 6:
        raise factory.FactoryError("Not an original mechanical integration assignment")
    value = factory.decode_json(command[5])
    if (not isinstance(value, dict)
            or set(value) != {"schema_version", "task", "assignment", "state_dir", "admission", "inputs", "pr", "review", "required", "review_digest", "issues", "gh", "scripts", "commands", "approvals"}
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or any(not isinstance(value.get(key), str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value[key])
                   for key in ("task", "assignment"))
            or not isinstance(value.get("state_dir"), str) or not Path(value["state_dir"]).is_absolute()
            or value.get("inputs") != assignment["request"].get("inputs")
            or not factory.valid_inputs(value.get("inputs"))
            or not isinstance(value.get("admission"), dict)
            or not factory.valid_repository_identity(value["admission"].get("repository_identity"))
            or not isinstance(value.get("pr"), dict) or value["pr"].get("head") != value["inputs"]["candidate"]
            or not isinstance(value.get("required"), list) or not value["required"]
            or not isinstance(value.get("review"), str)
            or not isinstance(value.get("review_digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["review_digest"])
            or not isinstance(value.get("approvals"), dict) or not isinstance(value.get("commands"), dict)
            or any(not isinstance(value.get(key), str) or not Path(value[key]).is_absolute() for key in ("gh", "scripts"))):
        raise factory.FactoryError("Damaged original integration intent")
    return value


def collected_native(assignment_id, record, assignment, *, current=True):
    if assignment["result"] is None or (current and not factory.result_current(record, assignment["result"])):
        raise factory.FactoryError("Collect a current original native result before integration")
    try:
        rex = factory.connect(record)
        reader = factory.claude_evidence if assignment["request"]["command"][:4] == [sys.executable, "-E", "-s", str(factory.ROOT / "claude_worker.py")] else factory.codex_evidence
        return reader(assignment_id, record, assignment, collected=True, current=current,
            observe=lambda: factory.status(rex, record, assignment))
    except (OSError, ValueError) as exc:
        raise factory.FactoryError("Cannot validate original native integration evidence: " + str(exc)) from None


def contributor_context(key, record, assignment):
    # A failed writer remains a contributor; never require or invent a success.
    import claude_worker
    import codex_worker
    command = assignment["request"]["command"]
    claude = command[:4] == [sys.executable, "-E", "-s", str(factory.ROOT / "claude_worker.py")]
    try:
        if claude:
            args = claude_worker.parser().parse_args(command[4:])
        else:
            args = codex_worker.parser().parse_args(command[4:] if command[1:3] == ["-E", "-s"] else command[2:])
        receipt = factory.decode_json(factory.read_record(args.receipt, private=True))
    except (SystemExit, OSError, ValueError):
        raise factory.FactoryError("Missing original contributor execution metadata") from None
    if claude:
        # The native session is frozen in the original command before launch, so even
        # a failed writer keeps the identity its receipt and events must carry.
        if not isinstance(receipt, dict) or receipt.get("native_session") != args.native_session:
            raise factory.FactoryError("Claude contributor receipt differs from its frozen native session")
        if receipt.get("state") != "failed":
            collected_native(key, record, assignment, current=False)
        return args.native_session
    if isinstance(receipt, dict) and receipt.get("state") == "failed":
        rex = factory.connect(record)
        _, _, native = factory.codex_evidence(key, record, assignment, failed_identity=True,
            observe=lambda: factory.status(rex, record, assignment))
        return native["thread_id"]
    _, receipt, _ = collected_native(key, record, assignment, current=False)
    return receipt["thread_id"]


def approvals(record, review, required):
    """Re-read collected original native reports/events, never generic dependencies."""
    if (not required or len(set(required)) != len(required) or review in required
            or any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", item) for item in [review, *required])):
        raise factory.FactoryError("Explicitly select distinct required approval IDs and one release-review ID")
    contributors, selected = set(), {}
    for key, assignment in record["assignments"].items():
        if mechanical(assignment):
            continue
        command = assignment["request"]["command"]
        native = (command[:4] == [sys.executable, "-E", "-s", str(factory.ROOT / "codex_worker.py")]
                  or command[:2] == [sys.executable, str(factory.ROOT / "codex_worker.py")])
        claude = command[:4] == [sys.executable, "-E", "-s", str(factory.ROOT / "claude_worker.py")]
        if not native and not claude:
            raise factory.FactoryError("Cannot establish contributor independence for an unverified command adapter: " + key)
        if "--write" in command:
            contributors.add(contributor_context(key, record, assignment))
    contexts = set()
    for key in [*required, review]:
        assignment = record["assignments"].get(key)
        if assignment is None:
            raise factory.FactoryError("Unknown selected approval: " + key)
        args, receipt, native = collected_native(key, record, assignment)
        if (args.write or args.role != assignment["request"]["role"]
                or native["report"]["outcome"] != "approved"
                or (key == review and (args.role != "release" or not hasattr(args, "codex")))
                or (hasattr(args, "claude") and args.role not in {"qa", "security", "code-review"})):
            raise factory.FactoryError("Selected relevant native judgments must be read-only approved reports, including release")
        context = receipt.get("thread_id") if hasattr(args, "codex") else receipt.get("native_session")
        if not isinstance(context, str) or not context or context in contributors or context in contexts:
            raise factory.FactoryError("Selected approvals need distinct original native contexts independent of contributors")
        contexts.add(context)
        frozen = evidence.from_assignment(record, assignment) if key == review else None
        selected[key] = dict(context=context, receipt=evidence.digest(receipt),
                             result=evidence.digest(assignment["result"]), evidence=frozen)
    return selected


def command_binding(record, transport, *, cwd=None):
    checkouts = [item["request"]["cwd"] for item in record["assignments"].values()]
    if cwd is not None:
        checkouts.append(cwd)
    if hasattr(transport, "checkouts"):
        transport.checkouts = tuple(checkouts)
    binding = transport.binding()
    managed = Path(record["admission"]["repository"]) / ".codex" / "skills" / "acr__jbaruch__coding-policy__release"
    for filename in binding:
        path = Path(filename)
        if not path.is_absolute():
            continue  # Offline transports use symbolic command identities.
        if path.parent == managed:
            continue  # GitHub.binding validates this protected installed directory.
        if any(path.resolve().is_relative_to(Path(root).resolve()) for root in checkouts):
            raise factory.FactoryError("Installed release commands cannot come from an assignment checkout")
    return binding


def verify(record, intent, transport, *, watch=False, reload=None):
    if record["hold"] is not None:
        raise factory.FactoryError("Integration held: " + record["hold"])
    if command_binding(record, transport) != intent["commands"]:
        raise factory.FactoryError("Installed command/policy bytes changed; obtain a current integration intent")
    factory.check_repository(record["admission"])
    if record["admission"] != intent["admission"] or record["inputs"] != intent["inputs"]:
        raise factory.FactoryError("Integration inputs/admission changed; obtain current reassessment")
    selected = approvals(record, intent["review"], intent["required"])
    if {key: {k: v for k, v in item.items() if k != "evidence"} for key, item in selected.items()} != intent["approvals"]:
        raise factory.FactoryError("Original collected native approvals changed")
    current = evidence.capture(record, intent["pr"]["url"], transport)
    if current["pr"] != intent["pr"] or current["review_digest"] != intent["review_digest"] or current["issues"] != intent["issues"]:
        raise factory.FactoryError("Source/base/review changed; current specialist reassessment required")
    if watch:
        snapshot = transport.snapshot(intent["pr"], watch=True)
        watched = snapshot.get("watch") if isinstance(snapshot, dict) else None
        if not isinstance(watched, dict) or watched.get("result") != "ready":
            raise factory.FactoryError("Canonical readiness held: " + json.dumps(snapshot))
        feedback_digest = evidence.digest(evidence.feedback(snapshot, intent["pr"]))
        required_ci = [run for run in snapshot.get("ci", {}).get("actions", {}).get("runs", [])
                       if isinstance(run, dict) and run.get("path") == ".github/workflows/checks.yml"]
        if (len(required_ci) != 1 or required_ci[0].get("status") != "completed"
                or required_ci[0].get("conclusion") != "success"
                or any(type(required_ci[0].get(key)) is not int or required_ci[0][key] <= 0
                       for key in ("id", "workflow_id", "run_number", "run_attempt"))):
            raise factory.FactoryError("Required current-head checks.yml has no terminal successful current attempt")
        if feedback_digest != intent["review_digest"]:
            raise factory.FactoryError("Review changed during canonical watch; reassess all original bodies")
        # The canonical watcher owns CI and native review readiness. Re-read source
        # binding and actual repository merge state after waiting, immediately pre-effect.
        current = verify(reload() if reload else record, intent, transport)
        if any(current["snapshot"].get(key) != snapshot.get(key) for key in ("ci", "merge_state")):
            raise factory.FactoryError("CI or merge state changed after canonical readiness; recheck with a new intent")
        guard = evidence.server_guard(transport, intent["pr"], required_ci[0])
        rules = transport.requirements(intent["pr"])
        if (not isinstance(rules, dict) or "reviewDecision" not in rules
                or rules["reviewDecision"] not in {None, "", "APPROVED"}
                or rules.get("headRefOid") != intent["pr"]["head"] or rules.get("baseRefOid") != intent["pr"]["base"]
                or rules.get("mergeable") != "MERGEABLE" or rules.get("mergeStateStatus") != "CLEAN"):
            raise factory.FactoryError("Actual repository required review state is not approved/mergeable")
        pr = transport.api(f"repos/{intent['pr']['repository']}/pulls/{intent['pr']['number']}")
        if (evidence.pr_identity(pr, intent["pr"]["url"]) != intent["pr"]
                or pr.get("mergeable") is not True or pr.get("mergeable_state") != "clean"):
            raise factory.FactoryError("Actual repository review/merge requirements or source binding are not ready")
        current.update(watch=snapshot, actual_review=rules, server_guard=guard)
    return current


def endpoint(intent):
    return f"repos/{intent['pr']['repository']}"


def merged_source(intent, transport):
    """Read exact PR, native merge parents and target ancestry."""
    pr = transport.api(f"{endpoint(intent)}/pulls/{intent['pr']['number']}")
    observed = evidence.pr_identity(pr, intent["pr"]["url"])
    if any(observed[key] != value for key, value in intent["pr"].items() if key != "base"):
        raise factory.FactoryError("Reconciliation source/repository/target binding changed")
    if pr.get("merged") is not True or pr.get("state") != "closed":
        raise factory.FactoryError("merge_outcome_unknown: exact PR is not confirmed merged; never resend automatically")
    merge = pr.get("merge_commit_sha")
    if not isinstance(merge, str) or not re.fullmatch(evidence.SHA, merge):
        raise factory.FactoryError("merge_outcome_unknown: missing native merge commit")
    commit = transport.api(f"{endpoint(intent)}/commits/{merge}")
    if (not isinstance(commit, dict) or not isinstance(commit.get("parents"), list)
            or not all(isinstance(p, dict) for p in commit["parents"])
            or commit.get("sha") != merge or [p.get("sha") for p in commit["parents"]]
            != [intent["pr"]["base"], intent["pr"]["head"]]):
        raise factory.FactoryError("Merged commit does not confirm the frozen source/base parents")
    from urllib.parse import quote
    branch = transport.api(f"{endpoint(intent)}/git/ref/heads/{quote(intent['pr']['target'], safe='')}")
    target = branch.get("object", {}).get("sha") if isinstance(branch, dict) and isinstance(branch.get("object"), dict) else None
    if not isinstance(target, str) or not re.fullmatch(evidence.SHA, target):
        raise factory.FactoryError("Cannot confirm current target ref")
    comparison = transport.api(f"{endpoint(intent)}/compare/{merge}...{target}")
    if (not isinstance(comparison, dict) or comparison.get("status") not in ("ahead", "identical")
            or not isinstance(comparison.get("merge_base_commit"), dict) or comparison["merge_base_commit"].get("sha") != merge):
        raise factory.FactoryError("Merge commit is not confirmed in target ancestry")
    return merge, target


def postmerge(intent, transport) -> dict:
    try:
        return _postmerge(intent, transport)
    except factory.FactoryError as exc:
        raise factory.FactoryError(str(exc) + "; restore complete GitHub API/CI evidence, wait for or inspect the exact source-bound push run, and reconcile the same integration ID without resending the merge") from None


def _postmerge(intent, transport) -> dict:
    merge, target = merged_source(intent, transport)
    workflow = transport.api(f"{endpoint(intent)}/actions/workflows/checks.yml")
    if (not isinstance(workflow, dict) or type(workflow.get("id")) is not int or workflow["id"] <= 0
            or workflow.get("path") != ".github/workflows/checks.yml" or workflow.get("state") != "active"):
        raise factory.FactoryError("Missing actual checks.yml workflow identity")

    def resolve():
        pages = transport.pages(f"{endpoint(intent)}/actions/workflows/{workflow['id']}/runs?head_sha={merge}&event=push&per_page=100")
        runs = []
        for page in pages:
            if (not isinstance(page, dict) or not isinstance(page.get("workflow_runs"), list)
                    or type(page.get("total_count")) is not int):
                raise factory.FactoryError("Malformed push CI inventory")
            runs.extend(page["workflow_runs"])
        if any(page["total_count"] != len(runs) for page in pages) or len(runs) != 1:
            raise factory.FactoryError("Missing or ambiguous source-bound post-merge push CI")
        run = runs[0]
        check_run(run, workflow, intent, merge)
        return run

    run = resolve()
    transport.watch_run(intent["pr"]["repository"], run["id"])
    observed_run = transport.api(f"{endpoint(intent)}/actions/runs/{run['id']}")
    current = resolve()
    check_run(observed_run, workflow, intent, merge)
    if (current["id"] != run["id"] or current["run_attempt"] != observed_run["run_attempt"]
            or observed_run["run_attempt"] != run["run_attempt"]
            or any(item.get("status") != "completed" or item.get("conclusion") != "success"
                   for item in (observed_run, current))):
        raise factory.FactoryError("Post-merge CI missing terminal success for the current native attempt")
    issues = evidence.issue_evidence(transport.issues(intent["pr"], merged=True), intent["pr"], merged=True)
    confirmed_merge, target = merged_source(intent, transport)
    if confirmed_merge != merge:
        raise factory.FactoryError("Native merged source changed during post-merge confirmation")
    return dict(issues=issues, pr=intent["pr"]["url"], head=intent["pr"]["head"], merge_commit=merge, target=target,
                ci=observed_run, publication="inapplicable", acceptance="pending_native_judgment", cleanup_owner="release")


def check_run(run, workflow, intent, merge):
    if (not isinstance(run, dict) or type(run.get("id")) is not int or run["id"] <= 0
            or type(run.get("run_attempt")) is not int or run["run_attempt"] <= 0
            or type(run.get("run_number")) is not int or run["run_number"] <= 0
            or run.get("workflow_id") != workflow["id"] or run.get("path") != workflow["path"]
            or run.get("head_sha") != merge or run.get("event") != "push"
            or run.get("head_branch") != intent["pr"]["target"]
            or not isinstance(run.get("repository"), dict)
            or run["repository"].get("id") != intent["pr"]["repository_id"]
            or run.get("repository", {}).get("full_name") != intent["pr"]["repository"]):
        raise factory.FactoryError("Post-merge CI lacks exact repository/source/workflow/push/target/attempt binding")


GOAL = "Integrate the admitted exact candidate"


def render_intent(intent):
    return "\n## Factory mechanical integration intent\n\n" + json.dumps(intent, sort_keys=True) + "\n"


def validate_launch(record, request, task, assignment_id, path):
    intent = intent_of({"request": request})
    if (intent["task"] != task or intent["assignment"] != assignment_id
            or intent["state_dir"] != str(path.parent.resolve())
            or record["admission"] != intent["admission"] or record["inputs"] != intent["inputs"]
            or request.get("purpose") != "work" or request.get("goal") != GOAL
            or request.get("dependencies") != sorted([intent["review"], *intent["required"]])
            or intent["review"] not in record["assignments"]
            or request["cwd"] != record["assignments"][intent["review"]]["request"]["cwd"]):
        raise factory.FactoryError("Integration command differs from original task/assignment binding")
    return intent


def bound_assignment(path, record, intent):
    assignment = record["assignments"].get(intent["assignment"])
    if assignment is None or intent_of(assignment) != intent or record["task"] != intent["task"]:
        raise factory.FactoryError("Worker needs the existing exact original integration intent")
    # Selected inputs may change during recovery; immutable request/admission must not.
    original = {**record, "inputs": intent["inputs"]}
    validate_launch(original, assignment["request"], record["task"], intent["assignment"], path)
    try:
        instructions = Path(assignment["brief"]).read_text()
    except (OSError, UnicodeError):
        raise factory.FactoryError("Restore the original frozen integration brief") from None
    if (evidence.digest_text(instructions) != assignment["request"]["instructions_sha256"]
            or not instructions.endswith(render_intent(intent))):
        raise factory.FactoryError("Original integration brief differs from its immutable command")
    return assignment


def read_owner(path):
    with factory.task_lock(path.parent, path.stem, wait=True):
        return factory.load_task(path)


@contextmanager
def integration_lock(path, intent, *, wait=False):
    """One repository effect at a time; unrelated ready work keeps its capacity."""
    identity = intent["admission"]["repository_identity"]["repository_id"]
    lock = path.parent / (".integration-" + identity + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise factory.FactoryError("Another integration owns this repository; reconnect after it finishes") from None
        yield
    finally:
        os.close(fd)


def receipt_file(path, intent):
    return path.parent / (intent["task"] + "." + intent["assignment"] + ".integration.json")


def validate_receipt(receipt, intent):
    if (not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int
            or receipt["schema_version"] != 1 or receipt.get("kind") != "integration-execution"
            or receipt.get("intent") != intent
            or not isinstance(receipt.get("state"), str)
            or receipt.get("state") not in {"executing", "merge_outcome_unknown", "held", "rejected", "delivered"}
            or not isinstance(receipt.get("process"), dict)
            or any(not isinstance(receipt["process"].get(key), str) or not receipt["process"][key]
                   for key in ("session_id", "window_id", "block_id"))
            or not isinstance(receipt.get("observations"), list)
            or any(not isinstance(item, dict) for item in receipt["observations"])
            or (receipt["state"] in {"held", "rejected"} and not isinstance(receipt.get("reason"), str))
            or (receipt["state"] == "delivered" and (not isinstance(receipt.get("delivery"), dict)
                or receipt["delivery"].get("head") != intent["inputs"]["candidate"]
                or not isinstance(receipt["delivery"].get("ci"), dict)))):
        raise factory.FactoryError("Unsupported or corrupt original integration receipt; preserve it for inspection")
    return receipt


def integrate(path, *, assignment_id, pr, review, required, gh, scripts, transport=None) -> dict:
    """Launch/reconnect the original Rex worker; existing effects only reconcile."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", assignment_id):
        raise factory.FactoryError("Integration ID must be a lowercase slug")
    record = read_owner(path)
    if record["admission"] is None:
        raise factory.FactoryError("Admit the task before integration")
    transport = transport or evidence.GitHub(gh, scripts, record["admission"]["repository"])
    assignment = record["assignments"].get(assignment_id)
    fresh = assignment is None
    if fresh:
        if record["hold"] is not None:
            raise factory.FactoryError("Integration held: " + record["hold"])
        if any(mechanical(other) and other["result"] is None for other in record["assignments"].values()):
            raise factory.FactoryError("Reconcile the existing pending integration before a new effect")
        selected = approvals(record, review, required)
        frozen = selected[review]["evidence"]
        intent = dict(schema_version=1, task=record["task"], assignment=assignment_id,
            state_dir=str(path.parent.resolve()), admission=record["admission"], inputs=dict(record["inputs"]), pr=frozen["pr"],
            review=review, required=sorted(required), review_digest=frozen["review_digest"], issues=frozen["issues"],
            gh=str(gh), scripts=str(scripts), commands=command_binding(record, transport),
            approvals={key: {k: v for k, v in item.items() if k != "evidence"} for key, item in selected.items()})
        if pr != frozen["pr"]["url"]:
            raise factory.FactoryError("Integration PR differs from selected original release review")
        verify(record, intent, transport)
        command = [*PREFIX, json.dumps(intent, sort_keys=True)]
        cwd = record["assignments"][review]["request"]["cwd"]
    else:
        intent = intent_of(assignment)
        bound_assignment(path, record, intent)
        if (pr != intent["pr"]["url"] or review != intent["review"] or sorted(required) != intent["required"]
                or str(gh) != intent["gh"] or str(scripts) != intent["scripts"]):
            raise factory.FactoryError("Existing integration ID has different immutable inputs")
    # Ordinary launch lock covers only brief submission and Rex launch/reconnect.
    with factory.execution_lock(path.parent), factory.task_lock(path.parent, record["task"]):
        current = factory.load_task(path)
        if fresh:
            if current != record:
                raise factory.FactoryError("Task changed during integration preparation; reassess current inputs")
            if (path.parent / (record["task"] + "." + assignment_id + ".md")).exists() or receipt_file(path, intent).exists():
                raise factory.FactoryError("Original integration artifacts already exist; restore their assignment")
            record, assignment = factory.launch(path, task=record["task"], assignment_id=assignment_id,
                role="integration", goal=GOAL, cwd=cwd, command=command, purpose="work", dependencies=sorted([review, *required]))
        else:
            record = current
            assignment = bound_assignment(path, record, intent)
            rex = factory.connect(record)
            if assignment["state"] == "launching":
                factory.reconcile(rex, record, assignment)
                factory.save(path, record)
    if fresh:
        return dict(state="launched", assignment=assignment_id, session_id=record["session_id"],
                    window_id=assignment["window_id"], block_id=assignment["block_id"])
    process = factory.status(factory.connect(record), record, assignment)
    if process["state"] in {"running", "unknown"}:
        return dict(state="launched", assignment=assignment_id, process=process)
    return execute(path, intent, transport=transport, start=False)


def execute(path, intent, *, transport=None, start=False) -> dict:
    """Only the original pane may start once; every existing receipt is read-only recovery."""
    with integration_lock(path, intent, wait=start):
        record = read_owner(path)
        assignment = bound_assignment(path, record, intent)
        if assignment["state"] != "launched":
            raise factory.FactoryError("Reconcile the original Rex launch before execution")
        rex = factory.connect(record)
        process = factory.status(rex, record, assignment)
        transport = transport or evidence.GitHub(intent["gh"], intent["scripts"], record["admission"]["repository"])
        receipt_path = receipt_file(path, intent)
        fresh = not os.path.lexists(receipt_path)
        if (not fresh or not start) and command_binding(record, transport) != intent["commands"]:
            raise factory.FactoryError("Installed command/policy bytes changed; restore the original helpers before recovery")
        if assignment["result"] is not None and fresh:
            raise factory.FactoryError("Restore the original terminal integration receipt; never replace its evidence")
        if fresh:
            receipt: dict = dict(schema_version=1, kind="integration-execution", intent=intent,
                state="executing" if start else "merge_outcome_unknown", observations=[], process=process)
            if start and process["state"] != "running":
                raise factory.FactoryError("Initial integration execution requires its running original Rex pane")
            factory.save(receipt_path, receipt, exclusive=True)
        else:
            receipt = validate_receipt(factory.decode_json(factory.read_record(receipt_path, private=True)), intent)
            if any(receipt.get("process", {}).get(key) != process.get(key)
                   for key in ("session_id", "window_id", "block_id")):
                raise factory.FactoryError("Original integration receipt differs from its Rex pane")
            if receipt["state"] == "executing":
                # Submission is preceded by a durable merge_outcome_unknown save.
                # This earlier phase therefore proves no merge was submitted.
                receipt.update(state="held", reason="Interrupted before merge submission; reassess current inputs before a new integration ID")
                factory.save(receipt_path, receipt)
        if receipt["state"] in {"delivered", "rejected", "held"}:
            if assignment["result"] is None and receipt["state"] == "delivered":
                current = postmerge(intent, transport)
                if not evidence.same_delivery(current, receipt["delivery"], rerun=True):
                    raise factory.FactoryError("Crash-surviving delivery evidence is missing or changed")
            finish(path, intent, receipt_path, receipt)
            return receipt
        if fresh and start:
            try:
                receipt["gates"] = verify(record, intent, transport, watch=True, reload=lambda: read_owner(path))
                # No network waits under the task lock. Reload immediately before
                # recording the external attempt; never save an old owner snapshot.
                with factory.task_lock(path.parent, intent["task"], wait=True):
                    current = factory.load_task(path)
                    bound_assignment(path, current, intent)
                    selected = {intent["assignment"], intent["review"], *intent["required"]}
                    if (any(current[key] != record[key] for key in ("hold", "inputs", "admission"))
                            or any(current["assignments"][key] != record["assignments"][key] for key in selected)
                            or current["hold"] is not None):
                        raise factory.FactoryError("Task controls or inputs changed before integration effect")
                    if command_binding(current, transport) != intent["commands"]:
                        raise factory.FactoryError("Installed command/policy bytes changed before integration effect")
                    receipt["state"] = "merge_outcome_unknown"
                    factory.save(receipt_path, receipt)
            except (factory.FactoryError, OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
                receipt.update(state="held", reason=str(exc))
            else:
                try:
                    response = transport.merge(intent["pr"])
                    receipt["observations"].append(dict(merge_response=response))
                    if isinstance(response, dict) and response.get("merged") is False:
                        receipt.update(state="rejected", reason="GitHub rejected merge")
                except (factory.FactoryError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
                    receipt["observations"].append(dict(merge_error=str(exc)))
        if receipt["state"] == "merge_outcome_unknown":
            try:
                receipt.update(state="delivered", delivery=postmerge(intent, transport))
            except (factory.FactoryError, OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
                receipt["observations"].append(dict(pending=str(exc)))
        factory.save(receipt_path, receipt)
        finish(path, intent, receipt_path, receipt)
        return receipt


def finish(path, intent, receipt_path, receipt):
    if receipt["state"] in {"delivered", "rejected", "held"}:
        with factory.task_lock(path.parent, intent["task"], wait=True):
            record = factory.load_task(path)
            assignment = bound_assignment(path, record, intent)
            result = dict(inputs=intent["inputs"], candidate=intent["inputs"]["candidate"],
                outcome="completed" if receipt["state"] == "delivered" else "blocked",
                artifacts=[str(receipt_path), "sha256:" + evidence.digest(receipt)])
            if assignment["result"] is not None:
                if assignment["result"] != result:
                    raise factory.FactoryError("Original integration result content changed")
                return
            assignment["result"] = result
            factory.save(path, record)


def accepted(record, integration_id, acceptance_id, transport):
    """Generated view: confirmed delivery plus explicitly selected final native judgment."""
    original = evidence.delivery(record, integration_id)
    if record["hold"] is not None:
        raise factory.FactoryError("Acceptance held: " + record["hold"])
    assignment = record["assignments"].get(acceptance_id)
    if assignment is None:
        raise factory.FactoryError("Unknown selected final native acceptance judgment")
    args, receipt, native = collected_native(acceptance_id, record, assignment)
    if not hasattr(args, "codex") or args.role != "release" or args.write or native["report"]["outcome"] != "approved":
        raise factory.FactoryError("Final acceptance requires an approved current native release judgment")
    assessed = evidence.from_delivery_assignment(record, assignment)
    if assessed["assignment"] != original["assignment"]:
        raise factory.FactoryError("Final acceptance assessed different delivered evidence")
    intent = original["receipt"]["intent"]
    if command_binding(record, transport) != intent["commands"]:
        raise factory.FactoryError("Installed command/policy bytes changed before acceptance; restore the original helpers")
    selected = approvals(record, intent["review"], intent["required"])
    if {key: {k: v for k, v in item.items() if k != "evidence"} for key, item in selected.items()} != intent["approvals"]:
        raise factory.FactoryError("Original selected native approvals changed before final acceptance")
    for key, item in record["assignments"].items():
        if "--write" in item["request"]["command"]:
            if receipt["thread_id"] == contributor_context(key, record, item):
                raise factory.FactoryError("A contributor cannot judge final acceptance")
    current = postmerge(intent, transport)
    confirmed = assessed.get("observation", assessed["receipt"]["delivery"])
    if not evidence.same_delivery(current, confirmed) or current["issues"] != confirmed["issues"]:
        raise factory.FactoryError("Delivery evidence changed; obtain a current final native reassessment")
    current_owner = read_owner(Path(intent["state_dir"]) / (intent["task"] + ".json"))
    if current_owner != record:
        raise factory.FactoryError("Task changed during final acceptance observation; reload current controls/inputs")
    return dict(outcome="approved", assignment=acceptance_id, integration=integration_id,
                native_receipt=str(args.receipt), delivery_receipt_digest=original["receipt_digest"],
                assessed_delivery_digest=evidence.digest(assessed))


def main(argv=None):
    """Original pane entrypoint; a retained execution receipt permits only observation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intent", required=True)
    args = parser.parse_args(argv)
    try:
        intent = factory.decode_json(args.intent)
        directory = Path(intent["state_dir"])
        if not directory.is_absolute() or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", intent["task"]):
            raise factory.FactoryError("Reconciliation needs the original absolute state directory and task slug")
        path = directory / (intent["task"] + ".json")
        receipt = execute(path, intent, start=True)
        print(json.dumps(receipt, indent=2))
        return 0 if receipt["state"] == "delivered" else 1
    except (factory.FactoryError, OSError, ValueError, KeyError, TypeError) as exc:
        print("integration: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
