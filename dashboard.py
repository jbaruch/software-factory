"""Local operator view generated from factory-owned records and optional Rex observations."""
import argparse
import base64
import binascii
from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import factory
import jev
import coordination_evidence

ASSETS = Path(__file__).resolve().parent / "web"
SLUG = r"[a-z0-9][a-z0-9-]{0,63}"
PANE_LIMIT = 64 * 1024


def invalid_worker(message=None):
    raise ValueError("Unknown saved worker envelope")


def application_worker(path):
    """Bind an exact worker file to this application or its registered Git worktrees."""
    worker = Path(path)
    try:
        info = worker.lstat()
        if (not worker.is_absolute() or str(worker.resolve(strict=True)) != path
                or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            return False
        root = worker.parent
        if root == factory.ROOT:
            return True
        # Inventory comes from the trusted application, never the task's repository
        # label or the caller's path. No repository markers are created/read here.
        inventory = factory.git_run(factory.ROOT, "worktree", "list", "--porcelain", "-z")
        if inventory.returncode:
            return False
        registered = any(
            fields[0] == "worktree " + str(root)
            and not any(field == "bare" or field.startswith("prunable") for field in fields)
            for entry in inventory.stdout.split("\0\0") if entry
            for fields in [entry.split("\0")])
        return (registered and factory.git_worktree(root) == str(root)
                and factory.git_common_dir(root) == factory.git_common_dir(factory.ROOT))
    except (factory.FactoryError, OSError, ValueError, RuntimeError):
        return False


def requested_native(record, identity, assignment):
    """Decode only owner-supported worker envelopes, without executing a worker/probe."""
    unknown = dict(adapter=None, harness=None, route=None, model=None, effort=None)
    request = assignment["request"]
    inputs = request.get("inputs")
    admission = record["admission"]
    if not inputs or not admission:
        return unknown
    # The owner stores the original template, expanding only whole arguments at launch.
    replacements = {"{brief}": assignment["brief"], "{brief_sha256}": request["instructions_sha256"],
                    "{cwd}": request["cwd"], "{repository}": admission["repository"],
                    "{candidate}": inputs["candidate"], "{requirements}": inputs["requirements"],
                    "{repository_identity}": json.dumps(admission.get("repository_identity"), sort_keys=True)}
    command = [replacements.get(part, part) for part in request["command"]]
    import codex_worker
    import grok_worker
    import claude_worker
    if command[:3] == [sys.executable, "-E", "-s"] and len(command) > 3:
        offset = 4
    elif command[:1] == [sys.executable] and len(command) > 1:
        offset = 2
    else:
        return unknown
    worker = Path(command[offset - 1])
    if worker.name == "codex_worker.py":
        parser, harness = codex_worker.parser(), "codex"
    elif worker.name == "grok_worker.py" and offset == 4:
        parser, harness = grok_worker.parser(), "grok"
    elif worker.name == "claude_worker.py" and offset == 4:
        parser, harness = claude_worker.parser(), "claude"
    else:
        return unknown
    if not application_worker(command[offset - 1]):
        return unknown
    # argparse's abbreviations and repeated options are not factory-generated envelopes.
    parser.allow_abbrev = False
    parser.error = invalid_worker
    parser.exit = lambda status=0, message=None: invalid_worker(message)
    options = [part.split("=", 1)[0] for part in command[offset:] if part.startswith("--")]
    if "-h" in command[offset:] or "--help" in options or len(options) != len(set(options)):
        return unknown
    try:
        # Parser diagnostics may contain saved argv; the overrides above never print them.
        args = parser.parse_args(command[offset:])
    except (SystemExit, ValueError):
        return unknown
    try:
        repository_identity = factory.decode_json(args.repo_identity)
        if harness == "codex":
            factory.check_commit_message(args.commit_message, write=args.write)
        elif str(uuid.UUID(args.native_session)) != args.native_session or not 1 <= args.turns <= 1000:
            return unknown
    except (ValueError, factory.FactoryError):
        return unknown
    if (repository_identity != admission.get("repository_identity")
            or not factory.valid_repository_identity(repository_identity)
            or args.job != record["task"] + "/" + identity
            or str(args.cwd) != request["cwd"] or str(args.repo) != admission["repository"]
            or str(args.brief) != assignment["brief"] or args.brief_sha256 != request["instructions_sha256"]
            or args.candidate != inputs["candidate"] or args.requirements != inputs["requirements"]
            or (args.role is not None and args.role != request["role"])
            or not args.model.strip() or not args.effort.strip()
            or not 1 <= args.seconds <= factory.MAX_NATIVE_SECONDS
            or any(not path.is_absolute() for path in (getattr(args, harness), args.worktree_git_dir,
                                                       args.events, args.receipt))):
        return unknown
    return dict(adapter=harness + "_worker", harness=harness, route=harness + "-native",
                model=args.model, effort=args.effort)


def saved_pane(record, assignment):
    # A tidied tab lived in the session it records, which the task may since have replaced.
    session_id = assignment["closed"]["session_id"] if assignment.get("closed") else record["session_id"]
    return dict(host_id=(record["rex"] or {}).get("host_id"), session_id=session_id,
                window_id=assignment.get("window_id"), block_id=assignment.get("block_id"))


def pane_link(task, identity):
    if re.fullmatch(SLUG, task) and re.fullmatch(SLUG, identity):
        return f"/tasks/{task}/assignments/{identity}/pane"
    return None


def observe_process(rex, record, assignment, clock):
    process: dict = factory.status(rex, record, assignment)
    # Do not forward native process objects, argv, or extra last_exit metadata.
    observed: dict = dict(state=process["state"], observed_at=clock(),
                          exit_code=process["exit"]["exit_code"] if process["exit"] else None)
    if process["state"] == "lost":
        observed["error"] = process["reason"]
    if process.get("closed"):
        observed["closed"] = True  # Retained final observation of a tidied tab.
    return observed


def pane_detail(state_dir, task, identity, *, connect=factory.connect, clock=time.time) -> dict:
    """A fresh read of one saved pane; no recovery or Rex mutation is permitted."""
    detail = dict(task=task, assignment=identity, pane=None, process=dict(state="unavailable", observed_at=None),
                  output=None, partial=False, captured_at=None,
                  error="Pane unavailable. Restore the original task/assignment record, then refresh.")
    if pane_link(task, identity) is None:
        return detail
    try:
        with factory.task_lock(state_dir, task) as path:
            record = factory.load_task(path)
            assignment = record["assignments"].get(identity)
            if assignment is None:
                return detail
            detail["pane"] = saved_pane(record, assignment)
            if assignment["state"] == "abandoned":
                detail["error"] = "No pane: the launch was abandoned. " + assignment["abandoned"]["reason"]
                return detail
            if assignment["state"] != "launched":
                detail["error"] = "Pane unavailable: launch needs reconciliation. Use factory status for this task, then refresh."
                return detail
            closed = assignment.get("closed")
            if closed is not None:
                # A tidied tab needs no Rex: show its retained final observation and output.
                detail["process"] = observe_process(None, record, assignment, clock)
                if closed["capture"] is None:
                    detail["error"] = "Tab closed without retained output: " + (closed["reason"] or closed["state"])
                    return detail
                output = factory.read_record(Path(closed["capture"]), private=True)
                detail.update(output=output[-PANE_LIMIT:], partial=len(output) > PANE_LIMIT,
                              captured_at=closed["at"], error=None)
                return detail
        # Release the lock before transport waits; this loaded scope stays fixed
        # through every ownership check/read and is never saved back to the task.
        # connect validates host and session marker; reads stay scoped to the saved block.
        rex = connect(record)
        process = observe_process(rex, record, assignment, clock)
        detail["process"] = process
        if process["state"] == "lost":
            # A closed pane has no output, and a restored pane shows only a new shell.
            detail["error"] = "Pane output unavailable: " + process["error"]
            return detail
        output = rex.call("capture", "-s", record["session_id"], "-b", assignment["block_id"],
                          "--trim", "--unwrap", raw=True)
        if not isinstance(output, str):
            raise factory.FactoryError("Invalid capture")
        detail.update(output=output[-PANE_LIMIT:], partial=len(output) > PANE_LIMIT,
                      captured_at=clock(), error=None)
    except (factory.FactoryError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        detail["error"] = "Pane unavailable. Inspect the pinned Rex host/session and original pane identity; restore access or use factory status, then refresh."
    return detail


def inbox_source_identity(directory, *, tasks=False):
    """Transient change detector, not an owner reader or a saved snapshot."""
    try:
        paths = sorted(path for path in directory.iterdir() if path.suffix == ".json"
                       and (not tasks or re.fullmatch(SLUG, path.stem)))
    except FileNotFoundError:
        try:
            directory.lstat()
        except FileNotFoundError:
            return None  # Only absent first use is empty; dangling links refuse.
        raise OSError("Unavailable source directory") from None
    info = directory.stat()
    files = []
    for path in paths:
        # Match the owner's regular-file boundary before reading any bytes.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise OSError("Source must be a regular file")
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(64 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if identity(before) != identity(after) or identity(after) != identity(path.lstat()):
            raise OSError("Source changed during collection")
        files.append((path.name, identity(after), digest.hexdigest()))
    return (info.st_dev, info.st_ino, files)


def decision_inbox(state_dir, decision_store, tasks):
    """Allowlisted owner views; no state-supplied paths, routing or effects."""
    inbox: dict = dict(status="not_configured", error=None, items=[], resolutions=[])
    if decision_store is None:
        return inbox
    try:
        sources_before = (inbox_source_identity(decision_store), inbox_source_identity(state_dir, tasks=True))
        original = jev.pending_decisions(decision_store)
    except (jev.JevError, OSError, ValueError):
        inbox.update(status="unavailable", error="Decision or task evidence unavailable. Restore the selected private Jev store and original task records; refresh does not retry requests.")
        return inbox
    inbox["status"] = "available"
    try:
        current = jev.pending_decisions(decision_store, task_state_dir=state_dir)
    except (jev.JevError, factory.FactoryError, OSError, ValueError):
        current = None
        inbox.update(status="unavailable", error="Native answer evidence unavailable. Restore original task/brief/receipt/events before deciding recovery.")
    pending = {(item["decision"], item["question"]): item for item in current["items"]} if current else {}
    resolved = {(item["decision"], item["question"]): item for item in current["resolutions"]} if current else {}
    task_ids = {task["id"] for task in tasks}
    try:
        for proposal in original["items"]:
            identity, question = proposal["decision"], proposal["question"]
            # IDs originate in the validating owner, never in arbitrary state paths.
            record = jev.read_decision(decision_store / (identity + ".json"))
            request = record["request"]
            key = (identity, question)
            active = pending.get(key)
            resolution = resolved.get(key)
            item: dict = dict(decision=identity, question=question,
                question_version=request["question_versions"].get(question),
                proposal_owner=proposal["owner"], proposal_reason=proposal["reason"],
                owner=active["owner"] if active else proposal["owner"],
                condition=active["reason"] if active else proposal["reason"],
                evidence=proposal["evidence"], evidence_revision=proposal["evidence_revision"],
                evidence_freshness="unknown", task=None, options={}, dispute_question=None,
                evaluation_questions=({name: dict(version=request["question_versions"][name], options=spec["criteria"])
                                       for name, spec in request["payload"]["questions"].items()} if question is None else {}),
                attempts=[], recorded_answer=None, availability="Planning only; no dispatch control")
            if question is not None:
                item["options"] = request["payload"]["questions"][question]["criteria"]
            sources = active.get("attempts", []) if active else ([resolution] if resolution else [])
            # Only explicit native bindings or the investigator owner's binding name a task.
            bindings = {source["task"] for source in sources}
            state = request["payload"]["state"]
            investigation = state.get("investigation") if isinstance(state, dict) else None
            if proposal["owner"] == "judge" and isinstance(investigation, dict):
                bound = investigation.get("task")
                if isinstance(bound, str) and re.fullmatch(SLUG, bound):
                    bindings.add(bound)
            if len(bindings) == 1 and next(iter(bindings)) in task_ids:
                item["task"] = next(iter(bindings))
            for source in sources:
                answer = source.get("answer")
                item["attempts"].append(dict(task=source["task"], assignment=source["assignment"],
                    state=source["state"], evidence=source["evidence"],
                    answer=({field: answer[field] for field in ("choice", "ruling", "consequences", "missing_input") if field in answer}
                            if isinstance(answer, dict) else None)))
            if resolution:
                item.update(condition="recorded_current_answer", recorded_answer=item["attempts"][0]["answer"],
                            evidence_freshness="current native task inputs; original Jev evidence freshness remains unverified")
            if current is None:
                item.update(owner="operator", condition="native_evidence_unavailable")
            if proposal["owner"] == "judge":
                try:
                    case = coordination_evidence.dispute(request)
                    item.update(dispute_question=case["question"], options=case["options"])
                    if item["task"] is None:
                        raise factory.FactoryError("Unbound investigation")
                    with factory.task_lock(state_dir, item["task"]) as path:
                        task_record = factory.load_task(path)
                        coordination_evidence.check_investigation(request, task_record)
                    item["availability"] = "Investigation binding verified; native eligibility and authority still require owner checks"
                except (factory.FactoryError, OSError, ValueError):
                    item.update(owner="operator", condition="investigation_unavailable_or_stale",
                                availability="Blocked: requires the exact current completed investigation and original dispute")
                    # A collected answer remains visible, but cannot clear a newly invalid investigation.
                    resolution = None
            if item["task"] is None:
                item["availability"] += "; Unbound: operator must establish task/scope; no task or allowance is inferred"
                if not resolution:
                    item["owner"] = "operator"
            elif next(task for task in tasks if task["id"] == item["task"])["hold"] is not None:
                item["availability"] += "; Held: operator hold remains binding; no dispatch control"
            item["decision_needed"] = (
                "Inspect the recorded answer and original evidence; acceptance remains separate" if resolution else
                "Inspect retained request/service status and decide recovery; no automatic retry" if question is None else
                "Supply missing evidence, binding or authority and decide the next bounded assignment" if item["owner"] == "operator" else
                "Answer the original bounded question using only its supported options, or identify missing input")
            inbox["resolutions" if resolution else "items"].append(item)
    except (jev.JevError, factory.FactoryError, OSError, ValueError):
        # A source changing/failing during collection is never an all-resolved inbox.
        inbox.update(status="unavailable", items=[], resolutions=[],
                     error="Original decision evidence changed or became unavailable. Restore the selected store and refresh.")
    try:
        sources_after = (inbox_source_identity(decision_store), inbox_source_identity(state_dir, tasks=True))
        if sources_before != sources_after:
            raise OSError("Source changed during collection")
    except (OSError, ValueError):
        # Partial/mixed results cannot establish current answers or available work.
        inbox.update(status="unavailable", items=[], resolutions=[],
                     error="Decision or task evidence changed or became unavailable during collection. Refresh to read the original sources again; no current answers are established by this view.")
    return inbox


def task_conditions(tasks, errors):
    items = []
    for task in tasks:
        common = dict(task=task["id"], evidence=task["id"] + ".json", attempts=task["allowances"])
        if task["hold"] is not None:
            items.append(dict(**common, owner="operator", condition="operator_hold", detail=task["hold"],
                              decision_needed="Decide whether to retain or explicitly resume the hold"))
        if task["allowances"] is None:
            items.append(dict(**common, owner="operator", condition="allowances_unknown", detail="Task not admitted",
                              decision_needed="Establish authorized scope/admission before assigning work"))
        else:
            current = [assignment for assignment in task["assignments"] if assignment["result_current"]]
            failures = [assignment for assignment in current
                        if assignment.get("purpose") != "investigation"
                        and assignment["result"]["outcome"] in {"blocked", "findings"}]
            corrections = [assignment for assignment in current if assignment.get("purpose") == "correction"]
            investigations = [assignment for assignment in task["assignments"]
                              if assignment.get("purpose") == "investigation"]
            for purpose, values in task["allowances"].items():
                if values["remaining"] <= 0:
                    item: dict = dict(**common, owner="operator",
                        condition=purpose + ("_exhausted" if values["used"] else "_not_authorized"),
                        detail=purpose + (" allowance exhausted" if values["used"] else " has no authorized attempts; no attempts made"),
                        availability="Recorded limit reached; an explicit extension is required before another " + purpose + " assignment",
                        decision_needed="Review progress and standing authorization for an explicit " + purpose + " extension, or decide stop/wait")
                    if purpose == "correction" and corrections and failures:
                        item["artifacts"] = [ref for assignment in failures for ref in assignment["result"]["artifacts"]]
                        item["detail"] += "; attempted correction work and current blocked/findings evidence remain"
                        blocked = any(a["result_current"] and a["result"]["outcome"] in {"blocked", "findings"}
                                      for a in investigations)
                        completed = any(a["result_current"] and a["result"]["outcome"] == "completed"
                                        for a in investigations)
                        pending = any(a["inputs_current"] and a["result"] is None for a in investigations)
                        item["artifacts"] += [ref for a in investigations if a["result_current"]
                                              for ref in a["result"]["artifacts"]]
                        if blocked:
                            item.update(availability="Blocked: original current investigation requires operator recovery",
                                        decision_needed="Inspect the original investigation report and supply its missing input or decide stop/wait")
                            if task["allowances"]["investigation"]["remaining"] <= 0:
                                item["availability"] += "; no investigation allowance remains"
                                item["decision_needed"] += "; an authorized owner must record an explicit investigation extension before further work"
                        elif completed:
                            item.update(availability="Current completed investigation evidence exists; no repeat investigation proposed",
                                        decision_needed="Use the original completed investigation for bounded reassessment; do not repeat investigation or infer a ruling")
                        elif pending:
                            item.update(availability="Investigation already assigned; no duplicate assignment proposed",
                                        decision_needed="Wait for or reconcile the original investigation assignment")
                        elif task["allowances"]["investigation"]["remaining"] <= 0:
                            item.update(availability="Blocked: no investigation allowance remains",
                                        decision_needed="Review progress and standing authorization for an explicit investigation extension, or decide stop/wait")
                        else:
                            item.update(owner="investigator", availability="Investigation allowance remains; operator must select any assignment",
                                        decision_needed="Gather evidence for the failed correction loop using the original current reports; no automatic assignment")
                    if task["hold"] is not None:
                        item["availability"] += "; Held: operator hold remains binding"
                    items.append(item)
        for assignment in task["assignments"]:
            report = assignment["result"]
            if assignment["result_current"] and report["outcome"] in {"blocked", "findings"}:
                items.append(dict(**common, owner="operator", condition="current_" + report["outcome"],
                    assignment=assignment["id"], artifacts=report["artifacts"],
                    detail=assignment["role"] + " reported " + report["outcome"],
                    decision_needed="Inspect original report/artifacts and plan the responsible specialist; recipient/options are not inferred"))
    for error in errors:
        items.append(dict(owner="operator", task=error["task"], condition="task_unavailable", detail=error["error"],
                          evidence=error["task"] + ".json", attempts=None,
                          decision_needed="Restore the original task record/access before planning"))
    return items


def snapshot(state_dir, *, decision_store=None, live=False, connect=factory.connect, clock=time.time, timer=time.monotonic) -> dict:
    started = timer()
    task_sources_available, task_sources_before = True, None
    if decision_store is not None:
        try:
            task_sources_before = inbox_source_identity(state_dir, tasks=True)
        except (OSError, ValueError):
            task_sources_available = False
    try:
        paths = sorted(path for path in state_dir.iterdir() if path.suffix == ".json")
    except FileNotFoundError:
        paths = []
    except NotADirectoryError:
        raise factory.FactoryError("State path is not a directory; supply the factory's --state-dir") from None
    except OSError:
        raise factory.FactoryError("Cannot list task records; restore access to the factory state directory") from None
    tasks, errors = [], []
    for path in paths:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", path.stem):
            continue  # Receipts and schemas are evidence, not task records.
        try:
            with factory.task_lock(state_dir, path.stem):
                record = factory.load_task(path)
                view = factory.task_view(record)
            rex, observation_error = None, None
            if live and record["rex"] is not None:
                try:
                    rex = connect(record)
                except (factory.FactoryError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
                    observation_error = "Inspect the pinned Rex host/session and restore access before refreshing"
            assignments = []
            for identity, assignment in view["assignments"].items():
                process = dict(state="not_checked", observed_at=None)
                if live:
                    if assignment.get("closed") is not None:
                        process = observe_process(None, record, assignment, clock)
                    elif observation_error:
                        process = dict(state="unavailable", observed_at=None, error=observation_error)
                    elif assignment["state"] == "launching":
                        process = dict(state="unreconciled", observed_at=None, error="Use factory status to reconcile the existing launch")
                    elif rex is not None:
                        try:
                            process = observe_process(rex, record, assignment, clock)
                        except (factory.FactoryError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
                            process = dict(state="unavailable", observed_at=None, error="Inspect the original Rex pane and restore access before refreshing")
                assignments.append(dict(id=identity, role=assignment["request"]["role"],
                    purpose=assignment["request"].get("purpose"),
                    goal=assignment["request"]["goal"], launch_state=assignment["state"],
                    inputs=assignment["request"].get("inputs"), process=process,
                    inputs_current=(assignment["request"].get("inputs") == record["inputs"]
                                    if assignment["request"].get("inputs") else None),
                    requested_native=requested_native(record, identity, assignment), observed_model=None,
                    pane=saved_pane(record, assignment),
                    pane_href=(pane_link(record["task"], identity) if assignment["state"] == "launched" else None),
                    result=assignment["result"], result_current=assignment["result_current"]))
            tasks.append(dict(id=record["task"], goal=(record["admission"] or {}).get("goal"),
                              hold=record["hold"], inputs=record["inputs"],
                              control_events=record["control_events"],
                              allowance_events=record["allowance_events"],
                              admission=({key: record["admission"][key] for key in
                                          ("goal", "initial_candidate", "acceptance", "limits")} if record["admission"] else None),
                              provisioning=view["provisioning"],
                              allowances=view["allowances"], assignments=assignments))
        except (factory.FactoryError, OSError) as exc:
            errors.append(dict(task=path.stem, error=str(exc)))
    catalog = json.loads((factory.ROOT / "config/providers.json").read_text())
    if (not isinstance(catalog, dict) or type(catalog.get("schema_version")) is not int
            or catalog["schema_version"] != 1 or not isinstance(catalog.get("routes"), list)):
        raise factory.FactoryError("Unsupported provider catalog; update the dashboard before reading this format")
    if any(not isinstance(route, dict) or "quota_observation" not in route or any(
            not isinstance(route.get(key), str) or not route[key].strip()
            for key in ("id", "harness", "readiness")) for route in catalog["routes"]):
        raise factory.FactoryError("Incomplete provider catalog; restore its route identities and observations")
    inbox = decision_inbox(state_dir, decision_store, tasks)
    if decision_store is not None:
        try:
            task_sources_available = (task_sources_available
                                      and task_sources_before == inbox_source_identity(state_dir, tasks=True))
        except (OSError, ValueError):
            task_sources_available = False
        if not task_sources_available:
            inbox.update(status="unavailable", items=[], resolutions=[],
                         error="Task evidence changed or became unavailable during collection. Refresh before deciding; no current native answers are established by this view.")
    inbox["conditions"] = task_conditions(tasks, errors)
    return dict(schema_version=4, observed_at=clock(), collection_seconds=timer() - started,
                live=live, tasks=tasks, errors=errors, routes=catalog["routes"], decision_inbox=inbox)


def text(value):
    return escape(str(value).encode("utf-8", errors="backslashreplace").decode("utf-8"), quote=True)


def when(value):
    return (datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            if value is not None else "Unknown — no process observation in this view")


def pane_identity(pane):
    return '<dl class="pane-identity">' + "".join(
        f'<dt>{label}</dt><dd><code>{text((pane or {}).get(key) or "Unknown")}</code></dd>'
        for key, label in (("host_id", "Host"), ("session_id", "Session"),
                           ("window_id", "Window"), ("block_id", "Block"))) + '</dl>'


def render_pane(detail):
    link = pane_link(detail["task"], detail["assignment"])
    process = detail["process"]
    output = ''
    if detail["output"] is not None:
        marker = (f'Partial view: last {PANE_LIMIT:,} characters of this capture; earlier captured text omitted.'
                  if detail["partial"] else 'All text returned by this capture is shown.')
        output = (f'<h2>Original terminal output</h2><p>{text(marker)}</p>'
                  '<p class="muted">Rex retains bounded scrollback; this is a terminal view, not a complete execution log.</p>'
                  f'<p>Captured at <time>{text(when(detail["captured_at"]))}</time></p>'
                  f'<pre class="terminal-output">{text(detail["output"])}</pre>')
    error = f'<p class="errors">{text(detail["error"])}</p>' if detail["error"] else ''
    refresh = f'<a class="button secondary" href="{text(link)}">Refresh pane observation</a>' if link else ''
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Owned Rex pane · Software Factory</title><link rel="stylesheet" href="/dashboard.css"></head>'
            '<body><header><div class="brand">Software Factory</div></header><main>'
            '<nav aria-label="Dashboard"><a href="/">Team</a></nav><h1>Owned Rex pane</h1>'
            f'<p>Task <code>{text(detail["task"])}</code> / Assignment <code>{text(detail["assignment"])}</code></p>'
            f'{refresh}<h2>Saved Rex identity</h2>{pane_identity(detail["pane"])}'
            f'<h2>Process observation</h2><p>{text(process["state"].capitalize())}</p>'
            f'<p>Last actual process observation: <time>{text(when(process.get("observed_at")))}</time></p>'
            f'{error}{output}<footer>Read-only observation. Refresh reads again; reports and acceptance stay separate.</footer>'
            '</main></body></html>')


def allowance_text(allowances):
    if allowances is None:
        return "Attempts unknown — no admitted task binding"
    return "; ".join(f'{purpose}: {values["used"]} used, {values["remaining"]} remaining of {values["limit"]}'
                     for purpose, values in allowances.items())


def allowance_controls(task, csrf_token):
    history = ''.join(
        f'<li>{text(event["purpose"])}: +{text(event["additional"])} from {text(event["previous_limit"])}. '
        f'{text(event["reason"])} <time>{text(when(event["at"]))}</time><small>Request {text(event["id"])}</small></li>'
        for event in task.get("allowance_events", []))
    result = ('<details class="control-history"><summary>Original allowance decisions</summary><ol>'
              + history + '</ol></details>' if history else '')
    admission = task.get("admission")
    if admission:
        result += ('<details class="control-history"><summary>Original admission</summary>'
                   f'<p>Candidate <code>{text(admission["initial_candidate"])}</code></p>'
                   f'<p>Original limits: {text(admission["limits"])}</p><ul>'
                   + ''.join(f'<li><code>{text(ref)}</code></li>' for ref in admission["acceptance"]) + '</ul></details>')
    if csrf_token is None or task["allowances"] is None:
        return result
    result += ('<p class="muted">Default counts are progress-review checkpoints. The recorded limit remains enforced '
               'until an explicit extension. The foreman can continue within existing standing authorization '
               'without asking the operator again; user budgets, scope and holds remain binding. '
               'Workers and classifiers cannot grant themselves attempts.</p>')
    identity = task["id"]
    for purpose, values in task["allowances"].items():
        field = text(f'approval-{identity}-{purpose}')
        result += (f'<details class="task-controls"><summary>Approve one additional {text(purpose)} attempt</summary>'
                   f'<p>{text(allowance_text({purpose: values}))}. This adds exactly one attempt; it starts no work and preserves holds.</p>'
                   f'<form method="post" action="/tasks/{text(identity)}/allowance">'
                   f'<input type="hidden" name="csrf" value="{text(csrf_token)}">'
                   f'<input type="hidden" name="request_id" value="{secrets.token_hex(16)}">'
                   f'<input type="hidden" name="purpose" value="{text(purpose)}">'
                   f'<input type="hidden" name="expected_limit" value="{values["limit"]}">'
                   f'<label for="{field}">Operator approval reason (required, at most 512 characters)</label>'
                   f'<input id="{field}" name="reason" maxlength="512" required>'
                   f'<button>Approve +1 {text(purpose)} attempt</button></form></details>')
    return result


def render_inbox(view):
    inbox: dict = view.get("decision_inbox") or dict(status="not_configured", items=[], resolutions=[], conditions=[])
    status = {"not_configured": "Unknown — Jev decision store not configured",
              "available": "Selected Jev store read; proposals remain shadow judgments",
              "unavailable": "Decision evidence unavailable — this is not an empty or resolved inbox"}[inbox["status"]]
    queues = []
    tasks = {task["id"]: task for task in view["tasks"]}
    for owner in ("operator", "standby-reasoner", "investigator", "judge"):
        cards = []
        for item in inbox.get("conditions", []):
            if item["owner"] != owner:
                continue
            refs = ''.join(f'<li><code>{text(ref)}</code></li>' for ref in item.get("artifacts", []))
            cards.append(f'<article class="decision"><h4>{text(item["condition"])}</h4>'
                f'<p><a href="#task-{text(item["task"])}">Task {text(item["task"])}</a>: {text(item["detail"])}</p>'
                f'<p>{text(allowance_text(item["attempts"]))}</p><p>Source <code>{text(item["evidence"])}</code>'
                f' {text(item.get("assignment", ""))}</p><ul>{refs}</ul>'
                f'<p>{text(item.get("availability", "Planning only; no dispatch control"))}</p>'
                f'<p><strong>Decision needed:</strong> {text(item["decision_needed"])}</p>'
                '<p>Supported controls are in task detail. No semantic recipient or ruling is inferred.</p></article>')
        for item in inbox["items"]:
            if item["owner"] == owner:
                cards.append(render_decision(item, tasks))
        queues.append(f'<section class="decision-queue" aria-labelledby="queue-{owner}"><h3 id="queue-{owner}">'
                      f'{text(owner.capitalize())} proposals ({len(cards)})</h3>'
                      + (''.join(cards) or '<p>No known proposals in this queue. Source availability is shown above.</p>') + '</section>')
    resolutions = ''.join(render_decision(item, tasks) for item in inbox["resolutions"])
    return ('<section class="inbox" id="decisions"><h2>Decision inbox</h2>'
            '<p>Proposals, available human controls and accepted outcomes stay separate. No automatic dispatch or budget grants.</p>'
            f'<p class="source-status">{text(status)}</p><p>{text(inbox.get("error") or "")}</p>'
            + ''.join(queues) + '<details><summary>Recorded current native answers (' + str(len(inbox["resolutions"]))
            + ')</summary><p>These answers close matching owner questions only; they do not accept tasks or release work.</p>'
            + resolutions + '</details><p><a href="/api/state">View inbox source facts</a></p></section>')


def render_decision(item, tasks):
    bound = tasks.get(item["task"])
    task = (f'<a href="#task-{text(item["task"])}">Task {text(item["task"])}</a>' if bound else 'Task binding unknown')
    options = ''.join(f'<li><code>{text(key)}</code>: {text(value)}</li>' for key, value in item["options"].items())
    evaluation = ''.join(
        f'<details><summary>Original question {text(name)} / version {text(spec["version"])}</summary><ul>'
        + ''.join(f'<li><code>{text(key)}</code>: {text(value)}</li>' for key, value in spec["options"].items())
        + '</ul></details>' for name, spec in item["evaluation_questions"].items())
    answers = []
    for attempt in item["attempts"]:
        answer = attempt["answer"]
        detail = ''.join(f'<dt>{text(key)}</dt><dd>{text(value)}</dd>' for key, value in (answer or {}).items())
        answers.append(f'<li>{text(attempt["task"])}/{text(attempt["assignment"])} — {text(attempt["state"])}'
                       f'<p>Original evidence <code>{text(attempt["evidence"])}</code></p><dl>{detail}</dl></li>')
    return (f'<article class="decision"><h4>{text(item["question"] or "Retained evaluation")}</h4><p>{task}</p>'
            f'<p><strong>Original shadow proposal:</strong> {text(item["proposal_owner"])} / {text(item["proposal_reason"])}</p>'
            f'<p><strong>Current condition:</strong> {text(item["condition"])}</p><p>{text(item["availability"])}</p>'
            f'<p>{text(allowance_text(bound["allowances"] if bound else None))}</p>'
            f'<p>Original decision <code>{text(item["decision"])}</code>; source <code>{text(item["evidence"])}</code></p>'
            f'<p>Evidence revision <code>{text(item["evidence_revision"])}</code>; freshness: {text(item["evidence_freshness"])}</p>'
            f'<p>Original question ID/version: {text(item["question"])} / {text(item["question_version"])}</p>'
            f'<p>{text(item["dispute_question"] or "Question identified by its original ID; instruction contents are private.")}</p>'
            f'<details><summary>Original supported options</summary><ul>{options}</ul></details>'
            f'{evaluation}'
            f'<p><strong>Decision needed:</strong> {text(item["decision_needed"])}</p>'
            '<details><summary>Recorded native attempts/answers</summary><ul>' + ''.join(answers) + '</ul></details></article>')


def render(view, *, csrf_token=None):
    tasks = []
    for task in view["tasks"]:
        rows = []
        for assignment in task["assignments"]:
            process = assignment["process"]
            state = process["state"]
            label = {"not_checked": "Not checked", "unreconciled": "Needs reconciliation",
                     "exited": "Exited successfully"}.get(state, state.capitalize())
            if process.get("closed"):
                label = f"{label} (tab closed)"
            observed = f'<span class="process {text(state)}">{text(label)}</span>'
            if process.get("error"):
                observed += f'<p class="detail">{text(process["error"])}</p>'
            observed += f'<small>Last actual process observation: <time>{text(when(process.get("observed_at")))}</time></small>'
            native = assignment.get("requested_native") or {}
            requested = '<dl>' + ''.join(f'<dt>{label}</dt><dd>{text(native.get(key) or "Unknown")}</dd>'
                for key, label in (("adapter", "Native adapter"), ("harness", "Native harness"),
                                   ("route", "Requested subscription route"), ("model", "Requested model"),
                                   ("effort", "Requested effort"))) + '</dl><small>Observed model: Unknown</small>'
            requested += '<small>Requested identity does not verify runtime readiness or subscription eligibility.</small>'
            pane = pane_identity(assignment.get("pane"))
            link = pane_link(task["id"], assignment["id"])
            if (assignment.get("launch_state") == "launched" and link
                    and all((assignment.get("pane") or {}).get(key) for key in ("host_id", "session_id", "window_id", "block_id"))):
                pane += f'<a href="{text(link)}">Read owned Rex pane</a>'
            else:
                pane += '<small>Pane unavailable. Use factory status to inspect/reconcile the saved identity.</small>'
            source = assignment.get("inputs")
            freshness = {True: "current", False: "stale", None: "unknown freshness"}[assignment.get("inputs_current")]
            source_inputs = (f'<details><summary>Assignment source inputs ({freshness})</summary><dl>' + ''.join(
                f'<dt>{label}</dt><dd><code>{text(source[key])}</code></dd>'
                for key, label in (("candidate", "Candidate"), ("requirements", "Requirements"))) + '</dl></details>'
                if source else '<small>Assignment source inputs: Unknown</small>')
            report = assignment["result"]
            result = '<span class="muted">No report</span>'
            if report is not None:
                current = assignment["result_current"]
                result = (f'<span class="report {text(report["outcome"])} {"current" if current else "stale"}">'
                          f'{text(report["outcome"].capitalize())} ({"current" if current else "stale"})</span>')
                evidence = "".join(f'<li><code>{text(ref)}</code></li>' for ref in report["artifacts"])
                result += f'<details><summary>Evidence</summary><ul>{evidence}</ul></details>'
            rows.append(f'<tr><th scope="row">{text(assignment["role"])}<small>{text(assignment["id"])}</small></th>'
                        f'<td>{text(assignment["goal"])}{source_inputs}</td><td>{requested}</td><td>{pane}</td>'
                        f'<td>{observed}</td><td>{result}</td></tr>')
        table = ('<div class="table-scroll"><table><thead><tr><th scope="col">Specialty</th>'
                 '<th scope="col">Assignment</th><th scope="col">Native request</th><th scope="col">Saved Rex pane</th><th scope="col">Process observation</th>'
                 '<th scope="col">Specialist report</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'
                 if rows else '<p class="empty">No assignments recorded.</p>')
        hold = (f'<p class="hold">Paused: {text(task["hold"])}</p>' if task["hold"] is not None
                else '<p class="hold-status">Not paused</p>')
        controls = ""
        if csrf_token is not None:
            action = "resume" if task["hold"] is not None else "pause"
            identity = text(task["id"])
            reason = (f'<label for="reason-{identity}">Pause reason</label>'
                      f'<input id="reason-{identity}" name="reason" maxlength="512" required>'
                      if action == "pause" else '<input type="hidden" name="reason" value="">')
            controls = (f'<div class="task-controls"><form method="post" action="/tasks/{identity}/hold">'
                        f'<input type="hidden" name="csrf" value="{text(csrf_token)}">'
                        f'<input type="hidden" name="request_id" value="{secrets.token_hex(16)}">'
                        f'<input type="hidden" name="expected_revision" value="{len(task["control_events"])}">'
                        f'<input type="hidden" name="action" value="{action}">'
                        f'{reason}<button>{action.capitalize()} new assignments</button></form>'
                        '<p class="muted">Pausing blocks new assignments. Running workers continue.</p></div>')
        history = []
        for event in reversed(task["control_events"][-5:]):
            label = "Resumed" if event["hold"] is None else "Paused: " + event["hold"]
            event_time = datetime.fromtimestamp(event["at"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            history.append(f'<li>{text(label)} <time>{text(event_time)}</time><small>Request {text(event["id"])}</small></li>')
        trace = ('<details class="control-history"><summary>Recent control requests</summary><ol>'
                 + "".join(history) + '</ol></details>' if history else "")
        limits = allowance_text(task["allowances"])
        inputs = task["inputs"]
        revisions = (f'<details class="revisions"><summary>Selected inputs</summary><dl><dt>Candidate</dt>'
                     f'<dd><code>{text(inputs["candidate"])}</code></dd><dt>Requirements</dt>'
                     f'<dd><code>{text(inputs["requirements"])}</code></dd></dl></details>' if inputs else "")
        item = task.get("provisioning")
        worktree = (f'<p>Writer worktree {text(item["id"])}: {text(item["state"])} '
                    f'({"current inputs" if item["inputs_current"] else "previous inputs"})<br>'
                    f'<code>{text(item["request"]["path"])}</code> '
                    f'<code>{text(item["request"]["branch"])}</code></p>' if item else "")
        tasks.append(f'<section class="task" aria-labelledby="task-{text(task["id"])}">'
                     f'<div class="task-heading"><div><h2 id="task-{text(task["id"])}">{text(task["id"])}</h2>'
                     f'<p class="goal">{text(task["goal"] or "Scope not admitted")}</p></div>{revisions}</div>'
                     f'{hold}{controls}{trace}{worktree}{table}<p class="allowances">{text(limits)}</p>'
                     f'{allowance_controls(task, csrf_token)}</section>')
    errors = "".join(f'<li><strong>{text(item["task"])}</strong>: {text(item["error"])}</li>' for item in view["errors"])
    unavailable = f'<aside class="errors"><h2>Records needing attention</h2><ul>{errors}</ul></aside>' if errors else ""
    routes = "".join(f'<tr><th scope="row">{text(route["id"])}</th><td>{text(route["harness"])}</td>'
                     f'<td>{text(route["readiness"])}</td><td>'
                     f'{"Unknown" if route["quota_observation"] is None else "Recorded; not refreshed"}</td></tr>'
                     for route in view["routes"])
    timestamp = datetime.fromtimestamp(view["observed_at"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    mode = "Live processes checked" if view["live"] else "Stored records only"
    facts = "/api/state?live=1" if view["live"] else "/api/state"
    content = "".join(tasks) or '<section class="task empty"><h2>No task records</h2><p>Admit a task in the factory CLI, then refresh this view.</p></section>'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>Software Factory</title><link rel="stylesheet" href="/dashboard.css"></head><body>'
            f'<header><div class="brand">Software Factory</div><div class="observation">{text(mode)}<br><time>{text(timestamp)}</time></div></header>'
            '<main><nav aria-label="Dashboard"><a href="#decisions">Decision inbox</a> <a href="#team">Team</a> <a href="#routes">Subscription routes</a></nav>'
            f'{render_inbox(view)}'
            '<div class="toolbar"><div><h1 id="team">Team</h1><p>Process observations and specialist reports stay separate. Header time is record collection time.</p></div>'
            '<form method="get"><a class="button secondary" href="/">Refresh records</a><button name="live" value="1">Check live processes</button></form></div>'
            f'{unavailable}{content}<section class="routes" id="routes"><h2>Subscription routes</h2><p>Configured readiness; capacity has not been refreshed here.</p>'
            '<div class="table-scroll"><table><thead><tr><th scope="col">Route</th><th scope="col">Harness</th><th scope="col">Catalog status</th><th scope="col">Capacity observation</th></tr></thead>'
            f'<tbody>{routes}</tbody></table></div></section><footer>Reports do not accept a task or satisfy release gates. '
            f'This view collected records in {view["collection_seconds"]:.3f}s. <a href="{facts}">View source facts</a></footer></main></body></html>')


def authorized(header, access_key):
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, password = base64.b64decode(encoded.strip(), validate=True).decode("utf-8").split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    return user == "operator" and secrets.compare_digest(password.encode(), access_key.encode())


@contextmanager
def access_file(access_key, url):
    """Only the launching user can read this process's temporary capability."""
    with tempfile.TemporaryDirectory(prefix="software-factory-view-") as directory:
        path = Path(directory) / "access.json"
        factory.save(path, dict(schema_version=1, username="operator", password=access_key, url=url))
        yield path


def make_server(state_dir, port=8090, *, access_key, decision_store=None):
    if not isinstance(access_key, str) or not access_key:
        raise ValueError("Supply a nonempty private per-process access capability")
    csrf_token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def check_access(self):
            allowed = {f"127.0.0.1:{active_port}", f"localhost:{active_port}"}
            if active_port == 80:
                allowed.update(("127.0.0.1", "localhost"))
            if self.headers.get("Host", "").lower() not in allowed:
                self.send_error(403, "Use this dashboard's loopback address")
                return False
            if not authorized(self.headers.get("Authorization", ""), access_key):
                body = b'{"error":"Use operator credentials from the private access file announced at startup"}'
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Software Factory", charset="UTF-8"')
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return False
            return True

        def do_GET(self):
            if not self.check_access():
                return
            try:
                target = urlsplit(self.path)
            except ValueError:
                self.send_error(400, "Use a dashboard relative URL")
                return
            # No aliases, fragments, encoded fields or arbitrary Rex scope parameters.
            if (target.scheme or target.netloc or target.fragment or not self.path.startswith("/")
                    or any(ord(character) <= 32 or ord(character) == 127 for character in self.path)
                    or self.path.startswith("//")
                    or target.query not in ("", "live=1")):
                self.send_error(400, "Use only the live=1 observation option")
                return
            pane = re.fullmatch(r"/tasks/(" + SLUG + r")/assignments/(" + SLUG + r")/pane", target.path)
            if (pane or target.path == "/dashboard.css") and target.query:
                self.send_error(400, "Use the saved pane or asset link without query parameters")
                return
            if target.path not in ("/", "/api/state", "/dashboard.css") and pane is None:
                self.send_error(404, "Use / or /api/state")
                return
            status, content_type = 200, "text/html; charset=utf-8"
            try:
                if target.path == "/dashboard.css":
                    body, content_type = (ASSETS / "dashboard.css").read_text(), "text/css; charset=utf-8"
                elif pane is not None:
                    detail = pane_detail(state_dir, pane[1], pane[2])
                    body = render_pane(detail)
                    if detail["error"]:
                        status = 503
                else:
                    view = snapshot(state_dir, decision_store=decision_store, live=bool(target.query))
                    if target.path == "/api/state":
                        body, content_type = json.dumps(view, allow_nan=False), "application/json"
                    else:
                        body = render(view, csrf_token=csrf_token)
            except (factory.FactoryError, OSError, ValueError) as exc:
                status = 503
                if target.path == "/api/state":
                    body, content_type = json.dumps(dict(schema_version=4, error=str(exc))), "application/json"
                else:
                    body = f'<h1>Factory records unavailable</h1><p>{text(exc)}</p><p>Restore the source, then refresh.</p>'
                print("dashboard: Restore the source, then refresh. " + str(exc), file=sys.stderr)
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if not self.check_access():
                return
            target = urlsplit(self.path)
            match = re.fullmatch(r"/tasks/(" + SLUG + r")/(hold|allowance)", self.path)
            if target.scheme or target.netloc or target.query or target.fragment or match is None:
                self.send_error(404, "Use a task's control form")
                return
            origin = self.headers.get("Origin")
            if ((origin is not None or match[2] == "allowance")
                    and origin != "http://" + self.headers.get("Host", "")):
                self.send_error(403, "Submit controls from this dashboard's own page")
                return
            try:
                length = self.headers.get("Content-Length", "")
                if (self.headers.get("Transfer-Encoding") is not None or not re.fullmatch(r"[0-9]+", length)
                        or not 0 < int(length) <= 8192
                        or self.headers.get("Content-Type", "").split(";", 1)[0] != "application/x-www-form-urlencoded"):
                    raise ValueError("Use a bounded form body")
                body = self.rfile.read(int(length))
                if len(body) != int(length):
                    raise ValueError("Incomplete form body")
                fields = parse_qs(body.decode("utf-8"), keep_blank_values=True,
                                  strict_parsing=True, errors="strict", max_num_fields=5)
                expected = ({"csrf", "request_id", "expected_revision", "action", "reason"} if match[2] == "hold"
                            else {"csrf", "request_id", "expected_limit", "purpose", "reason"})
                if (set(fields) != expected
                        or any(len(values) != 1 for values in fields.values())):
                    raise ValueError("Use the complete task form without duplicate fields")
                values = {key: items[0] for key, items in fields.items()}
                if not secrets.compare_digest(values["csrf"].encode(), csrf_token.encode()):
                    self.send_error(403, "Refresh the dashboard before submitting controls")
                    return
                if not re.fullmatch(SLUG, values["request_id"]) or len(values["reason"]) > 512:
                    raise ValueError("Supply a valid request ID and bounded reason")
                if match[2] == "hold":
                    if (values["action"] not in {"pause", "resume"}
                            or not re.fullmatch(r"[0-9]+", values["expected_revision"])
                            or (values["action"] == "pause" and not values["reason"].strip())
                            or (values["action"] == "resume" and values["reason"] != "")):
                        raise ValueError("Supply an action, revision and pause reason")
                    revision = int(values["expected_revision"])
                elif (values["purpose"] not in {"correction", "investigation"}
                      or not re.fullmatch(r"[0-9]+", values["expected_limit"])
                      or not values["reason"].strip()):
                    raise ValueError("Supply a purpose, observed limit and approval reason")
                if match[2] == "allowance":
                    expected_limit = int(values["expected_limit"])
            except (ValueError, UnicodeDecodeError):
                self.send_error(400, "Invalid control request; refresh the form and supply a reason of at most 512 characters")
                return
            try:
                with factory.task_lock(state_dir, match[1]) as path:
                    if match[2] == "hold":
                        factory.hold_task(path, values["reason"] if values["action"] == "pause" else None,
                                          request_id=values["request_id"], expected_revision=revision)
                    else:
                        factory.extend_allowance(path, purpose=values["purpose"], additional=1,
                            expected_limit=expected_limit, request_id=values["request_id"], reason=values["reason"])
            except factory.ControlConflict as exc:
                self.send_error(409, str(exc))
                return
            except (factory.FactoryError, OSError) as exc:
                self.send_error(503, "Task controls unavailable; inspect/restore the original state. " + str(exc))
                return
            self.send_response(303)
            self.send_header("Location", "/#task-" + match[1])
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, fmt, *args):
            # Keep private request URLs out of access logs; source failures go to stderr above.
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    active_port = server.server_port
    return server


def port_number(value):
    number = int(value)
    if not 0 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be 0–65535; choose 0 for a free local port")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=factory.ROOT / ".factory-state")
    parser.add_argument("--decision-store", type=Path, help="Existing private Jev store; omitted means unknown/not configured")
    parser.add_argument("--port", type=port_number, default=8090)
    args = parser.parse_args(argv)
    try:
        access_key = secrets.token_urlsafe(32)
        with make_server(args.state_dir.resolve(), args.port, access_key=access_key,
                         decision_store=args.decision_store.absolute() if args.decision_store is not None else None) as server:
            url = f"http://127.0.0.1:{server.server_port}"
            with access_file(access_key, url) as path:
                print(json.dumps(dict(schema_version=1, url=url, username="operator", access_file=str(path),
                                      state_dir=str(args.state_dir.resolve()))), flush=True)
                server.serve_forever()
    except (OSError, ValueError) as exc:
        print("dashboard: Cannot start local view; choose an accessible state directory and free --port. " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
