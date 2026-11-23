"""Delivery confirmation: the repository's release skill merges; the factory confirms what merged."""
import os
from pathlib import Path
import re

import factory
import release_evidence as evidence


def collected_native(assignment_id, record, assignment, *, current=True, captured=None):
    label = record["task"] + "/" + assignment_id
    command = assignment["request"]["command"]
    result = assignment["result"]
    if result is None:
        original = factory.native_harness(command)
        action = (f"use {original}-collect if the original worker exited successfully and its checkout is available" if original else
                  "preserve the attempt and ask the task owner to inspect its original collection evidence")
        raise factory.FactoryError(f"{label}: missing native result; {action} before delivery")
    if current and not factory.result_current(record, result):
        changed = [key for key, value in (("candidate", result["candidate"]), ("requirements", result["inputs"]["requirements"]))
                   if record["inputs"][key] != value]
        raise factory.FactoryError(f"{label}: stale result ({' and '.join(changed)} changed); "
                                   "obtain and select a fresh native judgment for the current inputs")
    try:
        harness = factory.worker_hint(command)
        if harness is None:
            raise factory.FactoryError("Cannot establish contributor independence for an unverified command adapter; "
                                       "ask the task owner to inspect the original worker command")
        rex = factory.connect(record)
        args, receipt, native = factory.evidence_reader(harness)(assignment_id, record, assignment,
            collected=True, delivery=True, current=current, captured=captured,
            observe=lambda: factory.status(rex, record, assignment))
        return harness, args, receipt, native
    except (factory.FactoryError, ValueError) as exc:
        raise factory.FactoryError(f"{label}: {exc}") from None
    except OSError:
        raise factory.FactoryError(f"{label}: inspect access to the original native brief, receipt and events before delivery") from None


def native_context(harness, receipt):
    """The independent native identity a receipt carries."""
    key = dict(codex="thread_id", antigravity="conversation_id").get(harness, "native_session")
    return receipt.get(key) if isinstance(receipt, dict) else None


# A Claude or Grok writer's starting receipt, field for field, as its worker writes it before the native run.
STARTING_FIELDS = {
    "claude": {"schema_version", "kind", "state", "job", "inputs", "repository_identity", "worktree_git_dir", "cwd",
               "brief_sha256", "role", "binary", "requested_model", "requested_effort", "discovery", "native_session",
               "seconds", "max_turns", "created_at", "finished_at", "elapsed_seconds", "exit_code", "report", "usage",
               "model_usage", "turns", "rate_limits", "candidate", "events", "error", "writer", "sandbox", "commit"},
    "grok": {"schema_version", "kind", "state", "job", "inputs", "repository_identity", "worktree_git_dir", "cwd",
             "brief_sha256", "role", "binary", "requested_model", "requested_effort", "discovery", "configuration",
             "native_session", "seconds", "max_turns", "created_at", "finished_at", "elapsed_seconds", "exit_code",
             "report", "request_id", "usage", "model_usage", "turns", "candidate", "events", "error", "writer",
             "sandbox", "commit"}}
UNFINISHED_FIELDS = ("finished_at", "elapsed_seconds", "exit_code", "report", "usage", "model_usage", "turns",
                     "candidate", "error", "commit")


def unfinished_writer(harness, receipt, args, assignment, record):
    """A Claude or Grok writer receipt exactly as its worker wrote it at start: bound to this assignment, never finished."""
    request = assignment["request"]
    late = UNFINISHED_FIELDS + (("rate_limits",) if harness == "claude" else ("request_id",))
    return (isinstance(receipt, dict) and set(receipt) == STARTING_FIELDS[harness]
            and receipt["kind"] == harness + "-execution" and receipt["schema_version"] == (2 if harness == "claude" else 3)
            and receipt["state"] == "starting" and receipt["writer"] is True
            and receipt["job"] == args.job and receipt["inputs"] == request.get("inputs")
            and receipt["brief_sha256"] == request.get("instructions_sha256")
            and receipt["repository_identity"] == record["admission"]["repository_identity"]
            and receipt["role"] == args.role and receipt["cwd"] == request.get("cwd")
            and receipt["worktree_git_dir"] == str(args.worktree_git_dir)
            and receipt["requested_model"] == args.model and receipt["requested_effort"] == args.effort
            and receipt["seconds"] == args.seconds and receipt["max_turns"] == args.turns
            and receipt["sandbox"] == str(args.sandbox) and receipt["events"] == str(args.events)
            and isinstance(receipt["binary"], str) and isinstance(receipt["discovery"], dict)
            and (harness == "claude" or isinstance(receipt["configuration"], dict))
            and type(receipt["created_at"]) in (int, float)
            and all(receipt[field] is None for field in late))


