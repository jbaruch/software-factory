"""Explicit native Rex launches. No scheduler, provider broker or model routing."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any
import uuid

ROOT = Path(__file__).resolve().parent


class FactoryError(Exception):
    pass


class Rex:
    def __init__(self, binary, server):
        self.binary, self.server = str(binary), server

    def call(self, *args, raw=False) -> Any:
        result = subprocess.run(
            [self.binary, "--autostart=false", "-S", self.server, *args],
            capture_output=True, text=True, timeout=20, check=False)
        if result.returncode:
            raise FactoryError(result.stderr.strip() or "Rex command failed")
        return result.stdout if raw else json.loads(result.stdout)


def save(path, value):
    """Save intent before launch, atomically; files are private to this user."""
    content = value if isinstance(value, str) else json.dumps(value, indent=2) + "\n"
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".saving-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def task_lock(directory, task):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", task):
        raise FactoryError("Task ID must be a lowercase slug, at most 64 characters")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / (task + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise FactoryError("Another command is working on this task; retry later") from exc
        yield directory / (task + ".json")
    finally:
        os.close(fd)


def new_task(task) -> dict:
    return dict(schema_version=1, task=task, admission=None, hold=None, rex=None,
                session_label="factory-" + task + "-" + uuid.uuid4().hex[:8],
                session_id=None, assignments={})


def load_task(path) -> dict:
    """Factory owns migration of its gate store; unknown formats cannot launch."""
    record = json.loads(path.read_text())
    if not isinstance(record, dict):
        raise FactoryError("Invalid task record; restore the original state before continuing")
    version = record.get("schema_version", 0)
    if type(version) is not int or version not in (0, 1):
        raise FactoryError("Unsupported task schema; update the factory before continuing")
    if (record.get("task") != path.stem or not isinstance(record.get("assignments"), dict)
            or not all(key in record for key in ("rex", "session_id", "session_label"))):
        raise FactoryError("Incomplete task record; restore the original state before continuing")
    if version == 0:
        if "admission" in record or "hold" in record:
            raise FactoryError("Unversioned task has new fields; restore its schema version before continuing")
        record.update(schema_version=1, admission=None, hold=None)
    if not all(key in record for key in ("admission", "hold")):
        raise FactoryError("Incomplete task record; restore the original state before continuing")
    if record["hold"] is not None and not isinstance(record["hold"], str):
        raise FactoryError("Invalid task hold; restore the original state before continuing")
    admission = record["admission"]
    if admission is not None:
        limits = admission.get("limits") if isinstance(admission, dict) else None
        if (not isinstance(limits, dict) or set(limits) != {"correction", "investigation"}
                or any(type(limit) is not int or limit < 0 for limit in limits.values())):
            raise FactoryError("Invalid task limits; restore the original state before continuing")
    for assignment in record["assignments"].values():
        if (not isinstance(assignment, dict) or not isinstance(assignment.get("request"), dict)
                or assignment.get("state") not in {"launching", "launched"}
                or (admission is not None and assignment["request"].get("purpose")
                    not in {"work", "correction", "investigation"})):
            raise FactoryError("Invalid assignment record; restore the original state before continuing")
    if version == 0:
        save(path, record)
    return record


def admit(path, *, task, goal, repository, acceptance, corrections=None, investigations=None):
    """Record operator-supplied scope once, without starting Rex or any worker."""
    if not goal.strip() or not acceptance or any(not ref.strip() for ref in acceptance):
        raise FactoryError("Supply a goal and at least one nonempty acceptance reference")
    policies = json.loads((ROOT / "config/coordination.json").read_text())["policies"]
    limits = dict(correction=policies["max_corrections"] if corrections is None else corrections,
                  investigation=policies["max_investigations"] if investigations is None else investigations)
    if any(type(limit) is not int or limit < 0 for limit in limits.values()):
        raise FactoryError("Attempt limits must be nonnegative integers")
    repository = str(Path(repository).resolve(strict=True))
    admission = dict(goal=goal, repository=repository, acceptance=acceptance, limits=limits)
    record = load_task(path) if path.exists() else new_task(task)
    if record["admission"] is not None:
        if any(record["admission"].get(key) != value for key, value in admission.items()):
            raise FactoryError("Task already has different admission inputs; use a new task ID")
        return record
    if record["assignments"]:
        raise FactoryError("Existing launches have unclassified attempts; admit a new task ID")
    revision = subprocess.run(["git", "-C", repository, "rev-parse", "--verify", "HEAD^{commit}"],
                              capture_output=True, text=True, timeout=10, check=False)
    if revision.returncode:
        raise FactoryError("Repository needs an existing Git commit; supply its checkout path")
    admission["initial_candidate"] = revision.stdout.strip()
    record["admission"] = admission
    save(path, record)
    return record


def task_view(record):
    """An offline snapshot, never a live process or task-acceptance verdict."""
    allowances = None
    if record["admission"] is not None:
        allowances = {}
        for purpose, limit in record["admission"]["limits"].items():
            used = sum(item["request"].get("purpose") == purpose
                       for item in record["assignments"].values())
            allowances[purpose] = dict(limit=limit, used=used, remaining=limit - used)
    return {**record, "allowances": allowances}


def hold_task(path, reason):
    if reason is not None and not reason.strip():
        raise FactoryError("Supply a nonempty pause reason")
    record = load_task(path)
    record["hold"] = reason
    save(path, record)
    return record


def brief(role_id, goal):
    roles = json.loads((ROOT / "skills/factory/references/roles.json").read_text())["roles"]
    role = next((item for item in roles if item["id"] == role_id), None)
    if role is None:
        raise FactoryError("Unknown specialty: " + role_id)
    return (f"# Assignment: {role_id}\n\n{goal}\n\n"
            + "\n".join("- " + item for item in role["instructions"])
            + "\n\nAuthority: " + role["decision_authority"]
            + "\nReturn: " + role["result"]
            + "\nUse original evidence; do not create a separate handover or status ledger.\n"
            + "Existing repository policy and user authorization remain binding.\n")


def connect(record, binary=None, server=None):
    connection = record["rex"]
    if binary and str(Path(binary).resolve()) != connection["binary"]:
        raise FactoryError("Task is pinned to another Rex binary")
    if server and server != connection["server"]:
        raise FactoryError("Task is pinned to another Rex endpoint")
    rex = Rex(connection["binary"], connection["server"])
    who = rex.call("whoami", "--json")
    if who["host_id"] != connection["host_id"]:
        raise FactoryError("Connected Rex host differs from the recorded task host")
    return rex


def reconcile(rex, record, assignment):
    """Read back a uniquely labeled pending launch; never relaunch an unknown one."""
    session_id = record.get("session_id")
    if not session_id:
        sessions = rex.call("ls", "--json")["sessions"]
        matches = [item for item in sessions if item.get("label") == record["session_label"]]
        if len(matches) != 1:
            raise FactoryError("Launch outcome unknown; no command was retried. Inspect Rex for "
                               + record["session_label"])
        session_id = matches[0]["session_id"]
    view = rex.call("session", "inspect", session_id, "--json")
    windows = [item for item in view.get("windows", [])
               if item.get("label") == assignment["window_label"]]
    blocks = [block for window in windows for layer in window.get("layers", [])
              for block in layer.get("blocks", [])]
    if len(windows) != 1 or len(blocks) != 1:
        raise FactoryError("Launch outcome unknown; expected one exact assignment window/block")
    record["session_id"] = session_id
    assignment.update(window_id=windows[0]["window_id"],
                      block_id=blocks[0]["block_id"], state="launched")


def launch(path, *, task, assignment_id, role, goal, cwd, command,
           binary=None, server=None, purpose=None):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", assignment_id):
        raise FactoryError("Assignment ID must be a lowercase slug, at most 64 characters")
    cwd = str(Path(cwd).resolve(strict=True))
    if not Path(cwd).is_dir() or not command or not Path(command[0]).is_file():
        raise FactoryError("Provide an existing directory and an absolute executable command")
    if not Path(command[0]).is_absolute() or not os.access(command[0], os.X_OK):
        raise FactoryError("Command executable must be an absolute executable file")
    instructions = brief(role, goal)
    request = dict(role=role, goal=goal, cwd=cwd, command=command,
                   instructions_sha256=hashlib.sha256(instructions.encode()).hexdigest())
    record = load_task(path) if path.exists() else new_task(task)
    if record["admission"] is not None:
        if purpose not in {"work", "correction", "investigation"}:
            raise FactoryError("Admitted tasks require --purpose work, correction or investigation")
        if (role == "investigator") != (purpose == "investigation"):
            raise FactoryError("Use purpose investigation with the investigator role")
        request["purpose"] = purpose
    elif purpose is not None:
        raise FactoryError("Admit a new task before supplying an attempt purpose")
    existing = record["assignments"].get(assignment_id)
    if existing:
        if existing["request"] != request:
            raise FactoryError("Assignment ID already has different inputs; use a new explicit ID")
        rex = connect(record, binary, server)
        if not existing.get("block_id"):
            reconcile(rex, record, existing)
            save(path, record)
        return record, existing
    if record["hold"] is not None:
        raise FactoryError("Task is paused: " + record["hold"] + "; use resume before new launches")
    if any(item["state"] == "launching" for item in record["assignments"].values()):
        raise FactoryError("Reconcile the existing pending assignment with status before another launch")
    allowances = task_view(record)["allowances"]
    if allowances is not None and purpose in allowances and allowances[purpose]["remaining"] <= 0:
        owner = "investigator" if purpose == "correction" else "operator"
        raise FactoryError(f"Task {purpose} allowance exhausted; route to {owner}")
    if record["rex"] is not None:
        rex = connect(record, binary, server)
    else:
        if (not binary or not Path(binary).is_absolute() or not Path(binary).is_file()
                or not os.access(binary, os.X_OK) or not server or not server.startswith("unix://")):
            raise FactoryError("First launch requires --rex PATH and an explicit local --server unix:// endpoint")
        rex = Rex(str(Path(binary).resolve()), server)
        who = rex.call("whoami", "--json")
        record["rex"] = dict(binary=rex.binary, server=server, host_id=who["host_id"])
    brief_path = path.parent / (task + "-" + assignment_id + ".md")
    save(brief_path, instructions)
    replacements = {"{brief}": str(brief_path), "{instructions}": instructions}
    argv = [replacements.get(part, part) for part in command]
    assignment = dict(request=request, state="launching", brief=str(brief_path),
                      window_label=role + "-" + assignment_id + "-" + uuid.uuid4().hex[:8])
    record["assignments"][assignment_id] = assignment
    save(path, record)
    options = ["--cwd", cwd, "--keep-open", "--shell=none", "--json", "--", *argv]
    try:
        if record["session_id"]:
            created = rex.call("window", "new", "-s", record["session_id"],
                               assignment["window_label"], "--focus=false", *options)
        else:
            created_session = rex.call("new", record["session_label"], "--window",
                                       assignment["window_label"], *options)
            record["session_id"] = created_session["session_id"]
            created = created_session["initial_windows"][0]
        assignment.update(window_id=created["window_id"], block_id=created["block_ids"][0],
                          state="launched")
        save(path, record)
    except (FactoryError, OSError, subprocess.TimeoutExpired, ValueError, KeyError, IndexError) as exc:
        raise FactoryError("Launch outcome uncertain; use status to reconcile. No automatic retry. "
                           + str(exc)) from exc
    return record, assignment


def status(rex, record, assignment):
    process = rex.call("block", "call", "-s", record["session_id"],
                       "-b", assignment["block_id"], "process")
    last_exit = process.get("last_exit")
    state = ("exited" if last_exit["exit_code"] == 0 else "failed") if last_exit else (
        "running" if process.get("child") or process.get("foreground") else "unknown")
    return dict(role=assignment["request"]["role"], state=state,
                session_id=record["session_id"], window_id=assignment["window_id"],
                block_id=assignment["block_id"], exit=last_exit)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".factory-state")
    parser.add_argument("--rex", help="Absolute Rex binary; required for the first launch")
    parser.add_argument("--server", help="Explicit local unix:// Rex endpoint; pinned per task")
    sub = parser.add_subparsers(dest="action", required=True)
    start = sub.add_parser("start", help="Launch an explicit command, or reuse the same assignment")
    start.add_argument("--task", required=True)
    start.add_argument("--id", required=True, help="Stable assignment ID; new attempt needs a new ID")
    start.add_argument("--role", required=True)
    start.add_argument("--goal", required=True)
    start.add_argument("--cwd", required=True)
    start.add_argument("--purpose", choices=("work", "correction", "investigation"),
                       help="Required for admitted tasks; consumes the declared task allowance")
    start.add_argument("command", nargs=argparse.REMAINDER)
    admission = sub.add_parser("admit", help="Record task scope and limits without launching")
    admission.add_argument("--task", required=True)
    admission.add_argument("--goal", required=True)
    admission.add_argument("--repo", required=True)
    admission.add_argument("--acceptance", action="append", required=True, help="Original evidence reference")
    admission.add_argument("--corrections", type=int)
    admission.add_argument("--investigations", type=int)
    for action in ("show", "pause", "resume"):
        command = sub.add_parser(action)
        command.add_argument("--task", required=True)
        if action == "pause":
            command.add_argument("--reason", required=True)
    for action in ("status", "capture"):
        command = sub.add_parser(action)
        command.add_argument("--task", required=True)
        command.add_argument("--id", required=True)
    args = parser.parse_args(argv)
    try:
        with task_lock(args.state_dir.resolve(), args.task) as path:
            if args.action in {"admit", "show", "pause", "resume"}:
                if args.action == "admit":
                    record = admit(path, task=args.task, goal=args.goal, repository=args.repo,
                                   acceptance=args.acceptance, corrections=args.corrections,
                                   investigations=args.investigations)
                elif args.action == "show":
                    record = load_task(path)
                else:
                    record = hold_task(path, args.reason if args.action == "pause" else None)
                print(json.dumps(task_view(record), indent=2))
                return 0
            if args.action == "start":
                argv = args.command[1:] if args.command[:1] == ["--"] else args.command
                record, assignment = launch(path, task=args.task, assignment_id=args.id,
                    role=args.role, goal=args.goal, cwd=args.cwd, command=argv,
                    binary=args.rex, server=args.server, purpose=args.purpose)
                rex = connect(record)
            else:
                record = load_task(path)
                assignment = record["assignments"][args.id]
                rex = connect(record, args.rex, args.server)
                if not assignment.get("block_id"):
                    reconcile(rex, record, assignment)
                    save(path, record)
            if args.action == "capture":
                print(rex.call("capture", "-s", record["session_id"], "-b",
                               assignment["block_id"], "--trim", "--unwrap", raw=True), end="")
            else:
                print(json.dumps(status(rex, record, assignment), indent=2))
    except (FactoryError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print("factory: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
