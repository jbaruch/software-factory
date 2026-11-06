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
RESULT_OUTCOMES = {"completed", "approved", "findings", "blocked"}
REPOSITORY_MARKER = "software-factory-repository.json"


class FactoryError(Exception):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


class Rex:
    def __init__(self, binary, server):
        self.binary, self.server = str(binary), server

    def call(self, *args, raw=False) -> Any:
        result = subprocess.run(
            [self.binary, "--autostart=false", "-S", self.server, *args],
            capture_output=True, text=True, timeout=20, check=False)
        if result.returncode:
            raise FactoryError(result.stderr.strip() or "Rex command failed")
        if raw:
            return result.stdout
        try:
            return json.loads(result.stdout)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise FactoryError("Cannot decode Rex response; inspect the pinned binary/server API compatibility") from None


def save(path, value, *, exclusive=False):
    """Save intent before launch, atomically; files are private to this user."""
    content = value if isinstance(value, str) else json.dumps(value, indent=2) + "\n"
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".saving-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(name, path)
        else:
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
    return dict(schema_version=4, task=task, admission=None, inputs=None, hold=None, rex=None,
                session_label="factory-" + task + "-" + uuid.uuid4().hex[:8],
                session_id=None, assignments={})


def load_task(path) -> dict:
    """Factory owns migration of its gate store; unknown formats cannot launch."""
    try:
        record = json.loads(path.read_text(), object_pairs_hook=unique_object)
    except FileNotFoundError:
        raise FactoryError(f"Unknown task {path.stem!r}; correct its ID or create it with admit/start") from None
    except (ValueError, UnicodeDecodeError):
        raise FactoryError(f"Cannot decode task {path.stem!r}; restore the original task record before continuing") from None
    except OSError:
        raise FactoryError(f"Cannot read task {path.stem!r}; restore access to its original state file") from None
    if not isinstance(record, dict):
        raise FactoryError("Invalid task record; restore the original state before continuing")
    version = record.get("schema_version", 0)
    if type(version) is not int or version not in (0, 1, 2, 3, 4):
        raise FactoryError("Unsupported task schema; update the factory before continuing")
    if (record.get("task") != path.stem or not isinstance(record.get("assignments"), dict)
            or not all(key in record for key in ("rex", "session_id", "session_label"))):
        raise FactoryError("Incomplete task record; restore the original state before continuing")
    if version < 2 and ("inputs" in record or any(
            isinstance(item, dict) and ("result" in item or (
                isinstance(item.get("request"), dict)
                and any(key in item["request"] for key in ("inputs", "dependencies"))))
            for item in record["assignments"].values())):
        raise FactoryError("Legacy task has newer fields; restore its schema version before continuing")
    if version < 3 and isinstance(record.get("admission"), dict) and "repository_identity" in record["admission"]:
        raise FactoryError("Legacy task has newer fields; restore its schema version before continuing")
    if version < 4 and isinstance(record.get("admission"), dict) and "legacy_repository_identity" in record["admission"]:
        raise FactoryError("Legacy task has newer fields; restore its schema version before continuing")
    connection = record["rex"]
    if not record["assignments"] and (connection is not None or record["session_id"] is not None):
        raise FactoryError("Missing assignment history for recorded Rex identity; restore the original task record")
    if connection is None:
        if record["assignments"] or record["session_id"] is not None:
            raise FactoryError("Missing pinned Rex identity; restore the original task record before continuing")
    elif (not isinstance(connection, dict)
            or any(not isinstance(connection.get(key), str) or not connection[key].strip()
                   for key in ("binary", "server", "host_id"))
            or not Path(connection["binary"]).is_absolute() or not connection["server"].startswith("unix://")):
        raise FactoryError("Invalid pinned Rex identity; restore the original task record before continuing")
    if (not isinstance(record["session_label"], str)
            or not re.fullmatch("factory-" + re.escape(record["task"]) + r"-[0-9a-f]{8}", record["session_label"])
            or (record["session_id"] is not None
                and (not isinstance(record["session_id"], str) or not record["session_id"]))):
        raise FactoryError("Invalid Rex session identity; restore the original task record before continuing")
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
        if (not isinstance(admission, dict)
                or any(not isinstance(admission.get(key), str) or not admission[key].strip()
                       for key in ("goal", "repository", "initial_candidate"))
                or not Path(admission["repository"]).is_absolute()
                or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", admission["initial_candidate"])
                or not isinstance(admission.get("acceptance"), list) or not admission["acceptance"]
                or any(not isinstance(ref, str) or not ref.strip() for ref in admission["acceptance"])):
            raise FactoryError("Invalid task admission; restore its goal, repository, acceptance and initial candidate")
        limits = admission.get("limits") if isinstance(admission, dict) else None
        if (not isinstance(limits, dict) or set(limits) != {"correction", "investigation"}
                or any(type(limit) is not int or limit < 0 for limit in limits.values())):
            raise FactoryError("Invalid task limits; restore the original state before continuing")
    earlier, labels, window_ids, block_ids = set(), set(), set(), set()
    for assignment_id, assignment in record["assignments"].items():
        if (not isinstance(assignment, dict) or not isinstance(assignment.get("request"), dict)
                or assignment.get("state") not in ("launching", "launched")
                or (admission is not None and assignment["request"].get("purpose")
                    not in ("work", "correction", "investigation"))):
            raise FactoryError("Invalid assignment record; restore the original state before continuing")
        request = assignment["request"]
        if admission is None and any(key in request for key in ("purpose", "inputs", "dependencies")):
            raise FactoryError("Invalid unadmitted assignment; restore its original task scope before continuing")
        if admission is not None and ((request.get("role") == "investigator")
                                     != (request.get("purpose") == "investigation")):
            raise FactoryError("Invalid investigation purpose; restore the original task record")
        if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", assignment_id)
                or any(not isinstance(request.get(key), str) or not request[key].strip()
                       for key in ("role", "goal", "cwd", "instructions_sha256"))
                or not Path(request["cwd"]).is_absolute()
                or not re.fullmatch(r"[0-9a-f]{64}", request["instructions_sha256"])
                or not isinstance(request.get("command"), list) or not request["command"]
                or any(not isinstance(part, str) for part in request["command"])
                or not Path(request["command"][0]).is_absolute()
                or any(not isinstance(assignment.get(key), str) or not assignment[key].strip()
                       for key in ("brief", "window_label"))
                or not Path(assignment["brief"]).is_absolute()):
            raise FactoryError("Invalid assignment identity or request; restore the original task record")
        if not re.fullmatch(re.escape(request["role"] + "-" + assignment_id) + r"-[0-9a-f]{8}",
                            assignment["window_label"]):
            raise FactoryError("Assignment window marker differs from its owner; restore the original task record")
        if "inputs" in request and (admission is None or not valid_inputs(request["inputs"])):
            raise FactoryError("Invalid assignment inputs; restore the original task record")
        if "dependencies" in request and (not isinstance(request["dependencies"], list)
                or any(not isinstance(item, str) or item not in earlier
                       for item in request["dependencies"])):
            raise FactoryError("Invalid assignment dependencies; restore the original task record")
        if assignment["state"] == "launched" and (record["session_id"] is None
                or any(not isinstance(assignment.get(key), str) or not assignment[key]
                       for key in ("window_id", "block_id"))):
            raise FactoryError("Missing launched pane identity; restore the original task record before continuing")
        if assignment["state"] == "launching" and any(key in assignment for key in ("window_id", "block_id")):
            raise FactoryError("Pending launch has pane IDs; restore the original task record before reconciliation")
        if assignment["window_label"] in labels or (assignment["state"] == "launched" and (
                assignment["window_id"] in window_ids or assignment["block_id"] in block_ids)):
            raise FactoryError("Duplicate assignment pane identity; restore the original task record")
        labels.add(assignment["window_label"])
        if assignment["state"] == "launched":
            window_ids.add(assignment["window_id"])
            block_ids.add(assignment["block_id"])
        earlier.add(assignment_id)
    if version < 2:
        record["inputs"] = (dict(candidate=admission["initial_candidate"],
                                 requirements=admission["initial_candidate"])
                            if admission is not None else None)
        for assignment in record["assignments"].values():
            assignment["result"] = None
        record["schema_version"] = 2
    if ("inputs" not in record or (record["inputs"] is not None if admission is None
                                   else not valid_inputs(record["inputs"]))):
        raise FactoryError("Invalid task inputs; restore the original state before continuing")
    for assignment in record["assignments"].values():
        if "result" not in assignment:
            raise FactoryError("Missing assignment result field; restore the original state before continuing")
        result = assignment["result"]
        if result is not None and (not isinstance(result, dict)
                or assignment["state"] != "launched"
                or not isinstance(result.get("outcome"), str) or result["outcome"] not in RESULT_OUTCOMES
                or not valid_inputs(result.get("inputs"))
                or result["inputs"] != assignment["request"].get("inputs")
                or not isinstance(result.get("candidate"), str)
                or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", result["candidate"])
                or (result["outcome"] != "completed" and result["candidate"] != result["inputs"]["candidate"])
                or not isinstance(result.get("artifacts"), list) or not result["artifacts"]
                or any(not isinstance(ref, str) or not ref.strip() for ref in result["artifacts"])):
            raise FactoryError("Invalid assignment result; restore the original state before continuing")
    if version < 3:
        if admission is not None:
            admission["repository_identity"] = None
    if admission is not None:
        if "repository_identity" not in admission:
            raise FactoryError("Missing repository identity; restore the original task record")
        identity = admission["repository_identity"]
        validator = valid_legacy_repository_identity if version == 3 else valid_repository_identity
        if identity is not None and not validator(identity):
            raise FactoryError("Invalid repository identity; restore the original task record")
        if version == 3 and identity is not None:
            admission["legacy_repository_identity"] = identity
            admission["repository_identity"] = None
        if ("legacy_repository_identity" in admission
                and not valid_legacy_repository_identity(admission["legacy_repository_identity"])):
            raise FactoryError("Invalid legacy repository identity; restore the original task record")
    if version < 4:
        record["schema_version"] = 4
        save(path, record)
    return record