def contributor_context(key, record, assignment):
    # A failed writer remains a contributor; never require or invent a success.
    import importlib
    command = assignment["request"]["command"]
    harness = factory.native_harness(command)
    if assignment.get("native_collected") is True:
        proven, _, receipt, _ = collected_native(key, record, assignment, current=False)
        return native_context(proven, receipt)
    label = record["task"] + "/" + key
    if harness is None:
        raise factory.FactoryError(f"{label}: cannot verify the historical writer's original contributor identity; "
                                   "preserve the recorded attempt and ask the task owner to inspect its original evidence")
    worker = importlib.import_module(harness + "_worker")
    try:
        args = factory.parse_saved_args(worker.parser(), command[4:] if command[1:3] == ["-E", "-s"] else command[2:])
    except factory.FactoryError as exc:
        raise factory.FactoryError(f"{label}: {exc}") from None
    try:
        receipt = factory.decode_json(factory.read_record(args.receipt, private=True))
    except (OSError, ValueError):
        raise factory.FactoryError(f"{label}: restore the missing or damaged original writer receipt") from None
    if harness == "antigravity" and isinstance(receipt, dict) and receipt.get("state") == "failed":
        return antigravity_contributor(label, assignment, args, receipt)
    if harness in ("claude", "grok"):
        # The native session is frozen in the original command before launch, so even
        # a failed writer keeps the identity its receipt and events must carry.
        if not isinstance(receipt, dict) or receipt.get("native_session") != args.native_session:
            raise factory.FactoryError(f"{label}: contributor receipt differs from its frozen native session; inspect the original writer receipt")
        if unfinished_writer(harness, receipt, args, assignment, record) and assignment["result"] is None and \
                factory.status(factory.connect(record), record, assignment)["state"] == "lost":
            # A writer lost with its host never finished its receipt or produced a result a reviewer could
            # share; like a failed writer, its frozen session is its identity (#156).
            return args.native_session
        if receipt.get("state") == "failed":
            return args.native_session
    elif isinstance(receipt, dict) and receipt.get("state") == "failed":
        rex = factory.connect(record)
        try:
            _, _, native = factory.codex_evidence(key, record, assignment, failed_identity=True,
                observe=lambda: factory.status(rex, record, assignment))
        except (factory.FactoryError, ValueError) as exc:
            raise factory.FactoryError(f"{label}: {exc}") from None
        return native["thread_id"]
    raise factory.FactoryError(f"{label}: inspect the original writer receipt and collect its completed result before delivery")


def antigravity_contributor(label, assignment, args, receipt):
    """Antigravity names its conversation at init, so a writer that failed before init has none."""
    import antigravity_worker
    import native_antigravity
    request = assignment["request"]
    brief = Path(assignment["brief"])
    if (type(receipt.get("schema_version")) is not int or receipt["schema_version"] not in (2, 4)
            or receipt.get("kind") != "antigravity-execution" or receipt.get("writer") is not True
            or not isinstance(receipt.get("error"), str)
            or receipt.get("job") != label or args.job != label or receipt.get("events") != str(args.events)
            or args.events != brief.with_suffix(".antigravity.jsonl") or args.receipt != brief.with_suffix(".antigravity.json")
            or receipt.get("brief_sha256") != request["instructions_sha256"] or receipt.get("inputs") != request["inputs"]
            or "conversation_id" not in receipt):
        raise factory.FactoryError(f"{label}: restore the original failed Antigravity writer receipt")
    if receipt["schema_version"] == 2 and "events_digest" not in receipt and receipt["conversation_id"] is None:
        raise factory.FactoryError(f"{label}: cannot verify the original Antigravity conversation or a pre-init failure; "
                                   "preserve the attempt for the task owner")
    try:
        lines = antigravity_worker.read_events(args.events)
        if receipt["schema_version"] == 4 or "events_digest" in receipt:
            if evidence.digest(lines) != receipt.get("events_digest"):
                raise native_antigravity.ExecutionError("stream differs from the failed writer receipt")
            if not lines and receipt["conversation_id"] is None:
                return "antigravity-unstarted:" + label
        context = native_antigravity.parse_execution(lines, expected_model=args.model, expected_cwd=request["cwd"],
            expected_schema=antigravity_worker.outcome_schema(), failed_identity=True)["conversation_id"]
        if receipt["conversation_id"] not in (None, context):
            raise native_antigravity.ExecutionError("init differs from the failed writer receipt")
        return context
    except native_antigravity.ExecutionError:
        raise factory.FactoryError(f"{label}: cannot verify the failed writer's contributor identity; "
                                   "inspect or restore the original Antigravity event stream and receipt") from None


