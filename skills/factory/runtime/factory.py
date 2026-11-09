"""Task admission, explicit native Rex launches and mechanical integration."""
import argparse
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys

sys.dont_write_bytecode = True  # Installed copies stay free of generated files.
import tempfile
import time
from typing import Any
import uuid

if __name__ == "__main__":
    sys.modules.setdefault("factory", sys.modules[__name__])

ROOT = Path(__file__).resolve().parent
# The runtime ships inside the factory skill; its role catalog sits beside it.
ROLES_PATH = ROOT.parent / "references" / "roles.json"
GIT_EXECUTION: ContextVar[Any] = ContextVar("factory_git_execution", default=None)
MAX_NATIVE_SECONDS = 86400
RESULT_OUTCOMES = {"completed", "approved", "findings", "blocked"}
# Process states that never change again; only these assignment tabs are tidied.
TERMINAL_STATES = ("exited", "failed", "lost")
REPOSITORY_MARKER = "software-factory-repository.json"
GIT_IDENTITY_VARIABLES = {"GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE",
                          "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_COMMITTER_DATE"}
GIT_ROUTING_VARIABLES = {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_CONFIG",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
    "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE", "GIT_REPLACE_REF_BASE", "GIT_NO_REPLACE_OBJECTS",
    "GIT_PREFIX", "GIT_INTERNAL_SUPER_PREFIX", "GIT_IMPLICIT_WORK_TREE",
    "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_QUARANTINE_PATH"}
GIT_SOURCE_CONFIG_KEYS = {"core.worktree", "core.bare", "core.repositoryformatversion",
    "extensions.objectformat", "extensions.refstorage", "extensions.worktreeconfig"}


class FactoryError(Exception):
    pass


class ControlConflict(FactoryError):
    pass


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key")
        result[key] = value
    return result


def reject_json_constant(value):
    raise ValueError("Nonstandard JSON numeric constant")


def finite_json_float(value):
    number = float(value)
    if not -sys.float_info.max <= number <= sys.float_info.max:
        raise ValueError("JSON number exceeds the supported finite range")
    return number


def finite_json_integer(value):
    number = int(value)
    if not -sys.float_info.max <= number <= sys.float_info.max:
        raise ValueError("JSON number exceeds the supported finite range")
    return number


def decode_json(value, *, object_pairs_hook=unique_object):
    try:
        return json.loads(value, object_pairs_hook=object_pairs_hook,
                          parse_constant=reject_json_constant, parse_float=finite_json_float,
                          parse_int=finite_json_integer)
    except RecursionError:
        raise ValueError("JSON nesting exceeds the supported depth; reduce nesting before retrying") from None


class Rex:
    def __init__(self, binary, server):
        self.binary, self.server = str(binary), server
        self.instance_id: str | None = None  # Server instance observed by connect(); fetched on demand.

    def call(self, *args, raw=False) -> Any:
        try:
            result = subprocess.run(
                [self.binary, "--autostart=false", "-S", self.server, *args],
                capture_output=True, text=True, timeout=20, check=False)
        except UnicodeDecodeError:
            raise FactoryError("Cannot decode Rex response; inspect the pinned binary/server API compatibility") from None
        except (OSError, subprocess.TimeoutExpired):
            raise FactoryError("Cannot reach the pinned Rex binary/server; restore its availability and reconcile the recorded session before retrying effects") from None
        if result.returncode:
            raise FactoryError(result.stderr.strip() or "Rex command failed")
        if raw:
            return result.stdout
        try:
            return decode_json(result.stdout)
        except ValueError:
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
def execution_lock(directory):
    """Serialize controller/CLI launch decisions within one factory instance."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / ".execution.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FactoryError("Another launcher or ready-work controller owns execution; wait for it to finish before starting more work") from None
        yield
    finally:
        os.close(fd)


@contextmanager
def task_lock(directory, task, *, wait=False):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", task):
        raise FactoryError("Task ID must be a lowercase slug, at most 64 characters")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory / (task + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise FactoryError("Another command is working on this task; retry later") from exc
        yield directory / (task + ".json")
    finally:
        os.close(fd)


def default_state_dir():
    """One per-user factory state, shared by every project that installs the factory."""
    base = os.environ.get("XDG_STATE_HOME")
    return (Path(base) if base and Path(base).is_absolute() else Path.home() / ".local" / "state") / "software-factory"


def runtime_home():
    """The one per-user runtime that every project's installed copy runs from."""
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base and Path(base).is_absolute() else Path.home() / ".local" / "share") / "software-factory"


def version_key(text):
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", text):
        raise FactoryError(f"Unsupported factory version {text!r}; restore the installed factory runtime")
    return tuple(int(part) for part in text.split("."))


def relocate(entry, argv, *, root=None, home=None, execv=os.execv):
    """Run an ACR-installed project copy from the one per-user runtime.

    Tasks are shared per user and freeze absolute worker paths, so every project must
    run the same installed files. The per-user copy is upgraded in place to the newest
    version any project brings and never downgraded. A source checkout runs in place.
    """
    skill = (root or ROOT).parent
    if not (skill.name.startswith("acr__") and skill.parent.name == "skills"):
        return
    home = home or runtime_home()
    installed = home / "skill"
    try:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = os.open(home / ".install.lock", os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        raise FactoryError(f"Cannot prepare the per-user factory runtime in {home}; restore access/space and retry") from None
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        offered = version_key((skill / "runtime" / "VERSION").read_text().strip())
        current = installed / "runtime" / "VERSION"
        if not current.exists() or version_key(current.read_text().strip()) < offered:
            staging = Path(tempfile.mkdtemp(prefix=".installing-", dir=home))
            try:
                # Generated bytecode is never part of an installed runtime.
                shutil.copytree(skill, staging / "skill", ignore=shutil.ignore_patterns("__pycache__"))
                if installed.exists():
                    os.rename(installed, staging / "retired")
                os.rename(staging / "skill", installed)
            finally:
                shutil.rmtree(staging)
    except OSError:
        raise FactoryError(f"Cannot install the per-user factory runtime in {home}; restore access/space and retry") from None
    finally:
        os.close(lock)
    execv(sys.executable, [sys.executable, "-B", str(installed / "runtime" / entry), *argv])


def new_session_label(task):
    return "factory-" + task + "-" + uuid.uuid4().hex[:8]


def new_task(task) -> dict:
    return dict(schema_version=10, task=task, admission=None, inputs=None, hold=None, control_events=[], allowance_events=[], provisioning=None, rex=None,
                session_label=new_session_label(task), session_id=None, assignments={})


def read_record(path, *, private=False, max_bytes=None):
    """Read a regular gate record without following links or blocking on FIFOs."""
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise ValueError("Record byte limit must be a nonnegative integer")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r" if max_bytes is None else "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (private and (
                info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077)):
            raise OSError("Gate record must be a regular file with its required ownership and permissions")
        if max_bytes is None:
            return stream.read()
        content = stream.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise OSError("Record exceeds its byte limit; inspect the original without rewriting it")
        return content.decode("utf-8")


def integration_request(request):
    """Recognize only the factory-owned mechanical adapter; imports stay lazy."""
    return (request.get("role") == "integration" and request.get("command", [])[:5]
            == [sys.executable, "-E", "-s", str(ROOT / "integration.py"), "--intent"])


def load_task(path) -> dict:
    """Factory owns migration of its gate store; unknown formats cannot launch."""
    try:
        record = json.loads(read_record(path), object_pairs_hook=unique_object)
    except FileNotFoundError:
        raise FactoryError(f"Unknown task {path.stem!r}; correct its ID or create it with admit/start") from None
    except (ValueError, UnicodeDecodeError):
        raise FactoryError(f"Cannot decode task {path.stem!r}; restore the original task record before continuing") from None
    except OSError:
        raise FactoryError(f"Cannot read task {path.stem!r}; restore access to its original state file") from None
    if not isinstance(record, dict):
        raise FactoryError("Invalid task record; restore the original state before continuing")
    version = record.get("schema_version", 0)
    if type(version) is not int or version not in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10):
        raise FactoryError("Unsupported task schema; update the factory before continuing")
    if (version < 6 or version == 7) and "provisioning" in record:
        raise FactoryError("Legacy task has newer fields; restore its schema version before continuing")
    if version < 7 and "allowance_events" in record:
        raise FactoryError("Legacy task has newer fields; restore its schema version before continuing")
    if version < 5 and "control_events" in record:
        raise FactoryError("Legacy task has newer fields; restore its schema version before continuing")
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
    if record["hold"] is not None and (not isinstance(record["hold"], str) or not record["hold"].strip()):
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
                or assignment.get("state") not in ("launching", "launched", "abandoned")
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
        if assignment["state"] == "launched" and ((record["session_id"] is None and assignment.get("closed") is None)
                or any(not isinstance(assignment.get(key), str) or not assignment[key]
                       for key in ("window_id", "block_id"))):
            raise FactoryError("Missing launched pane identity; restore the original task record before continuing")
        if assignment["state"] == "launching" and any(key in assignment for key in ("window_id", "block_id")):
            raise FactoryError("Pending launch has pane IDs; restore the original task record before reconciliation")
        if (assignment["state"] == "abandoned") != ("abandoned" in assignment) or (
                assignment["state"] == "abandoned" and (
                    version < 10 or any(key in assignment for key in ("window_id", "block_id", "closed"))
                    or not isinstance(assignment["abandoned"], dict) or set(assignment["abandoned"]) != {"reason", "at"}
                    or not isinstance(assignment["abandoned"]["reason"], str) or not assignment["abandoned"]["reason"].strip()
                    or type(assignment["abandoned"]["at"]) not in (int, float)
                    or not 0 <= assignment["abandoned"]["at"] <= 253402300799)):
            raise FactoryError("Invalid abandoned launch record; restore the original task record")
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
        if version < 9:
            if "native_collected" in assignment:
                raise FactoryError("Legacy assignment has newer fields; restore its schema version before continuing")
            assignment["native_collected"] = False
        if type(assignment.get("native_collected")) is not bool or (assignment["native_collected"] and assignment["result"] is None):
            raise FactoryError("Invalid native collection provenance; restore the original task record")
        if version < 10 and "rex_instance" in assignment:
            raise FactoryError("Legacy assignment has newer fields; restore its schema version before continuing")
        if "rex_instance" in assignment and (not isinstance(assignment["rex_instance"], str)
                                             or not assignment["rex_instance"].strip()):
            raise FactoryError("Invalid Rex server instance; restore the original task record")
        if version < 10 and "closed" in assignment:
            raise FactoryError("Legacy assignment has newer fields; restore its schema version before continuing")
        if "closed" in assignment and not valid_closed(assignment):
            raise FactoryError("Invalid closed tab record; restore the original task record")
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
    if version < 5:
        record.update(schema_version=5, control_events=[])
    validate_control_events(record)
    if version < 6 or version == 7:
        record["provisioning"] = None
    validate_provisioning(record)
    if version < 7:
        record["allowance_events"] = []
    allowance_limits(record)
    if version < 10:
        record["schema_version"] = 10
        try:
            save(path, record)
        except OSError:
            raise FactoryError("Cannot confirm task schema migration save; restore state-store access/space, then reload or inspect the task record before continuing") from None
    return record


def valid_closed(assignment):
    """A tidied tab keeps its final process observation and any retained output."""
    closed = assignment["closed"]
    return (assignment["state"] == "launched" and isinstance(closed, dict)
            and set(closed) == {"state", "exit", "reason", "session_id", "capture", "at"}
            and closed["state"] in TERMINAL_STATES
            and (closed["exit"] is None or (isinstance(closed["exit"], dict)
                                            and type(closed["exit"].get("exit_code")) is int))
            and (closed["exit"] is not None) == (closed["state"] != "lost")
            and (closed["reason"] is None if closed["state"] != "lost"
                 else isinstance(closed["reason"], str) and bool(closed["reason"].strip()))
            and isinstance(closed["session_id"], str) and bool(closed["session_id"])
            and (closed["capture"] is None or (isinstance(closed["capture"], str)
                                               and Path(closed["capture"]).is_absolute()))
            and type(closed["at"]) in (int, float) and 0 <= closed["at"] <= 253402300799)


def validate_control_events(record):
    events = record.get("control_events")
    if not isinstance(events, list):
        raise FactoryError("Missing task control history; restore the original state before continuing")
    seen, previous = set(), None
    for index, event in enumerate(events):
        if (not isinstance(event, dict) or set(event) != {"id", "expected_revision", "previous_hold", "hold", "at"}
                or not isinstance(event["id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", event["id"])
                or event["id"] in seen
                or (event["expected_revision"] is not None and (
                    type(event["expected_revision"]) is not int or event["expected_revision"] != index))
                or any(value is not None and (not isinstance(value, str) or not value.strip())
                       for value in (event["previous_hold"], event["hold"]))
                or type(event["at"]) not in (int, float) or not 0 <= event["at"] <= 253402300799
                or (index > 0 and event["previous_hold"] != previous)):
            raise FactoryError("Invalid task control history; restore the original state before continuing")
        seen.add(event["id"])
        previous = event["hold"]
    if events and previous != record["hold"]:
        raise FactoryError("Task hold differs from its control history; restore the original state before continuing")


def valid_inputs(value):
    return isinstance(value, dict) and all(isinstance(value.get(key), str) and value[key].strip()
                                         for key in ("candidate", "requirements")) and bool(
        re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value["candidate"]))