def valid_inputs(value):
    return isinstance(value, dict) and all(isinstance(value.get(key), str) and value[key].strip()
                                         for key in ("candidate", "requirements")) and bool(
        re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value["candidate"]))


def valid_repository_identity(value):
    return (isinstance(value, dict) and set(value) == {"common_dir", "repository_id"}
            and isinstance(value.get("common_dir"), str) and Path(value["common_dir"]).is_absolute()
            and isinstance(value.get("repository_id"), str)
            and bool(re.fullmatch(r"[0-9a-f]{32}", value["repository_id"])))


def valid_legacy_repository_identity(value):
    return (isinstance(value, dict) and set(value) == {"common_dir", "device", "inode"}
            and isinstance(value.get("common_dir"), str) and Path(value["common_dir"]).is_absolute()
            and type(value.get("device")) is int and value["device"] >= 0
            and type(value.get("inode")) is int and value["inode"] > 0)


def git_run(repository, *args):
    try:
        return subprocess.run(["git", "-C", repository, *args], capture_output=True,
                              text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise FactoryError("Cannot run Git; restore Git availability and checkout access, then retry the command") from None


def git_commit(repository, revision):
    result = git_run(repository, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}")
    if result.returncode:
        raise FactoryError("Revision is not an existing Git commit; fetch it or supply a local commit")
    return result.stdout.strip()


def git_common_dir(repository):
    result = git_run(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if result.returncode:
        raise FactoryError("Working directory is not a Git checkout; supply an isolated worktree of the admitted repository")
    return Path(result.stdout.strip()).resolve()


def repository_identity(repository, *, create=False):
    common = git_common_dir(repository)
    marker = common / REPOSITORY_MARKER
    try:
        if create and not marker.exists():
            try:
                save(marker, dict(schema_version=1, repository_id=uuid.uuid4().hex), exclusive=True)
            except FileExistsError:
                pass  # A concurrent admission published the shared marker; read it below.
        value = json.loads(marker.read_text(), object_pairs_hook=unique_object)
    except (OSError, ValueError, UnicodeDecodeError):
        raise FactoryError("Cannot read repository marker; restore the original Git-local marker and access, or have the operator authorize a new admission") from None
    if (not isinstance(value, dict) or set(value) != {"schema_version", "repository_id"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not valid_repository_identity(dict(common_dir=str(common), repository_id=value["repository_id"]))):
        raise FactoryError("Unsupported or damaged repository marker; update the factory or restore the original marker before continuing")
    return dict(common_dir=str(common), repository_id=value["repository_id"])


def check_repository(admission):
    if admission["repository_identity"] is None:
        raise FactoryError("Legacy admission lacks repository identity; inspect its history and have the operator authorize a new admission")
    try:
        if repository_identity(admission["repository"]) != admission["repository_identity"]:
            raise FactoryError("Repository identity differs; restore the original Git-local marker and common directory")
        observed = git_commit(admission["repository"], admission["initial_candidate"])
    except FactoryError as exc:
        raise FactoryError("Cannot verify the admitted repository pin; inspect/restore the original checkout. " + str(exc)) from exc
    if observed != admission["initial_candidate"]:
        raise FactoryError("Admitted repository pin differs; restore the original checkout before continuing")


def check_checkout(repository, cwd, candidate, *, expected_identity=None):
    if expected_identity is not None and (not valid_repository_identity(expected_identity)
            or repository_identity(repository) != expected_identity):
        raise FactoryError("Assignment repository identity differs; restore the original Git-local marker and common directory")
    if git_commit(repository, candidate) != candidate:
        raise FactoryError("Stored candidate is not an exact commit; select task inputs before launching")
    if git_common_dir(repository) != git_common_dir(cwd) or git_commit(cwd, "HEAD") != candidate:
        raise FactoryError("Assignment checkout differs from the selected candidate; use its worktree at that commit")
    changed = git_run(cwd, "status", "--porcelain", "--untracked-files=normal")
    if changed.returncode:
        raise FactoryError("Cannot inspect assignment checkout; repair Git access before launching")
    if changed.stdout:
        raise FactoryError("Assignment checkout has uncommitted inputs; commit them or use a clean worktree before launching")
    if expected_identity is not None and (repository_identity(repository) != expected_identity
                                          or repository_identity(cwd) != expected_identity):
        raise FactoryError("Assignment repository identity differs; restore the original Git-local marker and common directory")


def admit(path, *, task, goal, repository, acceptance, corrections=None, investigations=None):
    """Record operator-supplied scope once, without starting Rex or any worker."""
    if not goal.strip() or not acceptance or any(not ref.strip() for ref in acceptance):
        raise FactoryError("Supply a goal and at least one nonempty acceptance reference")
    limits = dict(correction=corrections, investigation=investigations)
    if any(limit is None for limit in limits.values()):
        try:
            policies = json.loads((ROOT / "config/coordination.json").read_text(),
                                  object_pairs_hook=unique_object)["policies"]
            for purpose, key in (("correction", "max_corrections"), ("investigation", "max_investigations")):
                if limits[purpose] is None:
                    default = policies[key]
                    if type(default) is not int or default < 0:
                        raise ValueError("Invalid task limit default")
                    limits[purpose] = default
        except (OSError, ValueError, KeyError, TypeError):
            raise FactoryError("Cannot read task limit defaults from config/coordination.json; repair its policies or supply both --corrections and --investigations") from None
    if any(type(limit) is not int or limit < 0 for limit in limits.values()):
        raise FactoryError("Attempt limits must be nonnegative integers")
    try:
        repository = str(Path(repository).resolve(strict=True))
    except OSError:
        raise FactoryError("Cannot access --repo; supply an existing accessible Git checkout") from None
    admission = dict(goal=goal, repository=repository, acceptance=acceptance, limits=limits)
    record = load_task(path) if path.exists() else new_task(task)
    if record["admission"] is not None:
        if any(record["admission"].get(key) != value for key, value in admission.items()):
            raise FactoryError("Task already has different admission inputs; use a new task ID")
        check_repository(record["admission"])
        return record
    if record["assignments"]:
        raise FactoryError("Existing launches have unclassified attempts; admit a new task ID")
    admission["initial_candidate"] = git_commit(repository, "HEAD")
    admission["repository_identity"] = repository_identity(repository, create=True)
    record["admission"] = admission
    record["inputs"] = dict(candidate=admission["initial_candidate"],
                            requirements=admission["initial_candidate"])
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
    assignments = {key: {**item, "result_current": result_current(record, item["result"])}
                   for key, item in record["assignments"].items()}
    return {**record, "allowances": allowances, "assignments": assignments}


def result_current(record, result):
    return result is not None and record["inputs"] == dict(
        candidate=result["candidate"], requirements=result["inputs"]["requirements"])


def set_inputs(path, *, candidate, requirements):
    record = load_task(path)
    if record["admission"] is None or not requirements.strip():
        raise FactoryError("Admit a task and supply a nonempty requirements revision first")
    check_repository(record["admission"])
    record["inputs"] = dict(candidate=git_commit(record["admission"]["repository"], candidate),
                            requirements=requirements)
    save(path, record)
    return record


def record_result(path, *, assignment_id, candidate, requirements, outcome, artifacts):
    record = load_task(path)
    if assignment_id not in record["assignments"]:
        raise FactoryError(f"Unknown assignment {assignment_id!r}; use show --task {record['task']} to find its ID")
    assignment = record["assignments"][assignment_id]
    inputs = assignment["request"].get("inputs")
    if not valid_inputs(inputs):
        raise FactoryError("Assignment predates input binding; launch a new explicit assignment ID")
    if assignment["state"] != "launched":
        raise FactoryError("Reconcile the pending launch with status before recording its result")
    check_repository(record["admission"])
    reported = dict(candidate=git_commit(record["admission"]["repository"], candidate),
                    requirements=requirements)
    if requirements != inputs["requirements"] or (outcome != "completed" and reported != inputs):
        raise FactoryError("Result inputs differ from the assignment; report the revision it assessed")
    if outcome not in RESULT_OUTCOMES or not artifacts or any(not ref.strip() for ref in artifacts):
        raise FactoryError("Supply a supported outcome and at least one nonempty evidence reference")
    result = dict(inputs=dict(inputs), candidate=reported["candidate"], outcome=outcome, artifacts=artifacts)
    if assignment["result"] is not None and assignment["result"] != result:
        raise FactoryError("Assignment already has a different result; use a new explicit assignment ID")
    assignment["result"] = result
    save(path, record)
    return record


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
    who = rex_object(rex.call("whoami", "--json"), strings=("host_id",))
    if who["host_id"] != connection["host_id"]:
        raise FactoryError("Connected Rex host differs from the recorded task host")
    if record["session_id"] is not None:
        view = check_session(rex, record, record["session_id"])
        for assignment in record["assignments"].values():
            if assignment["state"] == "launched":
                window_id, block_id = assignment_pane(view, assignment)
                if (window_id, block_id) != (assignment["window_id"], assignment["block_id"]):
                    raise FactoryError("Rex pane identity differs from the task; inspect the original assignment before continuing")
    return rex


def check_session(rex, record, session_id):
    view = rex_object(rex.call("session", "inspect", session_id, "--json"),
                      strings=("session_id", "label"), lists=("windows",))
    if view["session_id"] != session_id:
        raise FactoryError("Rex session ID differs; inspect the pinned session before continuing")
    if view.get("label") != record["session_label"]:
        raise FactoryError("Rex session identity differs from the task; inspect the original session before continuing")
    labels, ids, blocks = set(), set(), set()
    for window in view["windows"]:
        rex_object(window, strings=("window_id", "label"), lists=("layers",))
        if window["label"] in labels or window["window_id"] in ids:
            raise FactoryError("Duplicate Rex window identity; inspect the original session before continuing")
        labels.add(window["label"])
        ids.add(window["window_id"])
        for layer in window["layers"]:
            rex_object(layer, lists=("blocks",))
            for block in layer["blocks"]:
                rex_object(block, strings=("block_id",))
                if block["block_id"] in blocks:
                    raise FactoryError("Duplicate Rex block identity; inspect the original session before continuing")
                blocks.add(block["block_id"])
    expected = {item["window_label"] for item in record["assignments"].values()}
    required = {item["window_label"] for item in record["assignments"].values() if item["state"] == "launched"}
    if not required <= labels <= expected:
        raise FactoryError("Rex assignment windows differ from saved history; inspect the pinned session and restore its windows or the original task history before continuing")
    return view


def rex_object(value, *, strings=(), lists=()):
    if (not isinstance(value, dict)
            or any(not isinstance(value.get(key), str) or not value[key] for key in strings)
            or any(not isinstance(value.get(key), list) for key in lists)):
        raise FactoryError("Malformed Rex response; inspect the pinned binary/server API compatibility")
    return value


def assignment_pane(view, assignment):
    windows = [item for item in view.get("windows", [])
               if item.get("label") == assignment["window_label"]]
    blocks = [block for window in windows for layer in window.get("layers", [])
              for block in layer.get("blocks", [])]
    if len(windows) != 1 or len(blocks) != 1:
        raise FactoryError("Launch outcome unknown; inspect Rex for one exact assignment window/block")
    window_id, block_id = windows[0].get("window_id"), blocks[0].get("block_id")
    if not isinstance(window_id, str) or not window_id or not isinstance(block_id, str) or not block_id:
        raise FactoryError("Rex pane identity is missing; inspect the original assignment before continuing")
    return window_id, block_id


def reconcile(rex, record, assignment):
    """Read back a uniquely labeled pending launch; never relaunch an unknown one."""
    session_id = record.get("session_id")
    if not session_id:
        sessions = rex_object(rex.call("ls", "--json"), lists=("sessions",))["sessions"]
        for item in sessions:
            rex_object(item, strings=("session_id",))
        matches = [item for item in sessions if item.get("label") == record["session_label"]]
        if len(matches) != 1:
            raise FactoryError("Launch outcome unknown; no command was retried. Inspect Rex for "
                               + record["session_label"])
        session_id = matches[0]["session_id"]
    view = check_session(rex, record, session_id)
    window_id, block_id = assignment_pane(view, assignment)
    record["session_id"] = session_id
    assignment.update(window_id=window_id, block_id=block_id, state="launched")


def launch(path, *, task, assignment_id, role, goal, cwd, command,
           binary=None, server=None, purpose=None, dependencies=()):
    if not goal.strip():
        raise FactoryError("Supply a nonempty assignment goal before launching")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", assignment_id):
        raise FactoryError("Assignment ID must be a lowercase slug, at most 64 characters")
    cwd = str(Path(cwd).resolve(strict=True))
    if not Path(cwd).is_dir() or not command or not Path(command[0]).is_file():
        raise FactoryError("Provide an existing directory and an absolute executable command")
    if not Path(command[0]).is_absolute() or not os.access(command[0], os.X_OK):
        raise FactoryError("Command executable must be an absolute executable file")
    record = load_task(path) if path.exists() else new_task(task)
    existing = record["assignments"].get(assignment_id)
    instructions = "" if existing else brief(role, goal)
    request: dict = dict(role=role, goal=goal, cwd=cwd, command=command)
    if record["admission"] is not None:
        if purpose not in {"work", "correction", "investigation"}:
            raise FactoryError("Admitted tasks require --purpose work, correction or investigation")
        if (role == "investigator") != (purpose == "investigation"):
            raise FactoryError("Use purpose investigation with the investigator role")
        request["purpose"] = purpose
        if not existing:
            instructions += ("\nTask goal: " + record["admission"]["goal"] + "\nAcceptance references:\n"
                             + "\n".join("- " + ref for ref in record["admission"]["acceptance"]) + "\n")
        inputs = existing["request"].get("inputs") if existing else record["inputs"]
        if inputs is not None:
            request["inputs"] = dict(inputs)
            if not existing:
                instructions += (f"\nCandidate commit: {inputs['candidate']}\n"
                                 f"Requirements revision: {inputs['requirements']}\n"
                                 "Bind your result to these inputs and link original evidence.\n")
    elif purpose is not None:
        raise FactoryError("Admit a new task before supplying an attempt purpose")
    if dependencies:
        if record["admission"] is None:
            raise FactoryError("Admit a task before declaring assignment dependencies")
        request["dependencies"] = sorted(set(dependencies))
    request["instructions_sha256"] = (existing["request"]["instructions_sha256"] if existing
                                      else hashlib.sha256(instructions.encode()).hexdigest())
    if existing:
        if existing["request"] != request:
            raise FactoryError("Assignment ID already has different inputs; use a new explicit ID")
        rex = connect(record, binary, server)
        if existing["state"] == "launching":
            reconcile(rex, record, existing)
            save(path, record)
        return record, existing
    if record["hold"] is not None:
        raise FactoryError("Task is paused: " + record["hold"] + "; use resume before new launches")
    if any(item["state"] == "launching" for item in record["assignments"].values()):
        raise FactoryError("Reconcile the existing pending assignment with status before another launch")
    for dependency in request.get("dependencies", []):
        if dependency not in record["assignments"] or dependency == assignment_id:
            raise FactoryError("Dependency must name an earlier assignment in this task: " + dependency)
        result = record["assignments"][dependency]["result"]
        if not result_current(record, result) or result["outcome"] not in {"completed", "approved"}:
            raise FactoryError("Dependency needs a successful result on current inputs: " + dependency)
    allowances = task_view(record)["allowances"]
    if allowances is not None and purpose in allowances and allowances[purpose]["remaining"] <= 0:
        owner = "investigator" if purpose == "correction" else "operator"
        raise FactoryError(f"Task {purpose} allowance exhausted; route to {owner}")
    if record["admission"] is not None:
        check_repository(record["admission"])
        current = record["inputs"]["candidate"]
        check_checkout(record["admission"]["repository"], cwd, current,
                       expected_identity=record["admission"]["repository_identity"])
    if record["rex"] is not None:
        rex = connect(record, binary, server)
    else:
        if (not binary or not Path(binary).is_absolute() or not Path(binary).is_file()
                or not os.access(binary, os.X_OK) or not server or not server.startswith("unix://")):
            raise FactoryError("First launch requires --rex PATH and an explicit local --server unix:// endpoint")
        rex = Rex(str(Path(binary).resolve()), server)
        who = rex_object(rex.call("whoami", "--json"), strings=("host_id",))
        record["rex"] = dict(binary=rex.binary, server=server, host_id=who["host_id"])
    brief_path = path.parent / (task + "." + assignment_id + ".md")
    save(brief_path, instructions)
    replacements = {"{brief}": str(brief_path), "{instructions}": instructions}
    argv = [replacements.get(part, part) for part in command]
    assignment: dict = dict(request=request, state="launching", result=None, brief=str(brief_path),
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
            rex_object(created_session, strings=("session_id",), lists=("initial_windows",))
            if len(created_session["initial_windows"]) != 1:
                raise FactoryError("Malformed Rex initial windows; inspect the pinned binary/server API compatibility")
            record["session_id"] = created_session["session_id"]
            created = created_session["initial_windows"][0]
        rex_object(created, strings=("window_id",), lists=("block_ids",))
        if len(created["block_ids"]) != 1 or not isinstance(created["block_ids"][0], str) or not created["block_ids"][0]:
            raise FactoryError("Malformed Rex created block IDs; inspect the pinned binary/server API compatibility")
        view = check_session(rex, record, record["session_id"])
        observed = assignment_pane(view, assignment)
        if observed != (created["window_id"], created["block_ids"][0]):
            raise FactoryError("Returned Rex pane IDs differ from the live assignment; inspect the original launch before continuing")
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
    rex_object(process)
    last_exit = process.get("last_exit")
    if last_exit is not None and (not isinstance(last_exit, dict) or type(last_exit.get("exit_code")) is not int):
        raise FactoryError("Malformed Rex exit status; inspect the pinned binary/server API compatibility")
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
    start.add_argument("--after", action="append", default=[], help="Earlier assignment that must have a current successful result")
    start.add_argument("command", nargs=argparse.REMAINDER)
    admission = sub.add_parser("admit", help="Record task scope and limits without launching")
    admission.add_argument("--task", required=True)
    admission.add_argument("--goal", required=True)
    admission.add_argument("--repo", required=True)
    admission.add_argument("--acceptance", action="append", required=True, help="Original evidence reference")
    admission.add_argument("--corrections", type=int)
    admission.add_argument("--investigations", type=int)
    inputs = sub.add_parser("inputs", help="Select the task's candidate and requirements revision")
    inputs.add_argument("--task", required=True)
    inputs.add_argument("--candidate", required=True)
    inputs.add_argument("--requirements", required=True)
    result = sub.add_parser("result", help="Record a revision-bound specialist outcome")
    result.add_argument("--task", required=True)
    result.add_argument("--id", required=True)
    result.add_argument("--candidate", required=True)
    result.add_argument("--requirements", required=True)
    result.add_argument("--outcome", choices=sorted(RESULT_OUTCOMES), required=True)
    result.add_argument("--artifact", action="append", required=True)
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
            if args.action in {"admit", "show", "pause", "resume", "inputs", "result"}:
                if args.action == "admit":
                    record = admit(path, task=args.task, goal=args.goal, repository=args.repo,
                                   acceptance=args.acceptance, corrections=args.corrections,
                                   investigations=args.investigations)
                elif args.action == "show":
                    record = load_task(path)
                elif args.action == "inputs":
                    record = set_inputs(path, candidate=args.candidate, requirements=args.requirements)
                elif args.action == "result":
                    record = record_result(path, assignment_id=args.id, candidate=args.candidate,
                                           requirements=args.requirements, outcome=args.outcome,
                                           artifacts=args.artifact)
                else:
                    record = hold_task(path, args.reason if args.action == "pause" else None)
                print(json.dumps(task_view(record), indent=2))
                return 0
            if args.action == "start":
                argv = args.command[1:] if args.command[:1] == ["--"] else args.command
                record, assignment = launch(path, task=args.task, assignment_id=args.id,
                    role=args.role, goal=args.goal, cwd=args.cwd, command=argv,
                    binary=args.rex, server=args.server, purpose=args.purpose, dependencies=args.after)
                rex = connect(record)
            else:
                record = load_task(path)
                if args.id not in record["assignments"]:
                    raise FactoryError(f"Unknown assignment {args.id!r}; use show --task {args.task} to find its ID")
                assignment = record["assignments"][args.id]
                rex = connect(record, args.rex, args.server)
                if assignment["state"] == "launching":
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