def _all_contributors(record):
    """Combined local --write contributor contexts plus any origin contributors; fails closed on archive changes."""
    import writer_transfer
    contributors = set()
    for key, assignment in record["assignments"].items():
        command = assignment["request"]["command"]
        if "--write" in command:
            contributors.add(contributor_context(key, record, assignment))
    if record.get("schema_version") == 12 or "writer_origin" in record:
        origin = writer_transfer.verified_origin(record)
        contributors.update(c["context"] for c in origin["contributors"])
    return contributors


def approvals(record, review, required):
    """Re-read collected original native reports/events, never generic dependencies."""
    if (not required or len(set(required)) != len(required) or review in required
            or any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", item) for item in [review, *required])):
        raise factory.FactoryError("Explicitly select distinct required approval IDs and one release-review ID")
    contributors = _all_contributors(record)
    for key, assignment in record["assignments"].items():
        command = assignment["request"]["command"]
        if "--write" not in command and factory.native_harness(command) is None and key not in required and key != review:
            # An omitted off-runtime reader still needs proof of what ran.
            collected_native(key, record, assignment, current=False)
    selected = {}
    contexts = set()
    for key in [*required, review]:
        assignment = record["assignments"].get(key)
        if assignment is None:
            raise factory.FactoryError("Unknown selected approval: " + key)
        harness, args, receipt, native = collected_native(key, record, assignment)
        if (args.write or args.role != assignment["request"]["role"]
                or native["report"]["outcome"] != "approved"
                or (key == review and args.role != "release")
                or (not hasattr(args, "codex") and args.role not in factory.VERIFIER_ROLES | {"release"})):
            raise factory.FactoryError(f"Selected approval {key} (role {args.role}, outcome {native['report']['outcome']}) must be a "
                "read-only approved report from " + ", ".join(sorted(factory.VERIFIER_ROLES | {"release"}))
                + ", and the review a release judgment")
        context = native_context(harness, receipt)
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
    managed = evidence.managed_installs(record["admission"]["repository"])
    for filename in binding:
        path = Path(filename)
        if not path.is_absolute():
            continue  # Offline transports use symbolic command identities.
        if path.parent in managed:
            continue  # GitHub.binding validates these protected installed directories.
        if any(path.resolve().is_relative_to(Path(root).resolve()) for root in checkouts):
            raise factory.FactoryError("Installed release commands cannot come from an assignment checkout")
    return binding


def target_contains(pr, merge, transport):
    """The current target branch has the merge commit in its history; returns the branch's head."""
    from urllib.parse import quote
    root = "repos/" + pr["repository"]
    branch = transport.api(f"{root}/git/ref/heads/{quote(pr['target'], safe='')}")
    target = branch.get("object", {}).get("sha") if isinstance(branch, dict) and isinstance(branch.get("object"), dict) else None
    if not isinstance(target, str) or not re.fullmatch(evidence.SHA, target):
        raise factory.FactoryError("Cannot confirm current target ref")
    comparison = transport.api(f"{root}/compare/{merge}...{target}")
    if (not isinstance(comparison, dict) or comparison.get("status") not in ("ahead", "identical")
            or not isinstance(comparison.get("merge_base_commit"), dict) or comparison["merge_base_commit"].get("sha") != merge):
        raise factory.FactoryError("Merge commit is not confirmed in target ancestry")
    return target


def read_owner(path):
    with factory.task_lock(path.parent, path.stem, wait=True):
        return factory.load_task(path)


def delivered(path, *, pr, review, required, transport) -> dict:
    """The repository's release skill merges; confirm it merged what the release judgment approved (#77)."""
    record = read_owner(path)
    if record["admission"] is None:
        raise factory.FactoryError("Admit the task before confirming its delivery")
    if record["hold"] is not None:
        raise factory.FactoryError("Delivery held: " + record["hold"])
    selected = approvals(record, review, required)
    approved = selected[review]["evidence"]["pr"]
    if pr != approved["url"]:
        raise factory.FactoryError("The PR differs from the one the release judgment approved")
    observed = transport.api(f"repos/{approved['repository']}/pulls/{approved['number']}")
    identity = evidence.pr_identity(observed, pr)
    if observed.get("merged") is not True or observed.get("state") != "closed":
        raise factory.FactoryError("The PR is not merged yet: merge it through the repository's release skill, then confirm again")
    if any(identity[key] != approved[key] for key in ("repository_id", "head_repository_id", "head_ref", "target")):
        raise factory.FactoryError("The merged PR's source or target differs from the one the release judgment approved")
    if identity["head"] != approved["head"]:
        raise factory.FactoryError(f"The release skill merged head {identity['head']}, but the release judgment approved "
            f"{approved['head']}: the merged change was never judged. Report it to the operator")
    merge = observed.get("merge_commit_sha")
    if not isinstance(merge, str) or not re.fullmatch(evidence.SHA, merge):
        raise factory.FactoryError("GitHub reports no merge commit for the merged PR")
    target_contains(approved, merge, transport)
    receipt = dict(schema_version=1, kind="release-skill-delivery", task=record["task"], inputs=record["inputs"],
        pr=pr, head=approved["head"], merge_commit=merge, review=review, required=sorted(required),
        approvals={key: {k: v for k, v in item.items() if k != "evidence"} for key, item in selected.items()})
    receipt_path = path.parent / (record["task"] + "." + review + ".delivered.json")
    with factory.task_lock(path.parent, record["task"], wait=True):
        if factory.load_task(path) != record:
            raise factory.FactoryError(f"{record['task']}/{review}: task changed during delivery observation; reload current controls/inputs")
        if os.path.lexists(receipt_path):
            if factory.decode_json(factory.read_record(receipt_path, private=True)) != receipt:
                raise factory.FactoryError("A different delivery is already recorded for this release judgment: " + str(receipt_path))
        else:
            factory.save(receipt_path, receipt, exclusive=True)
    return dict(receipt, receipt_path=str(receipt_path))


def delivered_accepted(path, review, acceptance_id) -> dict:
    """Generated view: the recorded `delivered` receipt plus the final native judgment that assessed it (#151)."""
    record = read_owner(path)
    if record["hold"] is not None:
        raise factory.FactoryError("Acceptance held: " + record["hold"])
    original = evidence.delivered_receipt(record, review)
    assignment = record["assignments"].get(acceptance_id)
    if assignment is None:
        raise factory.FactoryError("Unknown selected final native acceptance judgment")
    harness, args, receipt, native = collected_native(acceptance_id, record, assignment)
    if args.role != "release" or args.write or native["report"]["outcome"] != "approved":
        raise factory.FactoryError("Final acceptance requires an approved current native release judgment")
    if evidence.from_delivery_assignment(record, assignment) != original:
        raise factory.FactoryError("Final acceptance assessed a different delivery; judge the recorded `delivered` receipt")
    judge = native_context(harness, receipt)
    if judge in _all_contributors(record):
        raise factory.FactoryError("A contributor cannot judge final acceptance")
    if read_owner(path) != record:
        raise factory.FactoryError("Task changed during final acceptance observation; reload current controls/inputs")
    return dict(outcome="approved", assignment=acceptance_id, review=review, native_receipt=str(args.receipt),
                delivery_receipt_digest=original["receipt_digest"])
