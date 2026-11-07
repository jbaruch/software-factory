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
import tempfile
import time
from typing import Any
import uuid

if __name__ == "__main__":
    sys.modules.setdefault("factory", sys.modules[__name__])

ROOT = Path(__file__).resolve().parent
GIT_EXECUTION: ContextVar[Any] = ContextVar("factory_git_execution", default=None)
MAX_NATIVE_SECONDS = 86400
RESULT_OUTCOMES = {"completed", "approved", "findings", "blocked"}
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
    return json.loads(value, object_pairs_hook=object_pairs_hook,
                      parse_constant=reject_json_constant, parse_float=finite_json_float,
                      parse_int=finite_json_integer)


class Rex:
    def __init__(self, binary, server):
        self.binary, self.server = str(binary), server

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
            return json.loads(result.stdout)
        except json.JSONDecodeError:
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
    return dict(schema_version=7, task=task, admission=None, inputs=None, hold=None, control_events=[], allowance_events=[], rex=None,
                session_label="factory-" + task + "-" + uuid.uuid4().hex[:8],
                session_id=None, assignments={})


def read_record(path, *, private=False):
    """Read a regular gate record without following links or blocking on FIFOs."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (private and (
                info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077)):
            raise OSError("Gate record must be a regular file with its required ownership and permissions")
        return stream.read()


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
    # Schema 6 belongs to the retained provisioning prototype, not this reader.
    if type(version) is not int or version not in (0, 1, 2, 3, 4, 5, 7):
        raise FactoryError("Unsupported task schema; update the factory before continuing")
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
        if integration_request(request):
            import integration
            integration.intent_of(assignment)
            if any(key in assignment for key in ("window_id", "block_id")):
                raise FactoryError("Mechanical integration cannot own Rex panes")
            earlier.add(assignment_id)
            continue
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
    if version < 5:
        record.update(schema_version=5, control_events=[])
    validate_control_events(record)
    if version < 7:
        record.update(schema_version=7, allowance_events=[])
    allowance_limits(record)
    if version < 7:
        save(path, record)
    return record


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
                              text=True, timeout=10, check=False,
                              env=git_environment() if environment is None else environment)
        if scope is not None and result.returncode == 125:
            raise FactoryError("Native sandbox rewrote protected Git environment; inspect its environment settings before another attempt")
        return result
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
        allowed = set()
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
    return {**record, "allowances": allowances, "assignments": assignments}


def result_current(record, result):
    return result is not None and record["inputs"] == dict(
        candidate=result["candidate"], requirements=result["inputs"]["requirements"])


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


def role_spec(role_id):
    roles = json.loads((ROOT / "skills/factory/references/roles.json").read_text())["roles"]
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
            if assignment["state"] == "launched" and not integration_request(assignment["request"]):
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
    expected = {item["window_label"] for item in record["assignments"].values() if not integration_request(item["request"])}
    required = {item["window_label"] for item in record["assignments"].values() if item["state"] == "launched" and not integration_request(item["request"])}
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
    if integration_request(assignment["request"]):
        raise FactoryError("Use factory integrate with the original inputs to reconcile the pending merge read-only")
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
    if not existing and "--delivery-evidence" in command:
        import codex_worker
        import release_evidence
        if command[:4] != [sys.executable, "-E", "-s", str(ROOT / "codex_worker.py")]:
            raise FactoryError("Delivered evidence requires the verified native adapter")
        try:
            native_args = codex_worker.parser().parse_args(command[4:])
        except SystemExit:
            raise FactoryError("Damaged final native release command") from None
        if native_args.write or native_args.role != "release" or role != "release" or "--release-evidence" in command:
            raise FactoryError("Final acceptance evidence needs a distinct read-only release assignment")
        frozen = decode_json(native_args.delivery_evidence)
        if release_evidence.delivery(record, frozen["assignment"]) != frozen:
            raise FactoryError("Final release evidence differs from original confirmed delivery")
        instructions += release_evidence.render_delivery(frozen)
    if not existing and "--release-evidence" in command:
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
        frozen = release_evidence.validate(decode_json(native_args.release_evidence), record)
        instructions += release_evidence.render(frozen)
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
    else:
        if (not binary or not Path(binary).is_absolute() or not Path(binary).is_file()
                or not os.access(binary, os.X_OK) or not server or not server.startswith("unix://")):
            raise FactoryError("First launch requires --rex PATH and an explicit local --server unix:// endpoint")
        rex = Rex(str(Path(binary).resolve()), server)
        who = rex_object(rex.call("whoami", "--json"), strings=("host_id",))
        record["rex"] = dict(binary=rex.binary, server=server, host_id=who["host_id"])
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
    if integration_request(assignment["request"]):
        return dict(state="exited" if assignment["result"] is not None else "unknown",
                    owner="integration", action="Repeat factory integrate with the original inputs to reconcile read-only")
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
        command.extend(["--delivery-evidence", json.dumps(delivery_evidence, sort_keys=True)])
    if release_evidence is not None:
        command.extend(["--release-evidence", json.dumps(release_evidence, sort_keys=True)])
    return command + (["--role", role] if role is not None else []) + (["--write"] if write else []) + (["--commit-message", commit_message] if commit_message is not None else [])


def codex_evidence(assignment_id, record, assignment, *, observe=None, collected=False, current=True):
    """Verify original native evidence; offline callers require a collected current result."""
    from codex_worker import check_commit_receipt, parse_events, parser, read_events

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
    if observe is None and (assignment["result"] is None or (current and not result_current(record, assignment["result"]))):
        raise FactoryError("Collect a current native result before using its answer as coordination evidence")
    if observe is not None and observe()["state"] != "exited":
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
            or receipt.get("kind") != "codex-execution" or receipt.get("state") != "completed"
            or any(type(receipt.get(key)) not in (int, float)
                   or not 0 <= receipt[key] <= sys.float_info.max
                   for key in ("created_at", "finished_at", "elapsed_seconds"))
            or receipt["finished_at"] < receipt["created_at"]
            or "error" not in receipt or receipt["error"] is not None
            or any(key not in receipt for key in ("thread_id", "report", "usage"))
            or type(receipt.get("exit_code")) is not int or receipt["exit_code"] != 0
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
            or not isinstance(receipt.get("candidate"), str)
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", receipt["candidate"])
            or (not args.write and receipt.get("candidate") != receipt["inputs"]["candidate"])
            or receipt.get("events") != str(args.events)):
        raise FactoryError("Native receipt does not match this completed assignment; inspect the original artifacts")
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
        check_commit_receipt(receipt, args, record["admission"]["repository"])
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
    return record_result(path, assignment_id=assignment_id, candidate=receipt["candidate"],
                         requirements=receipt["inputs"]["requirements"],
                         outcome=native["report"]["outcome"], artifacts=artifacts)


def require_grok_execution():
    import native_grok
    try:
        native_grok.require_execution_boundary()
    except native_grok.GrokProbeError as exc:
        raise FactoryError(str(exc)) from None


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
    if status(rex, record, assignment)["state"] != "exited":
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".factory-state")
    parser.add_argument("--rex", help="Absolute Rex binary; required for the first launch")
    parser.add_argument("--server", help="Explicit local unix:// Rex endpoint; pinned per task")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("start", "codex-start", "grok-start"):
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
            start.add_argument("--grok", required=True)
            start.add_argument("--model", required=True)
            start.add_argument("--effort", required=True)
            start.add_argument("--seconds", type=int, default=600)
            start.add_argument("--turns", type=int, default=30)
            start.add_argument("--base", help="Full base commit ID; required for code-review")
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
    for action in ("status", "capture", "codex-collect", "grok-collect"):
        command = sub.add_parser(action)
        command.add_argument("--task", required=True)
        command.add_argument("--id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "codex-start" and (not args.model.strip() or not args.effort.strip()):
            raise FactoryError("Supply a discovered native model and supported effort before creating an assignment")
        if args.action == "codex-start" and (not Path(args.codex).is_absolute() or not 1 <= args.seconds <= MAX_NATIVE_SECONDS):
            raise FactoryError(f"Supply an absolute native Codex path and a positive execution allowance of at most {MAX_NATIVE_SECONDS} seconds")
        if args.action == "codex-start":
            check_commit_message(args.commit_message, write=args.write)
        if args.action == "grok-start" and (not args.model.strip() or not args.effort.strip()
                or not Path(args.grok).is_absolute() or not 1 <= args.seconds <= MAX_NATIVE_SECONDS
                or not 1 <= args.turns <= 1000):
            raise FactoryError("Supply a native Grok path, discovered model/effort and bounded seconds/turns before launch")
        with (execution_lock(args.state_dir.resolve()) if args.action in {"start", "codex-start", "grok-start", "integrate"} else nullcontext()), task_lock(args.state_dir.resolve(), args.task) as path:
            if args.action == "integrate":
                import integration
                if args.acceptance is not None:
                    original = load_task(path)["assignments"].get(args.id)
                    if original is None or original["result"] is None:
                        raise FactoryError("Final acceptance only observes an existing terminal integration")
                receipt = integration.integrate(path, assignment_id=args.id, pr=args.pr, review=args.review,
                    required=args.after, gh=args.gh, scripts=args.release_scripts)
                if args.acceptance is not None:
                    import release_evidence
                    record = load_task(path)
                    transport = release_evidence.GitHub(args.gh, args.release_scripts, record["admission"]["repository"])
                    receipt = {**receipt, "acceptance": integration.accepted(record, args.id, args.acceptance, transport)}
                print(json.dumps(receipt, indent=2))
                return 0 if receipt["state"] == "delivered" else 1
            if args.action in {"admit", "show", "pause", "resume", "inputs", "result", "extend-allowance"}:
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
                elif args.action == "result":
                    record = record_result(path, assignment_id=args.id, candidate=args.candidate,
                                           requirements=args.requirements, outcome=args.outcome,
                                           artifacts=args.artifact)
                else:
                    record = hold_task(path, args.reason if args.action == "pause" else None,
                                       request_id=args.request_id, expected_revision=args.expected_revision)
                print(json.dumps(task_view(record), indent=2))
                return 0
            if args.action in {"start", "codex-start", "grok-start"}:
                if args.action == "codex-start":
                    if load_task(path)["admission"] is None:
                        raise FactoryError("Admit a task before starting a native Codex assignment")
                    frozen_delivery = None
                    if args.delivery is not None:
                        import release_evidence
                        if args.release_pr is not None:
                            raise FactoryError("Select pre-merge release review or final delivery judgment")
                        frozen_delivery = release_evidence.delivery(load_task(path), args.delivery)
                    frozen_release = None
                    if args.release_pr is not None:
                        import release_evidence
                        selected_record = load_task(path)
                        original = selected_record["assignments"].get(args.id)
                        if original is not None:
                            frozen_release = release_evidence.from_assignment(selected_record, original)
                            if frozen_release["pr"]["url"] != args.release_pr:
                                raise FactoryError("Original release PR differs; use a new review assignment")
                        else:
                            if args.gh is None or args.release_scripts is None:
                                raise FactoryError("Supply --gh and --release-scripts explicit installed paths")
                            transport = release_evidence.GitHub(args.gh, args.release_scripts, selected_record["admission"]["repository"])
                            frozen_release = release_evidence.capture(selected_record, args.release_pr, transport)
                    argv = codex_command(path, task=args.task, assignment_id=args.id, codex=args.codex,
                                         model=args.model, effort=args.effort, cwd=args.cwd,
                                         write=args.write, seconds=args.seconds, commit_message=args.commit_message, role=args.role,
                                         decision_store=args.decision_store, decision=args.decision, question=args.question, release_evidence=frozen_release, delivery_evidence=frozen_delivery)
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
            if args.action == "codex-collect":
                print(json.dumps(task_view(collect_codex(path, args.id, rex, record, assignment)), indent=2))
            elif args.action == "grok-collect":
                print(json.dumps(task_view(collect_grok(path, args.id, rex, record, assignment)), indent=2))
            elif args.action == "capture":
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