def allowance_limits(record):
    """Validate the operator's adjustment chain and derive effective limits."""
    limits = None if record["admission"] is None else dict(record["admission"]["limits"])
    events = record.get("allowance_events")
    if not isinstance(events, list) or (limits is None and events):
        raise FactoryError("Invalid allowance history; restore the original task record before continuing")
    seen = set()
    for event in events:
        if (not isinstance(event, dict) or set(event) != {"id", "purpose", "previous_limit", "additional", "reason", "at"}
                or not isinstance(event["id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", event["id"])
                or event["id"] in seen or not isinstance(event["purpose"], str)
                or limits is None or event["purpose"] not in limits
                or type(event["previous_limit"]) is not int or event["previous_limit"] != limits[event["purpose"]]
                or type(event["additional"]) is not int or event["additional"] <= 0
                or not isinstance(event["reason"], str) or not event["reason"].strip()
                or type(event["at"]) not in (int, float) or not 0 <= event["at"] <= 253402300799):
            raise FactoryError("Invalid allowance history; restore its original ordered adjustments before continuing")
        seen.add(event["id"])
        limits[event["purpose"]] += event["additional"]
    return limits


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


def git_environment():
    return {key: value for key, value in os.environ.items()
            if not key.startswith("GIT_") or key in GIT_IDENTITY_VARIABLES}


def git_binary():
    binary = shutil.which("git", path=os.defpath)
    if binary is None:
        raise FactoryError("Cannot find trusted Git; install Git on the operating system's default executable path")
    return binary


def command_environment():
    environment = {key: value for key, value in os.environ.items() if key not in GIT_ROUTING_VARIABLES}
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    if any(key.startswith("GIT_CONFIG") for key in environment):
        try:
            observed = subprocess.run([git_binary(), "--no-replace-objects", "-C", git_worktree("."),
                "config", "--null", "--name-only", "--list", "--show-scope", "--includes"],
                env=environment, capture_output=True, text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
            raise FactoryError("Cannot verify inherited Git configuration; repair GIT_CONFIG_* settings and Git access before launching") from None
        fields = observed.stdout.split("\0")
        if observed.returncode or fields[-1] != "" or len(fields) % 2 != 1:
            raise FactoryError("Cannot parse inherited Git configuration; repair GIT_CONFIG_* settings and inspect Git compatibility before launching")
        for scope, key in zip(fields[0:-1:2], fields[1:-1:2]):
            inherited = (scope == "command" or scope == "global" and "GIT_CONFIG_GLOBAL" in environment
                         or scope == "system" and "GIT_CONFIG_SYSTEM" in environment)
            if inherited and key.lower() in GIT_SOURCE_CONFIG_KEYS:
                raise FactoryError("Inherited Git configuration contains source-routing options; remove them before launching and keep authentication settings")
    return environment


def git_worktree(repository):
    current = Path(repository).resolve()
    for directory in (current, *current.parents):
        if (directory / ".git").exists():
            return str(directory)
    raise FactoryError("Working directory is not a Git checkout; supply an isolated worktree of the admitted repository")


@contextmanager
def git_execution(prefix, environment):
    token = GIT_EXECUTION.set((prefix, environment))
    try:
        yield
    finally:
        GIT_EXECUTION.reset(token)


def git_run(repository, *args, environment=None):
    try:
        worktree = git_worktree(repository)
        scope = GIT_EXECUTION.get()
        prefix = [] if scope is None else scope[0]()
        if scope is not None:
            environment = scope[1]
        result = subprocess.run([*prefix, git_binary(), "--no-replace-objects", "-C", worktree,
                              "--work-tree=" + worktree, *args], capture_output=True,
                              timeout=10, check=False,
                              env=git_environment() if environment is None else environment)
        if scope is not None and result.returncode == 125:
            raise FactoryError("Native sandbox rewrote protected Git environment; inspect its environment settings before another attempt")
        return subprocess.CompletedProcess(result.args, result.returncode,
            result.stdout.decode("utf-8"), result.stderr.decode("utf-8"))
    except UnicodeDecodeError:
        raise FactoryError("Cannot decode Git output; inspect Git/checkout filename encoding and repair it before retrying") from None
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
    return Path(result.stdout.removesuffix("\n")).resolve()


def git_dir(repository):
    result = git_run(repository, "rev-parse", "--absolute-git-dir")
    if result.returncode:
        raise FactoryError("Cannot identify assignment worktree; restore its Git directory before launching")
    return Path(result.stdout.removesuffix("\n")).resolve()


def repository_identity(repository, *, create=False):
    common = git_common_dir(repository)
    marker = common / REPOSITORY_MARKER
    try:
        if create and not os.path.lexists(marker):
            try:
                save(marker, dict(schema_version=1, repository_id=uuid.uuid4().hex), exclusive=True)
            except FileExistsError:
                pass  # A concurrent admission published the shared marker; read it below.
        value = json.loads(read_record(marker, private=True), object_pairs_hook=unique_object)
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
        observed = pinned_commit(admission["repository"], admission["initial_candidate"],
                                 admission["repository_identity"])
    except FactoryError as exc:
        raise FactoryError("Cannot verify the admitted repository pin; inspect/restore the original checkout. " + str(exc)) from exc
    if observed != admission["initial_candidate"]:
        raise FactoryError("Admitted repository pin differs; restore the original checkout before continuing")


def pinned_commit(repository, revision, identity):
    if repository_identity(repository) != identity:
        raise FactoryError("Repository identity differs; restore the original Git-local marker and common directory")
    candidate = git_commit(repository, revision)
    if repository_identity(repository) != identity:
        raise FactoryError("Repository identity differs; restore the original Git-local marker and common directory")
    return candidate


def check_checkout(repository, cwd, candidate, *, expected_identity=None, expected_git_dir=None):
    if expected_identity is not None and (not valid_repository_identity(expected_identity)
            or repository_identity(repository) != expected_identity):
        raise FactoryError("Assignment repository identity differs; restore the original Git-local marker and common directory")
    if expected_git_dir is not None and git_dir(cwd) != Path(expected_git_dir):
        raise FactoryError("Assignment worktree differs; restore its original Git directory and use the assigned checkout")
    if git_commit(repository, candidate) != candidate:
        raise FactoryError("Stored candidate is not an exact commit; select task inputs before launching")
    if git_common_dir(repository) != git_common_dir(cwd) or git_commit(cwd, "HEAD") != candidate:
        raise FactoryError("Assignment checkout differs from the selected candidate; use its worktree at that commit")
    monitor = git_run(cwd, "config", "--get", "core.fsmonitor")
    if monitor.returncode not in (0, 1):
        raise FactoryError("Cannot inspect fsmonitor configuration; restore Git config access before launching")
    if monitor.returncode == 0 and monitor.stdout.strip().lower() not in {"false", "no", "off", "0"}:
        raise FactoryError("Assignment uses fsmonitor; disable core.fsmonitor and clear fsmonitor-valid flags before launching")
    flags = git_run(cwd, "ls-files", "--stage", "-v", "-f", "-z")
    if flags.returncode:
        raise FactoryError("Cannot inspect assignment index; restore Git index access before launching")
    if any(entry and (entry[0].islower() or entry[0] == "S") for entry in flags.stdout.split("\0")):
        raise FactoryError("Assignment index hides tracked inputs; clear assume-unchanged/skip-worktree/fsmonitor-valid flags or use a clean full checkout")
    if any(entry[2:].startswith("160000 ") for entry in flags.stdout.split("\0") if entry):
        raise FactoryError("Assignment contains submodules; this runtime requires a checkout without submodules before launching")
    # Compare only the index here: worktree status can execute clean filters.
    # Raw source validation below checks files, modes and untracked directories.
    changed = git_run(cwd, "-c", "core.fsmonitor=false", "diff-index", "--cached", "--quiet",
                      "--no-ext-diff", "--no-textconv", "--ignore-submodules=none", candidate, "--")
    if changed.returncode not in (0, 1):
        raise FactoryError("Cannot inspect assignment checkout; repair Git access before launching")
    if changed.returncode:
        raise FactoryError("Assignment checkout has uncommitted inputs (including ignored files); use a clean worktree and keep runtime state/output outside it")
    check_tracked_bytes(cwd, candidate)
    configured = git_run(cwd, "config", "--path", "--get", "core.worktree")
    if configured.returncode not in (0, 1):
        raise FactoryError("Cannot inspect worktree configuration; restore Git config access before launching")
    if configured.returncode == 0:
        gitdir = git_run(cwd, "rev-parse", "--absolute-git-dir")
        if gitdir.returncode or (Path(gitdir.stdout.removesuffix("\n")) / configured.stdout.removesuffix("\n")).resolve() != Path(git_worktree(cwd)):
            raise FactoryError("Git configuration redirects the command's worktree; restore core.worktree or use a clean isolated worktree")
    if expected_identity is not None and (repository_identity(repository) != expected_identity
                                          or repository_identity(cwd) != expected_identity):
        raise FactoryError("Assignment repository identity differs; restore the original Git-local marker and common directory")
    if expected_git_dir is not None and git_dir(cwd) != Path(expected_git_dir):
        raise FactoryError("Assignment worktree differs; restore its original Git directory and use the assigned checkout")


def independent_source(path):
    """Copy a shared regular source file without changing any of its aliases."""
    with tempfile.TemporaryDirectory(prefix=".factory-source-", dir=path.parent) as temporary:
        independent = Path(temporary) / "source"
        shutil.copy2(path, independent, follow_symlinks=False)
        os.replace(independent, path)


def check_tracked_bytes(cwd, candidate):
    """Check source bytes/modes against the commit without trusting index stat hints."""
    tree = git_run(cwd, "ls-tree", "-r", "-z", "--full-tree", candidate)
    if tree.returncode:
        raise FactoryError("Cannot read candidate tree; restore Git object access before launching")
    root = Path(git_worktree(cwd))
    try:
        allowed, identities = set(), set()
        for entry in tree.stdout.split("\0"):
            if not entry:
                continue
            metadata, name = entry.split("\t", 1)
            mode, kind, expected = metadata.split()
            relative = Path(name)
            allowed.add(relative)
            allowed.update(parent for parent in relative.parents if parent != Path("."))
            path = root / name
            info = path.lstat()
            if kind != "blob" or any(parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents):
                raise FactoryError("Assignment source differs from the candidate tree; restore a clean full checkout before launching")
            if stat.S_ISLNK(info.st_mode):
                raise FactoryError("Assignment source contains symlinks; use a full checkout of independent regular files before launching")
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink > 1:
                    independent_source(path)
                    info = path.lstat()
                identity = (info.st_dev, info.st_ino)
                if identity in identities:
                    raise FactoryError("Candidate paths alias the same source file; use filenames distinct on this filesystem before launching")
                identities.add(identity)
                actual_mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
                data = path.read_bytes()
            else:
                raise FactoryError("Assignment source has an unsupported file type; restore a clean full checkout before launching")
            algorithm = "sha1" if len(expected) == 40 else "sha256"
            observed = hashlib.new(algorithm, b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            if actual_mode != mode or observed != expected:
                raise FactoryError("Assignment source bytes or modes differ from the candidate; restore a clean full checkout (without working-tree filters) before launching")
        pending = [root]
        while pending:
            for path in pending.pop().iterdir():
                relative = path.relative_to(root)
                if relative == Path(".git"):
                    continue
                if relative not in allowed:
                    raise FactoryError("Assignment checkout has uncommitted inputs (including empty directories); use a clean full worktree before launching")
                if path.is_dir() and not path.is_symlink():
                    pending.append(path)
    except OSError:
        raise FactoryError("Cannot read assignment source; restore candidate files and access before launching") from None


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
    record = load_task(path) if os.path.lexists(path) else new_task(task)
    if record["admission"] is not None:
        if any(record["admission"].get(key) != value for key, value in admission.items()):
            raise FactoryError("Task already has different admission inputs; use a new task ID")
        check_repository(record["admission"])
        return record
    if record["assignments"]:
        raise FactoryError("Existing launches have unclassified attempts; admit a new task ID")
    admission["repository_identity"] = repository_identity(repository, create=True)
    admission["initial_candidate"] = pinned_commit(repository, "HEAD", admission["repository_identity"])
    record["admission"] = admission
    record["inputs"] = dict(candidate=admission["initial_candidate"],
                            requirements=admission["initial_candidate"])
    save(path, record)
    return record


def task_view(record):
    """An offline snapshot, never a live process or task-acceptance verdict."""
    allowances = None
    limits = allowance_limits(record)
    if limits is not None:
        allowances = {}
        for purpose, limit in limits.items():
            used = sum(item["request"].get("purpose") == purpose
                       for item in record["assignments"].values())
            allowances[purpose] = dict(limit=limit, used=used, remaining=limit - used)
    assignments = {key: {**item, "result_current": result_current(record, item["result"])}
                   for key, item in record["assignments"].items()}
    item = record["provisioning"]
    provisioning = None if item is None else {**item, "inputs_current": item["request"]["inputs"] == record["inputs"]}
    return {**record, "allowances": allowances, "assignments": assignments, "provisioning": provisioning}


def result_current(record, result):
    return result is not None and record["inputs"] == dict(
        candidate=result["candidate"], requirements=result["inputs"]["requirements"])


def validate_provisioning(record):
    item = record.get("provisioning")
    if "provisioning" not in record:
        raise FactoryError("Missing task provisioning field; restore the original record")
    if item is None:
        return
    if (record["admission"] is None or not isinstance(item, dict)
            or set(item) != {"id", "request", "owner_token", "state", "git_dir"}
            or not isinstance(item["id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", item["id"])
            or not isinstance(item["owner_token"], str) or not re.fullmatch(r"[0-9a-f]{32}", item["owner_token"])
            or item["state"] not in ("provisioning", "provisioned")):
        raise FactoryError("Invalid task provisioning intent; restore the original record")
    request = item["request"]
    if (not isinstance(request, dict) or set(request) != {"path", "branch", "inputs"}
            or not isinstance(request["path"], str) or not Path(request["path"]).is_absolute()
            or str(Path(request["path"])) != request["path"] or ".." in Path(request["path"]).parts
            or request["branch"] != f'factory/{record["task"]}/{item["id"]}'
            or not valid_inputs(request["inputs"])
            or (item["git_dir"] is not None if item["state"] == "provisioning" else
                not isinstance(item["git_dir"], str) or not Path(item["git_dir"]).is_absolute())):
        raise FactoryError("Invalid task provisioning request; restore the original record")


def provisioning_git(repository, *args, output=None):
    """Metadata/index operations and raw blobs only; never checkout callbacks."""
    environment = {**git_environment(), "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run([git_binary(), "--no-replace-objects", "-C", str(repository),
            "--work-tree=" + str(repository), "-c", "core.hooksPath=" + os.devnull,
            "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", *args],
            env=environment, stdout=subprocess.PIPE if output is None else output,
            stderr=subprocess.PIPE, timeout=30, check=False)
        diagnostic = result.stderr.decode("utf-8")
        output_text = "" if result.stdout is None else result.stdout.decode("utf-8")
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError) as exc:
        raise FactoryError("Provisioning Git outcome uncertain; inspect the retained intent and resources before continuing") from exc
    if result.returncode:
        raise FactoryError("Provisioning Git refused; inspect the retained intent and resources: " + diagnostic.strip())
    return output_text


def provisioning_source(repository, candidate, destination_parent):
    """Reject unsupported configuration and trees before any worktree mutation."""
    config = git_run(repository, "config", "--null", "--list")
    if config.returncode:
        raise FactoryError("Cannot inspect provisioning configuration; restore Git configuration access before provisioning")
    for field in config.stdout.split("\0"):
        key, _, value = field.partition("\n")
        key = key.lower()
        if (key in {"core.worktree", "core.alternaterefscommand", "extensions.partialclone"}
                or key in {"core.fsmonitor", "core.bare", "core.sparsecheckout", "extensions.worktreeconfig"}
                and value.lower() not in {"false", "no", "off", "0"}
                or key.startswith("remote.") and key.endswith(".promisor")
                and value.lower() not in {"false", "no", "off", "0"}):
            raise FactoryError("Unsupported provisioning configuration: " + key + "; remove this setting or choose a supported repository before provisioning")
    if os.path.lexists(git_common_dir(repository) / "objects/info/alternates"):
        raise FactoryError("Provisioning requires local objects without alternates; choose a repository with independent local objects before provisioning")
    try:
        tree = provisioning_git(repository, "ls-tree", "-r", "-z", "--full-tree", candidate)
    except FactoryError:
        raise FactoryError("Cannot inspect candidate tree before provisioning; restore local Git object access and select a candidate with supported UTF-8 filenames") from None
    entries = []
    for entry in tree.split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, kind, oid = metadata.split()
        relative = Path(name)
        if (kind != "blob" or mode not in {"100644", "100755"} or relative.is_absolute()
                or relative.as_posix() != name
                or any(part.lower() in {".git", ".."} for part in relative.parts)):
            raise FactoryError("Provisioning supports only regular-file trees; select a candidate without symlinks, submodules or unsupported paths")
        entries.append((name, mode, oid))
    # Probe only names, on the actual destination filesystem, before saving intent.
    # No normalization table can establish that filesystem's name equivalence.
    try:
        with tempfile.TemporaryDirectory(prefix=".factory-names-", dir=destination_parent) as temporary:
            root, directories = Path(temporary), {}
            for name, _, _ in entries:
                with provisioning_file(root, name):
                    pass
                for parent in Path(name).parents:
                    if parent == Path("."):
                        continue
                    info = (root / parent).stat()
                    identity = (info.st_dev, info.st_ino)
                    previous = directories.setdefault(identity, parent)
                    if previous != parent:
                        raise FactoryError("Candidate directory names collide on the destination filesystem; rename them or choose a compatible filesystem")
    except (FileExistsError, NotADirectoryError, IsADirectoryError):
        raise FactoryError("Candidate filenames collide on the destination filesystem; rename them or choose a compatible filesystem") from None
    except OSError:
        raise FactoryError("Cannot check destination filenames; restore temporary storage access/space before provisioning") from None
    return entries


def provisioning_owned(record, item):
    """A saved unique Git lock marker binds recovery to this effect, not a lookalike."""
    try:
        destination = Path(item["request"]["path"])
        if destination.is_symlink() or not destination.is_dir():
            raise FactoryError("Provisioning outcome uncertain: owned worktree is missing or replaced; inspect without retrying")
        read_record(destination / ".git")
        directory = git_dir(destination)
        common = Path(record["admission"]["repository_identity"]["common_dir"])
        marker = f'factory-{record["task"]}-{item["id"]}-{item["owner_token"]}'
        if (directory.parent != common / "worktrees"
                or directory.is_symlink()
                or read_record(directory / "locked").strip() != marker
                or read_record(directory / "gitdir").strip() != str(destination / ".git")
                or repository_identity(destination) != record["admission"]["repository_identity"]
                or item["state"] == "provisioning" and provisioning_git(destination, "symbolic-ref", "HEAD").strip() != "refs/heads/" + item["request"]["branch"]
                or item["git_dir"] is not None and item["git_dir"] != str(directory)):
            raise FactoryError("Provisioning ownership differs; inspect conflicting resources without resetting or deleting them")
        return directory
    except (OSError, UnicodeDecodeError):
        raise FactoryError("Cannot read provisioning ownership metadata; restore access to the original metadata and inspect existing resources without retrying, resetting or deleting them") from None


@contextmanager
def provisioning_file(root, name):
    """Create only new files beneath the owned directory, without following links."""
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = Path(name).parts
        for part in parts[:-1]:
            try:
                os.mkdir(part, dir_fd=directory)
                os.fsync(directory)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as stream:
            yield stream
        os.fsync(directory)
    finally:
        os.close(directory)


def provision_worktree(path, *, worktree_id, destination):
    """Caller holds the owner's execution/task locks, as for explicit launches."""
    record = load_task(path)
    if record["admission"] is None:
        raise FactoryError("Admit a task before provisioning a writer worktree")
    if not isinstance(worktree_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", worktree_id):
        raise FactoryError("Worktree ID must be a lowercase slug, at most 64 characters")
    supplied = Path(destination)
    if not supplied.is_absolute() or ".." in supplied.parts:
        raise FactoryError("Supply an absolute worktree path without parent traversal")
    try:
        parent = supplied.parent.resolve(strict=True)
        is_directory = parent.is_dir()
    except OSError:
        raise FactoryError("Create or restore access to the destination parent directory before provisioning") from None
    if not is_directory:
        raise FactoryError("Choose an existing directory as the destination parent before provisioning")
    destination = parent / supplied.name
    request: dict = dict(path=str(destination), branch=f'factory/{record["task"]}/{worktree_id}',
                         inputs=dict(record["inputs"]))
    check_repository(record["admission"])
    existing = record["provisioning"]
    if existing is not None:
        if existing["id"] != worktree_id or existing["request"] != request:
            raise FactoryError("Task already has a different provisioning request; inspect its original intent")
        directory = provisioning_owned(record, existing)
        if existing["state"] == "provisioned":
            return record  # Historical completion: writers may now have edited this checkout.
        # Reconciliation is read-only, including while held. Never repair shared files.
        try:
            for root, _, files in os.walk(destination, followlinks=False):
                for name in files:
                    if (Path(root) / name).lstat().st_nlink != 1:
                        raise FactoryError("Provisioning outcome uncertain: shared files require inspection")
            check_checkout(record["admission"]["repository"], str(destination), request["inputs"]["candidate"],
                           expected_identity=record["admission"]["repository_identity"], expected_git_dir=directory)
        except OSError:
            raise FactoryError("Cannot inspect retained provisioning resources; restore filesystem access and inspect the existing intent/worktree without retrying, resetting or deleting them") from None
    else:
        if record["hold"] is not None:
            raise FactoryError("Task is paused; resume before provisioning")
        if any(item["state"] == "launching" for item in record["assignments"].values()):
            raise FactoryError("Reconcile the pending launch before provisioning")
        for parent in (destination.parent, *destination.parent.parents):
            if (os.path.lexists(parent / ".git") or
                    (parent / "HEAD").exists() and (parent / "objects").is_dir()):
                raise FactoryError("Provision outside existing checkouts and Git directories")
        if (destination.is_relative_to(path.parent.resolve())
                or path.resolve().is_relative_to(destination)):
            raise FactoryError("Provision outside the task owner store")
        if os.path.lexists(destination):
            raise FactoryError("Worktree path already exists; never reuse or delete conflicting resources")
        repository = record["admission"]["repository"]
        entries = provisioning_source(repository, request["inputs"]["candidate"], destination.parent)
        branches = git_run(repository, "for-each-ref", "--format=%(refname)", "refs/heads")
        if branches.returncode:
            raise FactoryError("Cannot inspect the local branch namespace; restore Git access before provisioning")
        wanted = ("refs/heads/" + request["branch"]).casefold()
        if any(ref == wanted or ref.startswith(wanted + "/") or wanted.startswith(ref + "/")
               for ref in (name.casefold() for name in branches.stdout.splitlines())):
            raise FactoryError("Worktree branch exists or has a namespace conflict; choose an unused task/worktree name or explicitly resolve the conflicting ref")
        existing = dict(id=worktree_id, request=request, owner_token=uuid.uuid4().hex,
                        state="provisioning", git_dir=None)
        record["provisioning"] = existing
        try:
            save(path, record)  # Durable immutable request and before-effect intent.
        except OSError:
            raise FactoryError("Cannot confirm provisioning intent save; restore state-store access/space and reconcile the saved task record before continuing; no Git worktree creation was started") from None
        marker = f'factory-{record["task"]}-{worktree_id}-{existing["owner_token"]}'
        provisioning_git(repository, "worktree", "add", "--no-checkout", "--lock", "--reason", marker,
                         "-b", request["branch"], str(destination), request["inputs"]["candidate"])
        directory = provisioning_owned(record, existing)
        # --no-checkout avoids hooks and smudge filters; read-tree writes only this index.
        provisioning_git(destination, "read-tree", request["inputs"]["candidate"])
        try:
            for name, mode, oid in entries:
                with provisioning_file(destination, name) as stream:
                    provisioning_git(repository, "cat-file", "blob", oid, output=stream)
                    stream.flush()
                    os.fchmod(stream.fileno(), 0o755 if mode == "100755" else 0o644)
                    os.fsync(stream.fileno())
            check_checkout(repository, str(destination), request["inputs"]["candidate"],
                           expected_identity=record["admission"]["repository_identity"], expected_git_dir=directory)
        except OSError:
            raise FactoryError("Provisioning resource writes failed; restore filesystem access/space and inspect the retained intent/worktree without retrying, resetting or deleting them") from None
    existing.update(state="provisioned", git_dir=str(directory))
    try:
        save(path, record)
    except OSError:
        raise FactoryError("Cannot confirm provisioning completion save; restore state-store access/space and reconcile this same saved intent and existing worktree before continuing") from None
    return record


def set_inputs(path, *, candidate, requirements):
    record = load_task(path)
    if record["admission"] is None or not requirements.strip():
        raise FactoryError("Admit a task and supply a nonempty requirements revision first")
    check_repository(record["admission"])
    record["inputs"] = dict(candidate=pinned_commit(record["admission"]["repository"], candidate,
                                                   record["admission"]["repository_identity"]),
                            requirements=requirements)
    save(path, record)
    return record


def record_result(path, *, assignment_id, candidate, requirements, outcome, artifacts):
    record = load_task(path)
    if assignment_id not in record["assignments"]:
        raise FactoryError(f"Unknown assignment {assignment_id!r}; use show --task {record['task']} to find its ID")
    assignment = record["assignments"][assignment_id]
    if integration_request(assignment["request"]):
        raise FactoryError("Mechanical delivery results belong to factory integrate, not manual reports")
    inputs = assignment["request"].get("inputs")
    if not valid_inputs(inputs):
        raise FactoryError("Assignment predates input binding; launch a new explicit assignment ID")
    if assignment["state"] != "launched":
        raise FactoryError("Reconcile the pending launch with status before recording its result")
    check_repository(record["admission"])
    reported = dict(candidate=pinned_commit(record["admission"]["repository"], candidate,
                                            record["admission"]["repository_identity"]),
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


def hold_task(path, reason, *, request_id=None, expected_revision=None, clock=time.time):
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        raise FactoryError("Supply a nonempty pause reason")
    if request_id is not None and (not isinstance(request_id, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", request_id)):
        raise FactoryError("Supply a lowercase request ID of at most 64 characters")
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
        raise FactoryError("Supply a nonnegative task control revision")
    record = load_task(path)
    events = record["control_events"]
    previous = next((event for event in events if event["id"] == request_id), None)
    if previous is not None:
        if previous["hold"] != reason or previous["expected_revision"] != expected_revision:
            raise ControlConflict("Control request ID already has different inputs; use a new request ID")
        return record  # A replay cannot overwrite a later operator decision.
    if expected_revision is not None and len(events) != expected_revision:
        raise ControlConflict("Task controls changed; refresh the task before submitting a new decision")
    occurred = clock()
    if type(occurred) not in (int, float) or not 0 <= occurred <= 253402300799:
        raise FactoryError("Cannot timestamp the control request; restore the system clock before submitting")
    events.append(dict(id=request_id or uuid.uuid4().hex, expected_revision=expected_revision,
                       previous_hold=record["hold"], hold=reason, at=occurred))
    record["hold"] = reason
    save(path, record)
    return record


def extend_allowance(path, *, purpose, additional, expected_limit, request_id, reason, clock=time.time):
    """Record one explicit operator addition without resetting past attempts."""
    if (not isinstance(purpose, str) or purpose not in {"correction", "investigation"} or type(additional) is not int or additional <= 0
            or type(expected_limit) is not int or expected_limit < 0
            or not isinstance(request_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", request_id)
            or not isinstance(reason, str) or not reason.strip()):
        raise FactoryError("Supply a correction/investigation purpose, positive --additional, nonnegative --expected-limit, request ID and approval reason")
    record = load_task(path)
    limits = allowance_limits(record)
    if limits is None:
        raise FactoryError("Admit the task before adjusting its allowance")
    previous = next((event for event in record["allowance_events"] if event["id"] == request_id), None)
    request = dict(id=request_id, purpose=purpose, previous_limit=expected_limit, additional=additional, reason=reason)
    if previous is not None:
        if {key: previous[key] for key in request} != request:
            raise ControlConflict("Allowance request ID already has different inputs; inspect its original decision or use a new ID")
        return record
    if limits[purpose] != expected_limit:
        raise ControlConflict("Task allowance changed; refresh the task and obtain a decision for its current limit")
    occurred = clock()
    if type(occurred) not in (int, float) or not 0 <= occurred <= 253402300799:
        raise FactoryError("Cannot timestamp the allowance adjustment; restore the system clock before submitting")
    record["allowance_events"].append({**request, "at": occurred})
    save(path, record)
    return record


def factory_marker(label):
    """A window label the factory could have created: "<catalog role>-<assignment>-<8 hex>"."""
    roles = json.loads((ROLES_PATH).read_text())["roles"]
    return any(re.fullmatch(re.escape(item["id"]) + r"-[a-z0-9][a-z0-9-]{0,63}-[0-9a-f]{8}", label)
               for item in roles)


def role_spec(role_id):
    roles = json.loads((ROLES_PATH).read_text())["roles"]
    role = next((item for item in roles if item["id"] == role_id), None)
    if role is None:
        raise FactoryError("Unknown specialty: " + role_id)
    return role


def check_role_write(role_id):
    if not isinstance(role_id, str) or role_spec(role_id).get("write_allowed") is not True:
        raise FactoryError("Selected specialty does not permit writer assignments; select an authorized implementation role")


def brief(role_id, goal):
    role = role_spec(role_id)
    return (f"# Assignment: {role_id}\n\n{goal}\n\n"
            + "\n".join("- " + item for item in role["instructions"])
            + "\n\nAuthority: " + role["decision_authority"]
            + "\nReturn: " + role["result"]
            + "\nUse original evidence; do not create a separate handover or status ledger.\n"
            + "Existing repository policy and user authorization remain binding.\n")


def check_pins(record, binary=None, server=None):
    """Offline check that an explicitly supplied Rex binary/endpoint is the task's pinned one."""
    connection = record["rex"]
    if binary and str(Path(binary).resolve()) != connection["binary"]:
        raise FactoryError("Task is pinned to another Rex binary")
    if server and server != connection["server"]:
        raise FactoryError("Task is pinned to another Rex endpoint")
    return connection


def connect(record, binary=None, server=None):
    connection = check_pins(record, binary, server)
    rex = Rex(connection["binary"], connection["server"])
    who = rex_object(rex.call("whoami", "--json"), strings=("host_id",))
    if who["host_id"] != connection["host_id"]:
        raise FactoryError("Connected Rex host differs from the recorded task host")
    rex.instance_id = rex_object(who, strings=("instance_id",))["instance_id"]
    # Validate the pinned session if it still exists. Individual panes are checked
    # only when a command acts on them, so operator tab changes never block a task.
    session_view(rex, record)
    return rex


def session_view(rex, record):
    """The pinned task session, or None once it no longer exists in Rex."""
    session_id = record["session_id"]
    if session_id is None:
        return None
    try:
        return check_session(rex, record, session_id)
    except FactoryError:
        if any(item["session_id"] == session_id for item in rex_sessions(rex)):
            raise
        return None


def rex_sessions(rex):
    sessions = rex_object(rex.call("ls", "--json")).get("sessions", [])  # Rex omits empty lists.
    if not isinstance(sessions, list):
        raise FactoryError("Malformed Rex response; inspect the pinned binary/server API compatibility")
    for item in sessions:
        rex_object(item, strings=("session_id",))
    return sessions


def view_windows(view):
    windows = view.get("windows", [])  # Rex omits the list once a session has no windows.
    if not isinstance(windows, list):
        raise FactoryError("Malformed Rex response; inspect the pinned binary/server API compatibility")
    return windows


def view_blocks(view):
    return {block["block_id"] for window in view_windows(view)
            for layer in window["layers"] for block in layer["blocks"]}


def check_session(rex, record, session_id):
    """Validate session identity; operator tabs, splits, renames and closures are tolerated."""
    view = rex_object(rex.call("session", "inspect", session_id, "--json"),
                      strings=("session_id", "label"))
    if view["session_id"] != session_id:
        raise FactoryError("Rex session ID differs; inspect the pinned session before continuing")
    if view.get("label") != record["session_label"]:
        raise FactoryError("Rex session identity differs from the task; inspect the original session before continuing")
    owned = {item["window_label"] for item in record["assignments"].values()}
    labels, ids, blocks = set(), set(), set()
    for window in view_windows(view):
        rex_object(window, strings=("window_id",), lists=("layers",))
        label = window.get("label")
        if label is not None and not isinstance(label, str):
            raise FactoryError("Malformed Rex response; inspect the pinned binary/server API compatibility")
        if window["window_id"] in ids or (label in owned and label in labels):
            raise FactoryError("Duplicate Rex window identity; inspect the original session before continuing")
        if label is not None and label not in owned and factory_marker(label):
            # Operator tabs are fine; an unrecorded factory marker means lost task history.
            raise FactoryError("Rex assignment windows differ from saved history; inspect the pinned session and restore its windows or the original task history before continuing")
        labels.add(label)
        ids.add(window["window_id"])
        for layer in window["layers"]:
            rex_object(layer, lists=("blocks",))
            for block in layer["blocks"]:
                rex_object(block, strings=("block_id",))
                if block["block_id"] in blocks:
                    raise FactoryError("Duplicate Rex block identity; inspect the original session before continuing")
                blocks.add(block["block_id"])
    return view


def rex_object(value, *, strings=(), lists=()):
    if (not isinstance(value, dict)
            or any(not isinstance(value.get(key), str) or not value[key] for key in strings)
            or any(not isinstance(value.get(key), list) for key in lists)):
        raise FactoryError("Malformed Rex response; inspect the pinned binary/server API compatibility")
    return value


def assignment_pane(view, assignment):
    """The exact pane of a launch that was just created or is being reconciled."""
    windows = [item for item in view_windows(view)
               if item.get("label") == assignment["window_label"]]
    blocks = [block for window in windows for layer in window.get("layers", [])
              for block in layer.get("blocks", [])]
    if len(windows) != 1 or len(blocks) != 1:
        raise FactoryError("Launch outcome unknown; inspect Rex for one exact assignment window/block")
    window_id, block_id = windows[0].get("window_id"), blocks[0].get("block_id")
    if not isinstance(window_id, str) or not window_id or not isinstance(block_id, str) or not block_id:
        raise FactoryError("Rex pane identity is missing; inspect the original assignment before continuing")
    return window_id, block_id


ABANDON_HINT = ". If Rex rejected the launch, record that with factory.py abandon --task TASK --id ID --reason TEXT"


def abandon_launch(path, rex, *, assignment_id, reason) -> dict:
    """Record the operator's decision that a pending launch never created its pane."""
    record = load_task(path)
    assignment = record["assignments"].get(assignment_id)
    if assignment is None or assignment["state"] != "launching":
        raise FactoryError("Only a pending launch can be abandoned; inspect the assignment with status")
    if not isinstance(reason, str) or not reason.strip():
        raise FactoryError("Supply the nonempty reason the launch is known not to have happened")
    if record["session_id"] is None:
        if any(item.get("label") == record["session_label"] for item in rex_sessions(rex)):
            raise FactoryError("The pending task session exists; reconcile the launch with status instead")
    else:
        view = session_view(rex, record)
        if view is not None and any(item.get("label") == assignment["window_label"] for item in view_windows(view)):
            raise FactoryError("A pane with this launch marker exists; reconcile the launch with status instead")
    assignment.update(state="abandoned", abandoned=dict(reason=reason.strip(), at=time.time()))
    save(path, record)
    return record


def reconcile(rex, record, assignment):
    """Read back a uniquely labeled pending launch; never relaunch an unknown one."""
    session_id = record.get("session_id")
    if not session_id:
        matches = [item for item in rex_sessions(rex) if item.get("label") == record["session_label"]]
        if len(matches) != 1:
            raise FactoryError("Launch outcome unknown; no command was retried. Inspect Rex for "
                               + record["session_label"] + ABANDON_HINT)
        session_id = matches[0]["session_id"]
    view = check_session(rex, record, session_id)
    if not any(item.get("label") == assignment["window_label"] for item in view_windows(view)):
        raise FactoryError("Launch outcome unknown; no command was retried. Inspect Rex for "
                           + assignment["window_label"] + ABANDON_HINT)
    window_id, block_id = assignment_pane(view, assignment)
    record["session_id"] = session_id
    assignment.update(window_id=window_id, block_id=block_id, state="launched")


def launch(path, *, task, assignment_id, role, goal, cwd, command,
           binary=None, server=None, purpose=None, dependencies=(), frozen_release=None, frozen_delivery=None):
    if not goal.strip():
        raise FactoryError("Supply a nonempty assignment goal before launching")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", assignment_id):
        raise FactoryError("Assignment ID must be a lowercase slug, at most 64 characters")
    cwd = str(Path(cwd).resolve(strict=True))
    if not Path(cwd).is_dir() or not command or not Path(command[0]).is_file():
        raise FactoryError("Provide an existing directory and an absolute executable command")
    if not Path(command[0]).is_absolute() or not os.access(command[0], os.X_OK):
        raise FactoryError("Command executable must be an absolute executable file")
    record = load_task(path) if os.path.lexists(path) else new_task(task)
    existing = record["assignments"].get(assignment_id)
    if existing is None and "--write" in command and (
            command[:4] == [sys.executable, "-E", "-s", str(ROOT / "codex_worker.py")]
            or command[:2] == [sys.executable, str(ROOT / "codex_worker.py")]):
        check_role_write(role)
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
    if not existing and command[:4] == [sys.executable, "-E", "-s", str(ROOT / "claude_worker.py")]:
        import claude_worker
        import review_evidence
        try:
            native_args = claude_worker.parser().parse_args(command[4:])
        except SystemExit:
            raise FactoryError("Claude command is damaged; restore its arguments before creating an assignment") from None
        if record["admission"] is None or not valid_inputs(record["inputs"]) or native_args.role != role:
            raise FactoryError("Admit the Claude task and select matching role/revision inputs before launching")
        check_checkout(record["admission"]["repository"], cwd, record["inputs"]["candidate"],
                       expected_identity=record["admission"]["repository_identity"],
                       expected_git_dir=native_args.worktree_git_dir)
        if native_args.write:
            check_role_write(role)
        else:
            instructions += review_evidence.render(cwd, candidate=record["inputs"]["candidate"],
                requirements=record["inputs"]["requirements"], base=native_args.base, role=role)
    if not existing and command[:4] == [sys.executable, "-E", "-s", str(ROOT / "grok_worker.py")]:
        require_grok_execution()
        import grok_worker
        import review_evidence
        try:
            native_args = grok_worker.parser().parse_args(command[4:])
        except SystemExit:
            raise FactoryError("Grok command is damaged; restore its arguments before creating an assignment") from None
        if record["admission"] is None or not valid_inputs(record["inputs"]) or native_args.role != role:
            raise FactoryError("Admit the Grok task and select matching role/revision inputs before launching")
        check_checkout(record["admission"]["repository"], cwd, record["inputs"]["candidate"],
                       expected_identity=record["admission"]["repository_identity"],
                       expected_git_dir=native_args.worktree_git_dir)
        instructions += review_evidence.render(cwd, candidate=record["inputs"]["candidate"],
            requirements=record["inputs"]["requirements"], base=native_args.base, role=role)
    if not existing and any(flag in command for flag in ("--delivery-evidence", "--delivery-evidence-digest")):
        import codex_worker
        import release_evidence
        if command[:4] != [sys.executable, "-E", "-s", str(ROOT / "codex_worker.py")]:
            raise FactoryError("Delivered evidence requires the verified native adapter")
        try:
            native_args = codex_worker.parser().parse_args(command[4:])
        except SystemExit:
            raise FactoryError("Damaged final native release command") from None
        if native_args.write or native_args.role != "release" or role != "release" or any(flag in command for flag in ("--release-evidence", "--release-evidence-digest")):
            raise FactoryError("Final acceptance evidence needs a distinct read-only release assignment")
        frozen = decode_json(native_args.delivery_evidence) if native_args.delivery_evidence is not None else frozen_delivery
        if native_args.delivery_evidence_digest is not None and release_evidence.digest(frozen) != native_args.delivery_evidence_digest:
            raise FactoryError("Supply the original delivery evidence matching the command digest before launching")
        release_evidence.validate_delivery(frozen, record)
        instructions += release_evidence.render_delivery(frozen)
    if not existing and any(flag in command for flag in ("--release-evidence", "--release-evidence-digest")):
        import codex_worker
        import release_evidence
        if command[:4] != [sys.executable, "-E", "-s", str(ROOT / "codex_worker.py")]:
            raise FactoryError("Release evidence requires the verified native Codex adapter")
        try:
            native_args = codex_worker.parser().parse_args(command[4:])
        except SystemExit:
            raise FactoryError("Damaged native release-review command") from None
        if native_args.write or native_args.role != role:
            raise FactoryError("Frozen release evidence requires a matching read-only specialist")
        frozen = decode_json(native_args.release_evidence) if native_args.release_evidence is not None else frozen_release
        if native_args.release_evidence_digest is not None and release_evidence.digest(frozen) != native_args.release_evidence_digest:
            raise FactoryError("Supply the original release evidence matching the command digest before launching")
        frozen = release_evidence.validate(frozen, record)
        instructions += release_evidence.render(frozen)
    if not existing and integration_request(request):
        import integration
        integration.validate_launch(record, request, task, assignment_id, path)
        instructions += integration.render_intent(decode_json(command[5]))
    request["instructions_sha256"] = (existing["request"]["instructions_sha256"] if existing
                                      else hashlib.sha256(instructions.encode()).hexdigest())
    if existing:
        if existing["state"] == "abandoned":
            raise FactoryError("Assignment was abandoned after a rejected launch; use a new explicit ID")
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
        raise FactoryError("Reconcile the existing pending assignment with status before another launch, "
                           "or abandon it if Rex rejected that launch")
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
        worktree_git_dir = git_dir(cwd)
        check_checkout(record["admission"]["repository"], cwd, current,
                       expected_identity=record["admission"]["repository_identity"],
                       expected_git_dir=worktree_git_dir)
    if (not existing and command[:4] == [sys.executable, "-E", "-s", str(ROOT / "codex_worker.py")]
            and any(flag in command for flag in ("--decision-store", "--decision", "--question"))):
        import codex_worker
        import coordination_evidence
        try:
            native_args = codex_worker.parser().parse_args(command[4:])
        except SystemExit:
            raise FactoryError("Native command is damaged; restore its arguments before launching") from None
        if native_args.decision is not None or native_args.question is not None or native_args.decision_store is not None:
            if native_args.role != role or not native_args.decision_store or record["inputs"] is None:
                raise FactoryError("Bind a native coordination assignment to its role, original store and selected inputs")
            coordination_evidence.binding(native_args.decision, native_args.question, role, write=native_args.write)
            instructions += coordination_evidence.render(native_args.decision_store, native_args.decision,
                native_args.question, role, record["inputs"], task_record=record, native=native_args)
        request["instructions_sha256"] = hashlib.sha256(instructions.encode()).hexdigest()
    if record["rex"] is not None:
        rex = connect(record, binary, server)
        if record["session_id"] is not None and session_view(rex, record) is None:
            # Every pane of a vanished session is lost; keep that, then open a new session.
            tidy(path, rex, record)
    else:
        if (not binary or not Path(binary).is_absolute() or not Path(binary).is_file()
                or not os.access(binary, os.X_OK) or not server or not server.startswith("unix://")):
            raise FactoryError("First launch requires --rex PATH and an explicit local --server unix:// endpoint")
        rex = Rex(str(Path(binary).resolve()), server)
        who = rex_object(rex.call("whoami", "--json"), strings=("host_id", "instance_id"))
        record["rex"] = dict(binary=rex.binary, server=server, host_id=who["host_id"])
        rex.instance_id = who["instance_id"]
    brief_path = path.parent / (task + "." + assignment_id + ".md")
    save(brief_path, instructions)
    replacements = {"{brief}": str(brief_path), "{instructions}": instructions,
                    "{brief_sha256}": request["instructions_sha256"], "{cwd}": cwd}
    if record["admission"] is not None:
        replacements.update({"{candidate}": request["inputs"]["candidate"],
                             "{requirements}": request["inputs"]["requirements"],
                             "{repository}": record["admission"]["repository"],
                             "{repository_identity}": json.dumps(record["admission"]["repository_identity"], sort_keys=True)})
    argv = [replacements.get(part, part) for part in command]
    if record["admission"] is not None:
        argv = [sys.executable, "-E", "-s", str(ROOT / "checkout_exec.py"), "--identity",
                json.dumps(record["admission"]["repository_identity"], sort_keys=True),
                "--candidate", record["inputs"]["candidate"], "--git-dir", str(worktree_git_dir), "--", *argv]
    assignment: dict = dict(request=request, state="launching", result=None, native_collected=False, brief=str(brief_path),
                            window_label=role + "-" + assignment_id + "-" + uuid.uuid4().hex[:8],
                            rex_instance=current_instance(rex))
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


def current_instance(rex):
    """The connected Rex server instance; a restart replaces it and every original process."""
    if vars(rex).get("instance_id") is None:
        rex.instance_id = rex_object(rex.call("whoami", "--json"), strings=("instance_id",))["instance_id"]
    return rex.instance_id


def status(rex, record, assignment) -> dict:
    """Observe the original process. `lost` means Rex can no longer report how it ended."""
    if assignment["state"] == "abandoned":
        return dict(role=assignment["request"]["role"], state="abandoned", session_id=None, window_id=None,
                    block_id=None, exit=None, reason=assignment["abandoned"]["reason"])
    closed = assignment.get("closed")
    if closed is not None:
        retained = dict(role=assignment["request"]["role"], state=closed["state"],
                        session_id=closed["session_id"], window_id=assignment["window_id"],
                        block_id=assignment["block_id"], exit=closed["exit"], closed=True)
        return retained if closed["reason"] is None else {**retained, "reason": closed["reason"]}
    observation = dict(role=assignment["request"]["role"], state="unknown",
                       session_id=record["session_id"], window_id=assignment["window_id"],
                       block_id=assignment["block_id"], exit=None)
    launched_on = assignment.get("rex_instance")
    if launched_on is not None and launched_on != current_instance(rex):
        # Rex restores the pane with a fresh login shell and no exit status, which
        # would otherwise read as a running assignment forever.
        return {**observation, "state": "lost",
                "reason": "Rex server restarted after launch; the original process ended without an observable exit status"}
    try:
        process = rex.call("block", "call", "-s", record["session_id"],
                           "-b", assignment["block_id"], "process")
    except FactoryError:
        view = session_view(rex, record)
        if view is not None and assignment["block_id"] in view_blocks(view):
            raise
        return {**observation, "state": "lost",
                "reason": "Rex pane or session was closed outside the factory; the original process ended without an observable exit status"}
    rex_object(process)
    last_exit = process.get("last_exit")
    if last_exit is not None and (not isinstance(last_exit, dict) or type(last_exit.get("exit_code")) is not int):
        raise FactoryError("Malformed Rex exit status; inspect the pinned binary/server API compatibility")
    state = ("exited" if last_exit["exit_code"] == 0 else "failed") if last_exit else (
        "running" if process.get("child") or process.get("foreground") else "unknown")
    return {**observation, "state": state, "exit": last_exit}


def captured(rex, record, assignment):
    """Original terminal output: live while the tab is open, retained once it is tidied."""
    if assignment["state"] == "abandoned":
        raise FactoryError("No pane: the launch was abandoned. " + assignment["abandoned"]["reason"])
    closed = assignment.get("closed")
    if closed is not None:
        if closed["capture"] is None:
            raise FactoryError("Tab was closed without retained output: " + (closed["reason"] or closed["state"]))
        return read_record(Path(closed["capture"]), private=True)
    observed = status(rex, record, assignment)
    if observed["state"] == "lost":
        # A closed pane has no output, and a restored pane shows only a new shell.
        raise FactoryError("Pane output unavailable: " + observed["reason"])
    output = rex.call("capture", "-s", record["session_id"], "-b", assignment["block_id"],
                      "--trim", "--unwrap", raw=True)
    if not isinstance(output, str):
        raise FactoryError("Malformed Rex capture; inspect the pinned binary/server API compatibility")
    return output


def tidy(path, rex, record, *, only=None) -> dict:
    """Close finished assignment tabs; each keeps its final observation and output first."""
    closed, running = [], []
    view = session_view(rex, record)
    open_blocks = set() if view is None else view_blocks(view)
    for identity, assignment in record["assignments"].items():
        if (only is not None and identity not in only) or assignment["state"] != "launched":
            continue
        if assignment.get("closed") is None:
            observed = status(rex, record, assignment)
            if observed["state"] not in TERMINAL_STATES:
                running.append(identity)
                continue
            capture = None
            if observed["state"] != "lost":
                capture_path = path.parent / (record["task"] + "." + identity + ".capture.txt")
                save(capture_path, captured(rex, record, assignment))
                capture = str(capture_path)
            assignment["closed"] = dict(state=observed["state"], exit=observed["exit"], reason=observed.get("reason"),
                                        session_id=record["session_id"], capture=capture, at=time.time())
            save(path, record)  # Durable before the pane disappears.
            closed.append(identity)
        elif assignment["closed"]["session_id"] != record["session_id"]:
            continue  # Its session is already gone.
        # Also retries a retained tab whose earlier close failed. Closing only the factory's
        # block keeps any operator split; a lone block takes its tab along.
        if assignment["block_id"] in open_blocks:
            rex.call("block", "close", "-s", record["session_id"], assignment["block_id"], raw=True)
    return dict(closed=closed, running=running, session_closed=close_idle_session(path, rex, record))


def close_idle_session(path, rex, record):
    """Kill the task session once nothing open remains in it; the next launch starts a new one."""
    if record["session_id"] is None or any(
            item["state"] == "launching" or (item["state"] == "launched" and item.get("closed") is None)
            for item in record["assignments"].values()):
        return False
    view = session_view(rex, record)
    if view is not None:
        if view_windows(view):
            return False  # Operator tabs remain; leave their session alone.
        rex.call("kill", record["session_id"], raw=True)
    record.update(session_id=None, session_label=new_session_label(record["task"]))
    save(path, record)
    return True


def check_commit_message(message, *, write):
    if message is not None and (not write or not isinstance(message, str) or not message.strip()
            or not message.splitlines()[0].strip() or len(message.splitlines()[0]) > 72 or "\0" in message):
        raise FactoryError("Use --commit-message only with --write and supply a nonempty commit subject of at most 72 characters")


def codex_command(path, *, task, assignment_id, codex, model, effort, cwd, write=False, seconds=600, commit_message=None, role=None,
                  decision_store=None, decision=None, question=None, release_evidence=None, delivery_evidence=None):
    """Generate one explicit worker invocation; no model/effort or permission fallback."""
    if not model.strip() or not effort.strip():
        raise FactoryError("Supply a discovered native model and supported effort before creating an assignment")
    if not Path(codex).is_absolute() or type(seconds) is not int or not 1 <= seconds <= MAX_NATIVE_SECONDS:
        raise FactoryError(f"Supply an absolute native Codex path and a positive execution allowance of at most {MAX_NATIVE_SECONDS} seconds")
    check_commit_message(commit_message, write=write)
    binary = Path(codex).resolve()
    existing = load_task(path)["assignments"].get(assignment_id) if path.exists() else None
    if existing is None and write:
        check_role_write(role)
    if existing is None:
        try:
            binary = Path(codex).resolve(strict=True)
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise FactoryError("Supply a regular executable native Codex file before creating an assignment")
        except OSError:
            raise FactoryError("Cannot access --codex; supply an existing executable native Codex path") from None
    prefix = path.parent / (task + "." + assignment_id + ".codex")
    worker = [sys.executable, "-E", "-s", str(ROOT / "codex_worker.py")]
    if existing is not None and existing["request"]["command"][:2] == [sys.executable, str(ROOT / "codex_worker.py")]:
        worker = [sys.executable, str(ROOT / "codex_worker.py")]  # Reconnect the original command unchanged.
    command = worker + ["--job", task + "/" + assignment_id,
               "--codex", str(binary), "--model", model, "--effort", effort, "--seconds", str(seconds),
               "--repo", "{repository}", "--cwd", "{cwd}", "--brief", "{brief}",
               "--brief-sha256", "{brief_sha256}", "--candidate", "{candidate}",
               "--requirements", "{requirements}", "--events", str(prefix) + ".jsonl",
               "--receipt", str(prefix) + ".json"]
    if existing is None or "--repo-identity" in existing["request"]["command"]:
        command.extend(["--repo-identity", "{repository_identity}"])
    if existing is None:
        command.extend(["--worktree-git-dir", str(git_dir(cwd))])
    elif "--worktree-git-dir" in existing["request"]["command"]:
        saved = existing["request"]["command"]
        position = saved.index("--worktree-git-dir") + 1
        if position >= len(saved) or not Path(saved[position]).is_absolute():
            raise FactoryError("Saved worktree argument is damaged; restore the original assignment before reconnecting")
        command.extend(["--worktree-git-dir", saved[position]])
    if existing is not None and "--role" in existing["request"]["command"]:
        saved = existing["request"]["command"]
        position = saved.index("--role") + 1
        if position >= len(saved) or saved[position] != existing["request"]["role"]:
            raise FactoryError("Saved role argument is damaged; restore the original assignment before reconnecting")
        role = saved[position]
    elif existing is not None:
        role = None  # Preserve historical worker commands unchanged.
    if decision_store is not None or decision is not None or question is not None:
        import coordination_evidence
        if not coordination_evidence.binding(decision, question, role, write=write) or decision_store is None or not Path(decision_store).is_absolute():
            raise FactoryError("Supply the absolute original Jev decision store before a coordination assignment")
        if existing is None and role == "judge" and model != coordination_evidence.judge_model():
            raise FactoryError("Select the configured judge_model; arbitrary model choices cannot adjudicate this case")
        command.extend(["--decision-store", str(decision_store), "--decision", str(decision), "--question", str(question)])
    if delivery_evidence is not None:
        if existing is not None and "--delivery-evidence" in existing["request"]["command"]:
            command.extend(["--delivery-evidence", json.dumps(delivery_evidence, sort_keys=True)])
        else:
            import release_evidence as evidence
            command.extend(["--delivery-evidence-digest", evidence.digest(delivery_evidence)])
    if release_evidence is not None:
        if existing is not None and "--release-evidence" in existing["request"]["command"]:
            command.extend(["--release-evidence", json.dumps(release_evidence, sort_keys=True)])
        else:
            import release_evidence as evidence
            command.extend(["--release-evidence-digest", evidence.digest(release_evidence)])
    return command + (["--role", role] if role is not None else []) + (["--write"] if write else []) + (["--commit-message", commit_message] if commit_message is not None else [])


def codex_evidence(assignment_id, record, assignment, *, observe=None, collected=False, current=True, failed_identity=False):
    """Verify original native evidence; offline callers require a collected current result."""
    from codex_worker import parse_events, parser, read_events
    from factory_commit import check_commit_receipt

    command = assignment["request"]["command"]
    if command[:4] == [sys.executable, "-E", "-s", str(ROOT / "codex_worker.py")]:
        offset = 4
    elif command[:2] == [sys.executable, str(ROOT / "codex_worker.py")]:
        offset = 2  # Historical assignments retain their original launch command.
    else:
        raise FactoryError("Assignment is not a native Codex worker; use its provider's result adapter")
    if "--repo-identity" not in command:
        raise FactoryError("Native assignment predates repository identity; inspect its retained artifacts without relaunching it")
    if "--worktree-git-dir" not in command:
        raise FactoryError("Native assignment predates worktree identity; inspect its retained artifacts without relaunching it")
    try:
        args = parser().parse_args(command[offset:])
    except SystemExit:
        raise FactoryError("Saved native command is damaged; restore the original assignment arguments before collection, without relaunching it") from None
    if args.role is not None and args.role != assignment["request"]["role"]:
        raise FactoryError("Saved worker role differs from its assignment; inspect the original request before collection")
    if not failed_identity and (observe is None or collected) and (assignment["result"] is None or (current and not result_current(record, assignment["result"]))):
        raise FactoryError("Collect a current native result before using its answer as coordination evidence")
    if collected and assignment.get("native_collected") is not True:
        raise FactoryError("Native evidence lacks proven strict collection; explicitly collect it with its original checkout available")
    if observe is not None and observe()["state"] not in ({"failed", "exited", "lost"} if failed_identity else {"exited", "lost"}):
        raise FactoryError("Native worker has not exited successfully; inspect status/capture before collection")
    try:
        receipt = decode_json(args.receipt.read_text())
    except FileNotFoundError:
        raise FactoryError("Native receipt is missing; inspect the existing pane and events, never relaunch blindly") from None
    except (OSError, ValueError, UnicodeDecodeError):
        raise FactoryError(f"Cannot read native receipt {args.receipt.name!r}; inspect or restore the original artifact before collection") from None
    if isinstance(receipt, dict) and type(receipt.get("schema_version")) is int and receipt["schema_version"] in (1, 2, 3):
        raise FactoryError("Legacy native receipt lacks the current repository/worktree binding; inspect the original attempt without relaunching it")
    if (not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int
            or receipt["schema_version"] not in (4, 5, 6)
            or receipt.get("kind") != "codex-execution" or receipt.get("state") != ("failed" if failed_identity else "completed")
            or any(type(receipt.get(key)) not in (int, float)
                   or not 0 <= receipt[key] <= sys.float_info.max
                   for key in ("created_at", "finished_at", "elapsed_seconds"))
            or receipt["finished_at"] < receipt["created_at"]
            or (not isinstance(receipt.get("error"), str) if failed_identity else ("error" not in receipt or receipt["error"] is not None))
            or any(key not in receipt for key in ("thread_id", "report", "usage"))
            or (not failed_identity and (type(receipt.get("exit_code")) is not int or receipt["exit_code"] != 0))
            or receipt.get("job") != record["task"] + "/" + assignment_id
            or not valid_inputs(receipt.get("inputs"))
            or receipt.get("inputs") != assignment["request"].get("inputs")
            or not valid_repository_identity(receipt.get("repository_identity"))
            or receipt["repository_identity"] != record["admission"]["repository_identity"]
            or receipt.get("brief_sha256") != assignment["request"]["instructions_sha256"]
            or receipt.get("cwd") != assignment["request"]["cwd"]
            or not args.worktree_git_dir.is_absolute()
            or receipt.get("worktree_git_dir") != str(args.worktree_git_dir)
            or receipt.get("requested_model") != args.model or receipt.get("requested_effort") != args.effort
            or receipt.get("binary") != str(args.codex) or not args.codex.is_absolute()
            or type(receipt.get("writer")) is not bool or receipt["writer"] != args.write
            or (not failed_identity and (not isinstance(receipt.get("candidate"), str)
                or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", receipt["candidate"])
                or (not args.write and receipt.get("candidate") != receipt["inputs"]["candidate"])))
            or receipt.get("events") != str(args.events)):
        raise FactoryError("Native receipt does not match this completed assignment; inspect the original artifacts")
    if failed_identity:
        # Identity-only recovery of a failed contributor. This is never a report,
        # success receipt, dependency or approval, even if partial events contain one.
        events = read_events(args.events)
        threads = [event.get("thread_id") for event in events
                   if isinstance(event, dict) and event.get("type") == "thread.started"]
        if (not args.write or not events or not isinstance(events[0], dict)
                or events[0].get("type") != "thread.started" or len(threads) != 1
                or not isinstance(threads[0], str) or not threads[0]
                or receipt.get("thread_id") not in (None, threads[0])):
            raise FactoryError("Unknown or ambiguous original failed contributor context")
        try:
            instructions = Path(assignment["brief"]).read_text()
        except (OSError, UnicodeError):
            raise FactoryError("Restore the original failed contributor brief") from None
        if hashlib.sha256(instructions.encode()).hexdigest() != assignment["request"]["instructions_sha256"]:
            raise FactoryError("Original failed contributor brief changed")
        return args, receipt, dict(thread_id=threads[0])
    import coordination_evidence
    coordination = None
    if args.decision is not None or args.question is not None:
        try:
            args.brief = Path(assignment["brief"])
            instructions = args.brief.read_text()
        except (OSError, UnicodeError):
            raise FactoryError("Restore the original native brief before collecting its bounded answer") from None
        if hashlib.sha256(instructions.encode()).hexdigest() != assignment["request"]["instructions_sha256"]:
            raise FactoryError("Native coordination brief changed; inspect the original frozen question")
        coordination = coordination_evidence.read(instructions, args.decision, args.question, args.role,
            assignment["request"]["inputs"], write=args.write)
        if args.role == "judge":
            coordination_evidence.check_investigation(coordination["request"], record)
            coordination_evidence.validate_judge_capacity(coordination.get("judge_capacity"), args.codex,
                args.model, args.effort, coordination.get("judge_model"), receipt["created_at"])
    if (receipt["schema_version"] == 6) != (coordination is not None):
        raise FactoryError("Native receipt format differs from its bounded coordination assignment")
    native = parse_events(read_events(args.events), coordination)
    if any(receipt.get(key) != native[key] for key in ("thread_id", "report", "usage")):
        raise FactoryError("Native receipt differs from its original events; reconcile before recording results")
    args.cwd = Path(assignment["request"]["cwd"])  # Resolve the frozen launch placeholder.
    if observe is not None or collected:
        # Only a fully collected result may outlive its original checkout.
        # Initial collection (including stale work) keeps the strict HEAD check.
        check_commit_receipt(receipt, args, record["admission"]["repository"],
                             historical=collected)
    elif (coordination is None or args.write or receipt.get("commit") is not None
            or repository_identity(record["admission"]["repository"]) != receipt["repository_identity"]):
        raise FactoryError("Offline coordination needs collected read-only evidence from the original repository")
    if observe is None and (assignment["result"]["outcome"] != native["report"]["outcome"]
            or assignment["result"]["candidate"] != receipt["candidate"]
            or str(args.receipt) not in assignment["result"]["artifacts"]):
        raise FactoryError("Collected native result differs from its original answer evidence")
    if collected:
        expected = dict(inputs=receipt["inputs"], candidate=receipt["candidate"],
            outcome=native["report"]["outcome"], artifacts=list(dict.fromkeys([str(args.receipt), *native["report"]["artifacts"]])))
        if assignment["result"] != expected:
            raise FactoryError("Collected result differs from the complete original native report")
    if observe is not None or collected:
        try:
            instructions = Path(assignment["brief"]).read_text()
        except (OSError, UnicodeError):
            raise FactoryError("Missing original native brief") from None
        if hashlib.sha256(instructions.encode()).hexdigest() != assignment["request"]["instructions_sha256"]:
            raise FactoryError("Original native brief changed")
    return args, receipt, native


def collect_codex(path, assignment_id, rex, record, assignment):
    """Register a finished native report; this never accepts the whole task."""
    args, receipt, native = codex_evidence(assignment_id, record, assignment, observe=lambda: status(rex, record, assignment))
    artifacts = list(dict.fromkeys([str(args.receipt), *native["report"]["artifacts"]]))
    record = record_result(path, assignment_id=assignment_id, candidate=receipt["candidate"],
                           requirements=receipt["inputs"]["requirements"],
                           outcome=native["report"]["outcome"], artifacts=artifacts)
    record["assignments"][assignment_id]["native_collected"] = True
    save(path, record)
    return record


def claude_evidence(assignment_id, record, assignment, *, observe=None, collected=False, current=True):
    """Strict original reviewer or writer evidence; history is granted only by actual collection."""
    import claude_worker
    import native_claude
    import review_evidence
    command, request = assignment["request"]["command"], assignment["request"]
    if command[:4] != [sys.executable, "-E", "-s", str(ROOT / "claude_worker.py")]:
        raise FactoryError("Not an original Claude worker; use the original provider collector")
    options = [part.split("=", 1)[0] for part in command[4:] if part.startswith("--")]
    if len(options) != len(set(options)) or any("=" in part for part in command[4:] if part.startswith("--")):
        raise FactoryError("Restore the unambiguous original Claude command before collection")
    try:
        args = claude_worker.parser().parse_args(command[4:])
    except SystemExit:
        raise FactoryError("Restore original Claude worker arguments before collection") from None
    if collected and (assignment.get("native_collected") is not True or assignment["result"] is None):
        raise FactoryError("Claude evidence needs strict original collection, never manual result registration")
    if collected and current and not result_current(record, assignment["result"]):
        raise FactoryError("Claude result is stale; obtain a judgment of current inputs")
    if observe is None and not collected:
        raise FactoryError("First Claude collection requires original successful Rex exit")
    if observe is not None and observe()["state"] not in {"exited", "lost"}:
        raise FactoryError("Claude worker has not exited successfully; inspect its original Rex pane")
    try:
        receipt = decode_json(read_record(args.receipt, private=True))
        instructions = read_record(Path(assignment["brief"]), private=True)
    except (OSError, ValueError, UnicodeError):
        raise FactoryError("Restore original private Claude receipt and brief before collection") from None
    if (not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int
            or receipt["schema_version"] != (2 if args.write else 1)
            or (args.write and (receipt.get("writer") is not True or args.commit_message is None
                                or args.sandbox is None or receipt.get("sandbox") != str(args.sandbox)
                                or "commit" not in receipt))
            or receipt.get("kind") != "claude-execution" or receipt.get("state") != "completed"
            or "error" not in receipt or receipt["error"] is not None
            or type(receipt.get("exit_code")) is not int or receipt["exit_code"] != 0
            or any(type(receipt.get(k)) not in (int, float) or not 0 <= receipt[k] <= sys.float_info.max
                   for k in ("created_at", "finished_at", "elapsed_seconds"))
            or receipt["finished_at"] < receipt["created_at"]
            or args.job != record["task"] + "/" + assignment_id or receipt.get("job") != args.job
            or receipt.get("inputs") != request.get("inputs") or not valid_inputs(receipt.get("inputs"))
            or args.candidate != "{candidate}" or args.requirements != "{requirements}"
            or (not args.write and receipt.get("candidate") != receipt["inputs"]["candidate"])
            or receipt.get("role") != request["role"] or args.role != request["role"]
            or receipt.get("brief_sha256") != request["instructions_sha256"]
            or hashlib.sha256(instructions.encode()).hexdigest() != request["instructions_sha256"]
            or args.brief_sha256 != "{brief_sha256}" or str(args.brief) != "{brief}"
            or receipt.get("repository_identity") != record["admission"]["repository_identity"]
            or args.repo_identity != "{repository_identity}" or str(args.repo) != "{repository}"
            or str(args.cwd) != "{cwd}" or receipt.get("cwd") != request["cwd"]
            or not args.worktree_git_dir.is_absolute() or receipt.get("worktree_git_dir") != str(args.worktree_git_dir)
            or not args.claude.is_absolute() or receipt.get("binary") != str(args.claude)
            or not args.model.strip() or args.effort not in native_claude.EFFORTS
            or receipt.get("requested_model") != args.model or receipt.get("requested_effort") != args.effort
            or type(args.seconds) is not int or not 1 <= args.seconds <= MAX_NATIVE_SECONDS
            or type(receipt.get("seconds")) is not int or receipt["seconds"] != args.seconds
            or not 1 <= args.turns <= 1000 or type(receipt.get("max_turns")) is not int or receipt["max_turns"] != args.turns
            or receipt.get("native_session") != args.native_session
            or not args.events.is_absolute() or not args.receipt.is_absolute()
            or receipt.get("events") != str(args.events)):
        raise FactoryError("Claude receipt differs from its original bound assignment; restore original evidence without relaunching")
    args.candidate, args.requirements = receipt["inputs"]["candidate"], receipt["inputs"]["requirements"]
    try:
        if str(uuid.UUID(args.native_session)) != args.native_session:
            raise ValueError("Invalid session")
        native_claude.validate_auth(receipt.get("discovery"), args.claude, Path(request["cwd"]))
        if not args.write:
            review_evidence.validate(instructions, candidate=args.candidate, requirements=args.requirements, base=args.base, role=args.role)
        native = native_claude.parse_execution(claude_worker.read_events(args.events),
            session_id=args.native_session, cwd=request["cwd"], model=args.model, max_turns=args.turns,
            writer=args.write)
    except (ValueError, native_claude.ClaudeProbeError) as exc:
        raise FactoryError(str(exc)) from None
    if any(receipt.get(k) != native[k] for k in ("report", "usage", "model_usage", "turns", "rate_limits")):
        raise FactoryError("Claude receipt conflicts with original native events; restore original evidence")
    args.cwd = Path(request["cwd"])
    repository = record["admission"]["repository"]
    if repository_identity(repository) != receipt["repository_identity"]:
        raise FactoryError("Restore the admitted repository marker before validating Claude provenance")
    if (git_commit(repository, args.candidate) != args.candidate
            or git_run(repository, "cat-file", "-e", args.candidate + "^{tree}").returncode):
        raise FactoryError("Restore original Claude candidate Git objects before validating historical provenance")
    if args.write:
        from factory_commit import check_commit_receipt
        check_commit_receipt(receipt, args, repository, historical=collected)
    candidate = receipt["candidate"] if args.write else args.candidate
    if not collected:
        check_checkout(record["admission"]["repository"], request["cwd"], candidate,
            expected_identity=receipt["repository_identity"], expected_git_dir=args.worktree_git_dir)
    expected = dict(inputs=receipt["inputs"], candidate=candidate, outcome=native["report"]["outcome"],
                    artifacts=list(dict.fromkeys([str(args.receipt), *native["report"]["artifacts"]])))
    if collected and assignment["result"] != expected:
        raise FactoryError("Collected Claude result differs from the complete original report")
    return args, receipt, native


def collect_claude(path, assignment_id, rex, record, assignment):
    args, receipt, native = claude_evidence(assignment_id, record, assignment,
        collected=assignment.get("native_collected") is True, current=False,
        observe=lambda: status(rex, record, assignment))
    record = record_result(path, assignment_id=assignment_id, candidate=receipt["candidate"],
        requirements=receipt["inputs"]["requirements"], outcome=native["report"]["outcome"],
        artifacts=list(dict.fromkeys([str(args.receipt), *native["report"]["artifacts"]])))
    record["assignments"][assignment_id]["native_collected"] = True
    save(path, record)
    return record


def require_grok_execution():
    import native_grok
    try:
        native_grok.require_execution_boundary()
    except native_grok.GrokProbeError as exc:
        raise FactoryError(str(exc)) from None


def claude_command(path, *, task, assignment_id, claude, model, effort, cwd, role, seconds=600, turns=30, base=None,
                   write=False, commit_message=None, sandbox=None):
    """Freeze one explicit Claude effect, reviewer or writer, including its native session."""
    import native_claude
    if (not isinstance(model, str) or not model.strip() or effort not in native_claude.EFFORTS
            or not Path(claude).is_absolute()
            or type(seconds) is not int or not 1 <= seconds <= MAX_NATIVE_SECONDS
            or type(turns) is not int or not 1 <= turns <= 1000):
        raise FactoryError("Supply an absolute native Claude executable, a discovered model, a supported effort and bounded seconds/turns")
    check_commit_message(commit_message, write=write)
    tool = None
    if write:
        check_role_write(role)
        if commit_message is None or sandbox is None or not Path(sandbox).is_absolute():
            raise FactoryError("A Claude writer needs --commit-message and the absolute Codex CLI as its commit sandbox")
        tool = Path(sandbox).resolve()
    if base is not None and write:
        raise FactoryError("Writers implement from the selected candidate; --base is for code review")
    worker = [sys.executable, "-E", "-s", str(ROOT / "claude_worker.py")]
    existing = load_task(path)["assignments"].get(assignment_id) if path.exists() else None
    if existing is None or base is not None:
        from review_evidence import valid_base
        valid_base(base, role)
    binary = Path(claude).resolve()
    if existing is None:
        try:
            binary = Path(claude).resolve(strict=True)
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise FactoryError("Supply an executable native Claude file before creating an assignment")
        except OSError:
            raise FactoryError("Restore the selected native Claude installation before creating an assignment") from None
        native_session, gitdir = str(uuid.uuid4()), str(git_dir(cwd))
    else:
        saved = existing["request"]["command"]
        if saved[:4] != worker:
            raise FactoryError("Existing assignment uses another command; inspect its original effect without relaunching")
        try:
            native_session = saved[saved.index("--native-session") + 1]
            gitdir = saved[saved.index("--worktree-git-dir") + 1]
            if str(uuid.UUID(native_session)) != native_session or not Path(gitdir).is_absolute():
                raise ValueError("Invalid frozen binding")
        except (ValueError, IndexError):
            raise FactoryError("Saved Claude binding is damaged; restore the original command without relaunching") from None
    writer = []
    if tool is not None:
        if existing is None and (not tool.is_file() or not os.access(tool, os.X_OK)):
            raise FactoryError("Supply the executable Codex CLI as the Claude writer's commit sandbox")
        writer = ["--write", "--commit-message", commit_message, "--sandbox", str(tool)]
    prefix = path.parent / (task + "." + assignment_id + ".claude")
    return worker + ([] if base is None else ["--base", base]) + writer + ["--job", task + "/" + assignment_id, "--claude", str(binary), "--model", model,
        "--effort", effort, "--seconds", str(seconds), "--turns", str(turns), "--role", role,
        "--native-session", native_session, "--repo", "{repository}", "--cwd", "{cwd}",
        "--brief", "{brief}", "--brief-sha256", "{brief_sha256}", "--candidate", "{candidate}",
        "--requirements", "{requirements}", "--repo-identity", "{repository_identity}",
        "--worktree-git-dir", gitdir, "--events", str(prefix) + ".jsonl", "--receipt", str(prefix) + ".json"]


def grok_command(path, *, task, assignment_id, grok, model, effort, cwd, role, seconds=600, turns=30, base=None):
    """Freeze one explicit read-only Grok effect, including its native session."""
    if (not model.strip() or not effort.strip() or not Path(grok).is_absolute()
            or type(seconds) is not int or not 1 <= seconds <= MAX_NATIVE_SECONDS
            or type(turns) is not int or not 1 <= turns <= 1000):
        raise FactoryError("Supply an absolute native Grok executable, discovered model/effort and bounded seconds/turns")
    worker = [sys.executable, "-E", "-s", str(ROOT / "grok_worker.py")]
    existing = load_task(path)["assignments"].get(assignment_id) if path.exists() else None
    if existing is None:
        require_grok_execution()
    if existing is None or base is not None:
        from review_evidence import valid_base
        valid_base(base, role)
    binary = Path(grok).resolve()
    if existing is None:
        try:
            binary = Path(grok).resolve(strict=True)
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise FactoryError("Supply an executable native Grok file before creating an assignment")
        except OSError:
            raise FactoryError("Restore the selected native Grok installation before creating an assignment") from None
        native_session, gitdir = str(uuid.uuid4()), str(git_dir(cwd))
    else:
        saved = existing["request"]["command"]
        if saved[:4] != worker:
            raise FactoryError("Existing assignment uses another command; inspect its original effect without relaunching")
        try:
            native_session = saved[saved.index("--native-session") + 1]
            gitdir = saved[saved.index("--worktree-git-dir") + 1]
            if str(uuid.UUID(native_session)) != native_session or not Path(gitdir).is_absolute():
                raise ValueError("Invalid frozen binding")
        except (ValueError, IndexError):
            raise FactoryError("Saved Grok binding is damaged; restore the original command without relaunching") from None
    prefix = path.parent / (task + "." + assignment_id + ".grok")
    return worker + ([] if base is None else ["--base", base]) + ["--job", task + "/" + assignment_id, "--grok", str(binary), "--model", model,
        "--effort", effort, "--seconds", str(seconds), "--turns", str(turns), "--role", role,
        "--native-session", native_session, "--repo", "{repository}", "--cwd", "{cwd}",
        "--brief", "{brief}", "--brief-sha256", "{brief_sha256}", "--candidate", "{candidate}",
        "--requirements", "{requirements}", "--repo-identity", "{repository_identity}",
        "--worktree-git-dir", gitdir, "--events", str(prefix) + ".jsonl", "--receipt", str(prefix) + ".json"]


def collect_grok(path, assignment_id, rex, record, assignment):
    """Collect the original Grok report; this never accepts a task."""
    require_grok_execution()
    import grok_worker
    import native_grok

    command = assignment["request"]["command"]
    if command[:4] != [sys.executable, "-E", "-s", str(ROOT / "grok_worker.py")]:
        raise FactoryError("Assignment is not a Grok worker; use its original provider's result adapter")
    try:
        args = grok_worker.parser().parse_args(command[4:])
    except SystemExit:
        raise FactoryError("Saved Grok command is damaged; restore its original arguments before collection") from None
    from review_evidence import valid_base
    if args.role == "code-review" and args.base is None:
        raise FactoryError("Historical Grok code review has no frozen base/diff evidence and remains inspection-only; authorize a new assignment with --base before reviewing")
    valid_base(args.base, args.role)
    if status(rex, record, assignment)["state"] not in {"exited", "lost"}:
        raise FactoryError("Grok worker has not exited successfully; inspect its original status and capture")
    try:
        receipt = decode_json(args.receipt.read_text())
    except (OSError, ValueError, UnicodeError):
        raise FactoryError("Cannot read the original Grok receipt; inspect or restore it without relaunching") from None
    request = assignment["request"]
    keys = ("report", "request_id", "usage", "model_usage", "turns")
    if (not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int
            or receipt["schema_version"] != 2 or receipt.get("kind") != "grok-execution"
            or receipt.get("state") != "completed" or "error" not in receipt or receipt["error"] is not None
            or type(receipt.get("exit_code")) is not int or receipt["exit_code"] != 0
            or any(type(receipt.get(key)) not in (int, float) or not 0 <= receipt[key] <= sys.float_info.max
                   for key in ("created_at", "finished_at", "elapsed_seconds"))
            or receipt["finished_at"] < receipt["created_at"] or any(key not in receipt for key in keys)
            or receipt.get("job") != record["task"] + "/" + assignment_id
            or not valid_inputs(receipt.get("inputs")) or receipt["inputs"] != request.get("inputs")
            or receipt.get("candidate") != request["inputs"]["candidate"]
            or receipt.get("repository_identity") != record["admission"]["repository_identity"]
            or not valid_repository_identity(receipt.get("repository_identity"))
            or receipt.get("brief_sha256") != request["instructions_sha256"]
            or args.role != request["role"] or receipt.get("role") != args.role
            or receipt.get("cwd") != request["cwd"] or not args.worktree_git_dir.is_absolute()
            or receipt.get("worktree_git_dir") != str(args.worktree_git_dir)
            or not args.grok.is_absolute() or receipt.get("binary") != str(args.grok)
            or receipt.get("native_session") != args.native_session
            or not isinstance(receipt.get("discovery"), dict)
            or "configuration" not in receipt
            or receipt["discovery"].get("configuration") != receipt["configuration"]
            or receipt.get("requested_model") != args.model or receipt.get("requested_effort") != args.effort
            or type(receipt.get("seconds")) is not int or receipt["seconds"] != args.seconds
            or type(receipt.get("max_turns")) is not int or receipt["max_turns"] != args.turns
            or receipt.get("events") != str(args.events)):
        raise FactoryError("Grok receipt differs from its frozen completed assignment; inspect the original artifacts")
    try:
        native_grok.validate_discovery(receipt["discovery"], args.grok, request["cwd"])
        native = native_grok.parse_execution(grok_worker.read_events(args.events), session_id=args.native_session,
                                            cwd=request["cwd"], max_turns=args.turns)
    except native_grok.GrokProbeError as exc:
        raise FactoryError(str(exc)) from None
    if json.dumps({key: receipt[key] for key in keys}, sort_keys=True) != json.dumps(
            {key: native[key] for key in keys}, sort_keys=True):
        raise FactoryError("Grok receipt differs from its original events; reconcile the retained attempt before recording a result")
    if (repository_identity(request["cwd"]) != receipt["repository_identity"]
            or git_dir(request["cwd"]) != args.worktree_git_dir):
        raise FactoryError("Grok worktree binding changed; restore its original repository and Git directory before collection")
    return record_result(path, assignment_id=assignment_id, candidate=receipt["candidate"],
        requirements=receipt["inputs"]["requirements"], outcome=native["report"]["outcome"],
        artifacts=list(dict.fromkeys([str(args.receipt), *native["report"]["artifacts"]])))


def tidy_tasks(directory, task=None, binary=None, server=None):
    """Tidy one task or every task record in a state directory; failures are reported per task."""
    tasks = [task] if task else sorted(item.stem for item in directory.glob("*.json")
                                       if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", item.stem))
    report, failed = {}, False
    for name in tasks:
        try:
            with task_lock(directory, name) as path:
                record = load_task(path)
                report[name] = (dict(closed=[], running=[], session_closed=False) if record["rex"] is None
                                else tidy(path, connect(record, binary, server), record))
        except (FactoryError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
            report[name], failed = dict(error=str(exc)), True
    return report, failed


def main(argv=None):
    relocate("factory.py", sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--rex", help="Absolute Rex binary; required for the first launch")
    parser.add_argument("--server", help="Explicit local unix:// Rex endpoint; pinned per task")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("start", "codex-start", "grok-start", "claude-start"):
        start = sub.add_parser(action, help="Launch or reconnect to an explicit assignment")
        start.add_argument("--task", required=True)
        start.add_argument("--id", required=True, help="Stable assignment ID; new attempt needs a new ID")
        start.add_argument("--role", required=True)
        start.add_argument("--goal", required=True)
        start.add_argument("--cwd", required=True)
        start.add_argument("--purpose", choices=("work", "correction", "investigation"),
                           help="Required for admitted tasks; consumes the declared task allowance")
        start.add_argument("--after", action="append", default=[], help="Earlier assignment requiring a current successful result")
        if action == "start":
            start.add_argument("command", nargs=argparse.REMAINDER)
        elif action == "codex-start":
            start.add_argument("--codex", required=True)
            start.add_argument("--model", required=True)
            start.add_argument("--effort", required=True)
            start.add_argument("--write", action="store_true")
            start.add_argument("--commit-message", help="With --write, the factory commits a completed writer result")
            start.add_argument("--seconds", type=int, default=600)
            start.add_argument("--decision-store", type=Path)
            start.add_argument("--decision")
            start.add_argument("--question")
            start.add_argument("--delivery", help="Freeze an original confirmed integration for final release judgment")
            start.add_argument("--release-pr", help="Freeze complete canonical feedback for a native read-only judgment")
            start.add_argument("--gh", type=Path)
            start.add_argument("--release-scripts", type=Path)
        else:
            start.add_argument("--claude" if action == "claude-start" else "--grok", required=True)
            start.add_argument("--model", required=True)
            start.add_argument("--effort", required=True)
            start.add_argument("--seconds", type=int, default=600)
            start.add_argument("--turns", type=int, default=30)
            start.add_argument("--base", help="Full base commit ID; required for code-review")
            if action == "claude-start":
                start.add_argument("--write", action="store_true", help="Implementation specialist with edit tools")
                start.add_argument("--commit-message", help="With --write, the factory commits a completed result")
                start.add_argument("--sandbox", help="With --write, the absolute Codex CLI used as the commit's local sandbox")
    delivery = sub.add_parser("integrate", help="Integrate selected exact-input native approvals; existing IDs reconcile read-only")
    delivery.add_argument("--task", required=True)
    delivery.add_argument("--id", required=True)
    delivery.add_argument("--pr", required=True)
    delivery.add_argument("--review", required=True)
    delivery.add_argument("--acceptance", help="Explicit final native release judgment; read-only after delivery")
    delivery.add_argument("--after", action="append", required=True)
    delivery.add_argument("--gh", type=Path, required=True)
    delivery.add_argument("--release-scripts", type=Path, required=True)
    admission = sub.add_parser("admit", help="Record task scope and limits without launching")
    admission.add_argument("--task", required=True)
    admission.add_argument("--goal", required=True)
    admission.add_argument("--repo", required=True)
    admission.add_argument("--acceptance", action="append", required=True, help="Original evidence reference")
    admission.add_argument("--corrections", type=int)
    admission.add_argument("--investigations", type=int)
    extension = sub.add_parser("extend-allowance", help="Record an operator-approved addition without resetting history")
    extension.add_argument("--task", required=True)
    extension.add_argument("--purpose", choices=("correction", "investigation"), required=True)
    extension.add_argument("--additional", type=int, required=True)
    extension.add_argument("--expected-limit", type=int, required=True)
    extension.add_argument("--request-id", required=True)
    extension.add_argument("--reason", required=True)
    inputs = sub.add_parser("inputs", help="Select the task's candidate and requirements revision")
    inputs.add_argument("--task", required=True)
    inputs.add_argument("--candidate", required=True)
    inputs.add_argument("--requirements", required=True)
    provision = sub.add_parser("provision", help="Provision or reconcile one explicitly named writer worktree")
    provision.add_argument("--task", required=True)
    provision.add_argument("--id", required=True, help="Stable worktree name; branch is factory/TASK/ID")
    provision.add_argument("--path", required=True, help="New absolute path outside existing checkouts")
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
        if action in {"pause", "resume"}:
            command.add_argument("--request-id", help="Stable ID makes a repeated control request idempotent")
            command.add_argument("--expected-revision", type=int, help="Apply only to this control-history revision")
    for action in ("status", "capture", "codex-collect", "grok-collect", "claude-collect"):
        command = sub.add_parser(action)
        command.add_argument("--task", required=True)
        command.add_argument("--id", required=True)
    abandon = sub.add_parser("abandon", help="Record that a pending launch never created its pane")
    abandon.add_argument("--task", required=True)
    abandon.add_argument("--id", required=True)
    abandon.add_argument("--reason", required=True, help="Why the launch is known not to have happened")
    cleanup = sub.add_parser("tidy", help="Close finished assignment tabs, retaining their final status and output")
    cleanup.add_argument("--task", help="Only this task; default is every task in the state directory")
    args = parser.parse_args(argv)
    try:
        if args.action == "codex-start" and (not args.model.strip() or not args.effort.strip()):
            raise FactoryError("Supply a discovered native model and supported effort before creating an assignment")
        if args.action == "codex-start" and (not Path(args.codex).is_absolute() or not 1 <= args.seconds <= MAX_NATIVE_SECONDS):
            raise FactoryError(f"Supply an absolute native Codex path and a positive execution allowance of at most {MAX_NATIVE_SECONDS} seconds")
        if args.action == "codex-start":
            check_commit_message(args.commit_message, write=args.write)
        if args.action == "claude-start" and (not args.model.strip() or not Path(args.claude).is_absolute()
                or not 1 <= args.seconds <= MAX_NATIVE_SECONDS or not 1 <= args.turns <= 1000):
            raise FactoryError("Supply a discovered Claude model, an absolute binary and bounded seconds/turns")
        if args.action == "grok-start" and (not args.model.strip() or not args.effort.strip()
                or not Path(args.grok).is_absolute() or not 1 <= args.seconds <= MAX_NATIVE_SECONDS
                or not 1 <= args.turns <= 1000):
            raise FactoryError("Supply a native Grok path, discovered model/effort and bounded seconds/turns before launch")
        if args.action == "tidy":
            report, failed = tidy_tasks(args.state_dir.resolve(), args.task, args.rex, args.server)
            print(json.dumps(report, indent=2))
            return 1 if failed else 0
        if args.action == "integrate":
            import integration
            path = args.state_dir.resolve() / (args.task + ".json")
            if args.acceptance is not None:
                with task_lock(path.parent, args.task):
                    original = load_task(path)["assignments"].get(args.id)
                    if original is None or original["result"] is None:
                        raise FactoryError("Final acceptance only observes an existing terminal integration")
            receipt = integration.integrate(path, assignment_id=args.id, pr=args.pr, review=args.review,
                required=args.after, gh=args.gh, scripts=args.release_scripts)
            if args.acceptance is not None:
                import release_evidence
                with task_lock(path.parent, args.task):
                    record = load_task(path)
                transport = release_evidence.GitHub(args.gh, args.release_scripts, record["admission"]["repository"])
                receipt = {**receipt, "acceptance": integration.accepted(record, args.id, args.acceptance, transport)}
            print(json.dumps(receipt, indent=2))
            return 0 if receipt["state"] in {"launched", "delivered"} else 1
        prepared = None
        frozen_release = frozen_delivery = None
        if args.action == "codex-start" and (args.release_pr is not None or args.delivery is not None):
            import release_evidence
            if args.release_pr is not None and args.delivery is not None:
                raise FactoryError("Select pre-merge release review or final delivery judgment")
            with task_lock(args.state_dir.resolve(), args.task) as path:
                prepared = load_task(path)
            if prepared["admission"] is None:
                raise FactoryError("Admit a task before capturing native release evidence")
            original = prepared["assignments"].get(args.id)
            if args.delivery is not None:
                if original is not None:
                    frozen_delivery = release_evidence.from_delivery_assignment(prepared, original)
                    if frozen_delivery["assignment"] != args.delivery:
                        raise FactoryError("Original delivery differs; use a new final review assignment")
                else:
                    delivered = release_evidence.delivery(prepared, args.delivery)
                    intent = delivered["receipt"]["intent"]
                    transport = release_evidence.GitHub(intent["gh"], intent["scripts"], prepared["admission"]["repository"])
                    frozen_delivery = release_evidence.capture_delivery(prepared, args.delivery, transport, cwd=args.cwd)
            elif original is not None:
                frozen_release = release_evidence.from_assignment(prepared, original)
                if frozen_release["pr"]["url"] != args.release_pr:
                    raise FactoryError("Original release PR differs; use a new review assignment")
            else:
                if args.gh is None or args.release_scripts is None:
                    raise FactoryError("Supply --gh and --release-scripts explicit installed paths")
                transport = release_evidence.GitHub(args.gh, args.release_scripts, prepared["admission"]["repository"])
                frozen_release = release_evidence.capture(prepared, args.release_pr, transport, cwd=args.cwd)
        with (execution_lock(args.state_dir.resolve()) if args.action in {"start", "codex-start", "grok-start", "claude-start", "provision"} else nullcontext()), task_lock(args.state_dir.resolve(), args.task) as path:
            if prepared is not None and load_task(path) != prepared:
                raise FactoryError("Task changed during evidence capture; reload current controls/inputs before submission")
            if args.action in {"admit", "show", "pause", "resume", "inputs", "result", "extend-allowance", "provision"}:
                if args.action == "admit":
                    record = admit(path, task=args.task, goal=args.goal, repository=args.repo,
                                   acceptance=args.acceptance, corrections=args.corrections,
                                   investigations=args.investigations)
                elif args.action == "show":
                    record = load_task(path)
                elif args.action == "extend-allowance":
                    record = extend_allowance(path, purpose=args.purpose, additional=args.additional,
                        expected_limit=args.expected_limit, request_id=args.request_id, reason=args.reason)
                elif args.action == "inputs":
                    record = set_inputs(path, candidate=args.candidate, requirements=args.requirements)
                elif args.action == "provision":
                    record = provision_worktree(path, worktree_id=args.id, destination=args.path)
                elif args.action == "result":
                    record = record_result(path, assignment_id=args.id, candidate=args.candidate,
                                           requirements=args.requirements, outcome=args.outcome,
                                           artifacts=args.artifact)
                else:
                    record = hold_task(path, args.reason if args.action == "pause" else None,
                                       request_id=args.request_id, expected_revision=args.expected_revision)
                print(json.dumps(task_view(record), indent=2))
                return 0
            if args.action == "abandon":
                record = load_task(path)
                if record["rex"] is None:
                    raise FactoryError("Only a pending launch can be abandoned; inspect the assignment with status")
                record = abandon_launch(path, connect(record, args.rex, args.server), assignment_id=args.id, reason=args.reason)
                print(json.dumps(task_view(record), indent=2))
                return 0
            if args.action in {"start", "codex-start", "grok-start", "claude-start"}:
                if args.action == "codex-start":
                    if load_task(path)["admission"] is None:
                        raise FactoryError("Admit a task before starting a native Codex assignment")
                    argv = codex_command(path, task=args.task, assignment_id=args.id, codex=args.codex,
                                         model=args.model, effort=args.effort, cwd=args.cwd,
                                         write=args.write, seconds=args.seconds, commit_message=args.commit_message, role=args.role,
                                         decision_store=args.decision_store, decision=args.decision, question=args.question, release_evidence=frozen_release, delivery_evidence=frozen_delivery)
                elif args.action == "claude-start":
                    if load_task(path)["admission"] is None:
                        raise FactoryError("Admit a task before starting a native Claude assignment")
                    argv = claude_command(path, task=args.task, assignment_id=args.id, claude=args.claude,
                        model=args.model, effort=args.effort, cwd=args.cwd, role=args.role,
                        seconds=args.seconds, turns=args.turns, base=args.base,
                        write=args.write, commit_message=args.commit_message, sandbox=args.sandbox)
                elif args.action == "grok-start":
                    if load_task(path)["admission"] is None:
                        raise FactoryError("Admit a task before starting a native Grok assignment")
                    argv = grok_command(path, task=args.task, assignment_id=args.id, grok=args.grok,
                        model=args.model, effort=args.effort, cwd=args.cwd, role=args.role,
                        seconds=args.seconds, turns=args.turns, base=args.base)
                else:
                    argv = args.command[1:] if args.command[:1] == ["--"] else args.command
                record, assignment = launch(path, task=args.task, assignment_id=args.id,
                    role=args.role, goal=args.goal, cwd=args.cwd, command=argv,
                    binary=args.rex, server=args.server, purpose=args.purpose, dependencies=args.after,
                    frozen_release=frozen_release, frozen_delivery=frozen_delivery)
                rex = connect(record)
            else:
                record = load_task(path)
                if args.id not in record["assignments"]:
                    raise FactoryError(f"Unknown assignment {args.id!r}; use show --task {args.task} to find its ID")
                assignment = record["assignments"][args.id]
                if args.action in {"status", "capture"} and (
                        assignment["state"] == "abandoned" or assignment.get("closed") is not None):
                    # Answered from the task record alone, even while Rex is unavailable.
                    check_pins(record, args.rex, args.server)
                    rex = None
                else:
                    rex = connect(record, args.rex, args.server)
                if assignment["state"] == "launching":
                    reconcile(rex, record, assignment)
                    save(path, record)
            if args.action in {"codex-collect", "claude-collect", "grok-collect"}:
                collect = (collect_codex if args.action == "codex-collect"
                           else collect_claude if args.action == "claude-collect" else collect_grok)
                record = collect(path, args.id, rex, record, assignment)
                try:
                    tidy(path, rex, record, only={args.id})  # A collected result no longer needs its tab.
                except (FactoryError, OSError) as exc:
                    # The result is already saved; show it, then fail so the open tab is noticed.
                    print(json.dumps(task_view(record), indent=2))
                    print("factory: Collected result saved, but its tab was not tidied; run tidy: " + str(exc),
                          file=sys.stderr)
                    return 1
                print(json.dumps(task_view(record), indent=2))
            elif args.action == "capture":
                print(captured(rex, record, assignment), end="")
            else:
                print(json.dumps(status(rex, record, assignment), indent=2))
    except (FactoryError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print("factory: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
