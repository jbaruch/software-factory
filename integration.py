"""Mechanical repository integration. Original assignment intent precedes mutation."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

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
            or set(value) != {"task", "assignment", "state_dir", "admission", "inputs", "pr", "review", "required", "review_digest", "issues", "gh", "scripts", "commands", "approvals"}
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
        return factory.codex_evidence(assignment_id, record, assignment, collected=True, current=current,
            observe=lambda: factory.status(rex, record, assignment))
    except (OSError, ValueError) as exc:
        raise factory.FactoryError("Cannot validate original native integration evidence: " + str(exc)) from None


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
        native = command[:4] == [sys.executable, "-E", "-s", str(factory.ROOT / "codex_worker.py")]
        if not native:
            raise factory.FactoryError("Cannot establish contributor independence for an unverified command adapter: " + key)
        if "--write" in command:
            args, receipt, _ = collected_native(key, record, assignment, current=False)
            if not isinstance(receipt.get("thread_id"), str) or not receipt["thread_id"]:
                raise factory.FactoryError("Missing native contributor context")
            contributors.add(receipt["thread_id"])
    contexts = set()
    for key in [*required, review]:
        assignment = record["assignments"].get(key)
        if assignment is None:
            raise factory.FactoryError("Unknown selected approval: " + key)
        args, receipt, native = collected_native(key, record, assignment)
        if (args.write or args.role != assignment["request"]["role"]
                or native["report"]["outcome"] != "approved"
                or (key == review and args.role != "release")):
            raise factory.FactoryError("Selected relevant native judgments must be read-only approved reports, including release")
        context = receipt.get("thread_id")
        if not isinstance(context, str) or not context or context in contributors or context in contexts:
            raise factory.FactoryError("Selected approvals need distinct original native contexts independent of contributors")
        contexts.add(context)
        frozen = evidence.from_assignment(record, assignment)
        selected[key] = dict(context=context, receipt=evidence.digest(receipt),
                             result=evidence.digest(assignment["result"]), evidence=frozen)
    release = selected[review]["evidence"]
    if any(item["evidence"]["pr"] != release["pr"] or item["evidence"]["review_digest"] != release["review_digest"] or item["evidence"]["issues"] != release["issues"]
           for item in selected.values()):
        raise factory.FactoryError("Selected approvals need reassessment of the same source/base/review inputs")
    return selected


def verify(record, intent, transport, *, watch=False):
    if record["hold"] is not None:
        raise factory.FactoryError("Integration held: " + record["hold"])
    if transport.binding() != intent["commands"]:
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
        current = verify(record, intent, transport)
        if any(current["snapshot"].get(key) != snapshot.get(key) for key in ("ci", "merge_state")):
            raise factory.FactoryError("CI or merge state changed after canonical readiness; recheck with a new intent")
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
        current.update(watch=snapshot, actual_review=rules)
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


def integrate(path, *, assignment_id, pr, review, required, gh, scripts, transport=None):
    """Caller holds execution + task locks. Replays observe only; no merge retry."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", assignment_id):
        raise factory.FactoryError("Integration ID must be a lowercase slug")
    record = factory.load_task(path)
    if record["admission"] is None:
        raise factory.FactoryError("Admit the task before integration")
    transport = transport or evidence.GitHub(gh, scripts, record["admission"]["repository"])
    assignment = record["assignments"].get(assignment_id)
    fresh = assignment is None
    if fresh:
        if record["hold"] is not None:
            raise factory.FactoryError("Integration held: " + record["hold"])
        for other in record["assignments"].values():
            if mechanical(other) and other["result"] is None:
                raise factory.FactoryError("Reconcile the existing pending integration before a new effect")
        selected = approvals(record, review, required)
        frozen = selected[review]["evidence"]
        intent: dict = dict(task=record["task"], assignment=assignment_id, state_dir=str(path.parent.resolve()), admission=record["admission"], inputs=dict(record["inputs"]), pr=frozen["pr"],
            review=review, required=sorted(required), review_digest=frozen["review_digest"], issues=frozen["issues"], gh=str(gh), scripts=str(scripts), commands=transport.binding(),
            approvals={key: {k: v for k, v in item.items() if k != "evidence"} for key, item in selected.items()})
        if pr != frozen["pr"]["url"]:
            raise factory.FactoryError("Integration PR differs from selected original release review")
        verify(record, intent, transport)
        instructions = factory.brief("integration", "Execute only the frozen mechanical release intent") + json.dumps(intent, sort_keys=True) + "\n"
        brief = path.parent / (record["task"] + "." + assignment_id + ".md")
        receipt_path = path.parent / (record["task"] + "." + assignment_id + ".integration.json")
        if brief.exists() or receipt_path.exists():
            raise factory.FactoryError("Original integration artifacts already exist; restore/reconcile their assignment")
        factory.save(brief, instructions, exclusive=True)
        assignment: dict = dict(request=dict(role="integration", purpose="work", goal="Integrate the admitted exact candidate",
            cwd=record["admission"]["repository"], inputs=dict(record["inputs"]), dependencies=sorted([review, *required]),
            command=[*PREFIX, json.dumps(intent, sort_keys=True)], instructions_sha256=evidence.digest_text(instructions)),
            state="launching", result=None, brief=str(brief), window_label="integration-" + assignment_id + "-" + uuid.uuid4().hex[:8])
        record["assignments"][assignment_id] = assignment
        factory.save(path, record)  # Before any external mutation; a crash here is read-only reconciliation.
    else:
        intent = intent_of(assignment)
        if (pr != intent["pr"]["url"] or review != intent["review"] or sorted(required) != intent["required"]
                or str(gh) != intent["gh"] or str(scripts) != intent["scripts"]):
            raise factory.FactoryError("Existing integration ID has different immutable inputs")
    try:
        instructions = Path(assignment["brief"]).read_text()
    except (OSError, UnicodeError):
        raise factory.FactoryError("Restore the original frozen integration brief") from None
    if (evidence.digest_text(instructions) != assignment["request"]["instructions_sha256"]
            or not instructions.endswith(json.dumps(intent, sort_keys=True) + "\n")):
        raise factory.FactoryError("Original integration brief differs from its immutable command")
    receipt_path = path.parent / (record["task"] + "." + assignment_id + ".integration.json")
    if receipt_path.exists():
        receipt: dict = factory.decode_json(factory.read_record(receipt_path, private=True))
        if not isinstance(receipt, dict) or receipt.get("intent") != intent:
            raise factory.FactoryError("Original integration result differs from its intent")
        if receipt.get("state") in {"delivered", "rejected", "held"}:
            # Collect a crash-surviving success only after fresh read-only confirmation.
            if assignment["result"] is None and receipt["state"] == "delivered":
                current = postmerge(intent, transport)
                confirmed = receipt.get("delivery")
                if (not isinstance(confirmed, dict) or not isinstance(confirmed.get("ci"), dict)
                        or current["merge_commit"] != confirmed.get("merge_commit")
                        or any(current["ci"][key] != confirmed["ci"].get(key) for key in ("id", "run_attempt"))):
                    raise factory.FactoryError("Crash-surviving delivery evidence is missing or changed")
            finish(path, record, assignment, intent, receipt_path, receipt)
            return receipt
        if receipt.get("state") != "merge_outcome_unknown" or not isinstance(receipt.get("observations"), list):
            raise factory.FactoryError("Malformed original pending integration result")
    else:
        if assignment["result"] is not None:
            raise factory.FactoryError("Restore the original terminal integration receipt; never replace its evidence")
        receipt = dict(intent=intent, state="merge_outcome_unknown", observations=[])
    if fresh:
        try:
            receipt["gates"] = verify(record, intent, transport, watch=True)
        except (factory.FactoryError, OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
            receipt.update(state="held", reason=str(exc))
        else:
            factory.save(receipt_path, receipt)  # Unknown outcome is durable before sending.
            try:
                response = transport.merge(intent["pr"])
                receipt["observations"].append(dict(merge_response=response))
                if isinstance(response, dict) and response.get("merged") is False:
                    receipt.update(state="rejected", reason="GitHub rejected merge")
            except (factory.FactoryError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
                receipt["observations"].append(dict(merge_error=str(exc)))
    if receipt["state"] == "merge_outcome_unknown":
        try:
            delivered = postmerge(intent, transport)
            receipt.update(state="delivered", delivery=delivered)
        except (factory.FactoryError, OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as exc:
            receipt["observations"].append(dict(pending=str(exc)))
    factory.save(receipt_path, receipt)
    finish(path, record, assignment, intent, receipt_path, receipt)
    return receipt


def finish(path, record, assignment, intent, receipt_path, receipt):
    if receipt["state"] in {"delivered", "rejected", "held"}:
        assignment["state"] = "launched"
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
    if args.role != "release" or args.write or native["report"]["outcome"] != "approved":
        raise factory.FactoryError("Final acceptance requires an approved current native release judgment")
    if evidence.from_delivery_assignment(record, assignment) != original:
        raise factory.FactoryError("Final acceptance assessed different delivered evidence")
    intent = original["receipt"]["intent"]
    selected = approvals(record, intent["review"], intent["required"])
    if {key: {k: v for k, v in item.items() if k != "evidence"} for key, item in selected.items()} != intent["approvals"]:
        raise factory.FactoryError("Original selected native approvals changed before final acceptance")
    for key, item in record["assignments"].items():
        if "--write" in item["request"]["command"]:
            _, contributor, _ = collected_native(key, record, item, current=False)
            if receipt["thread_id"] == contributor["thread_id"]:
                raise factory.FactoryError("A contributor cannot judge final acceptance")
    current = postmerge(intent, transport)
    confirmed = original["receipt"]["delivery"]
    if (current["merge_commit"] != confirmed["merge_commit"] or current["issues"] != confirmed["issues"]
            or any(current["ci"][key] != confirmed["ci"][key] for key in ("id", "run_attempt"))):
        raise factory.FactoryError("Delivery evidence changed; obtain a current final native reassessment")
    return dict(outcome="approved", assignment=acceptance_id, integration=integration_id,
                native_receipt=str(args.receipt), delivery_receipt_digest=original["receipt_digest"])


def main(argv=None):
    """The persisted command can reconcile its existing effect, never send anew."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intent", required=True)
    args = parser.parse_args(argv)
    try:
        intent = factory.decode_json(args.intent)
        directory = Path(intent["state_dir"])
        if not directory.is_absolute() or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", intent["task"]):
            raise factory.FactoryError("Reconciliation needs the original absolute state directory and task slug")
        with factory.execution_lock(directory), factory.task_lock(directory, intent["task"]) as path:
            record = factory.load_task(path)
            assignment = record["assignments"].get(intent["assignment"])
            if assignment is None or intent_of(assignment) != intent:
                raise factory.FactoryError("Reconciliation needs the existing exact original integration intent")
            receipt = integrate(path, assignment_id=intent["assignment"], pr=intent["pr"]["url"],
                review=intent["review"], required=intent["required"], gh=intent["gh"], scripts=intent["scripts"])
            print(json.dumps(receipt, indent=2))
            return 0 if receipt["state"] == "delivered" else 1
    except (factory.FactoryError, OSError, ValueError, KeyError, TypeError) as exc:
        print("integration: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
