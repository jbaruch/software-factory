"""Linked-cache census, attended retirement and native Go aging."""
from contextlib import contextmanager, ExitStack
import calendar
import ctypes
import errno
import fcntl
import hashlib
import importlib
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import tempfile
import time

import factory
import release_evidence


MIN_KIB = 1024 * 1024
IDLE_SECONDS = 24 * 3600
GO_BUILD_SECONDS = 120
DARWIN = ctypes.CDLL(None, use_errno=True)
TASK = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


@contextmanager
def inode_lock(path, *, shared=False, directory=True):
    """Use the existing inode. A census must never create missing owner lock files."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_DIRECTORY if directory else 0))
    try:
        info = os.fstat(fd)
        if directory != stat.S_ISDIR(info.st_mode) or not directory and not (
                stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_nlink == 1 and not info.st_mode & 0o077):
            raise factory.FactoryError(f"Inspect unexpected lock inode at {path}")
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise factory.ExecutionBusy(f"Busy lock at {path}; retry after the current operation") from None
        yield fd
    finally:
        os.close(fd)


def real_directory(path, device=None):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or path.resolve(strict=True) != path
            or info.st_uid != os.getuid() or info.st_mode & 0o022
            or device is not None and info.st_dev != device):
        raise factory.FactoryError(f"Inspect changed path, owner, permissions or mount at {path}")
    return info


def linked_admin(cwd):
    """The inode shared by every cwd that routes Git to the same linked cache."""
    for root in (cwd, *cwd.parents):
        pointer = root / ".git"
        if (pointer.is_file() or (pointer / "commondir").is_file()
                or (root / "commondir").is_file() and (root / "HEAD").is_file()):
            return factory.git_dir(root)
        if pointer.is_dir():
            break
    return None


@contextmanager
def launch_lock(admin, directory):
    """Publish the actual intent store before Rex can start using a provisioned tree."""
    with inode_lock(admin, shared=True) as fd:
        try:
            marker = factory.read_record(admin / "locked")
        except FileNotFoundError:
            marker = ""
        if re.fullmatch(r"factory-[a-z0-9-]+-[0-9a-f]{32}", marker.strip()):
            directory = directory.resolve(strict=True)
            info = directory.stat()
            name = f"factory-launch-store-{info.st_dev}-{info.st_ino}"
            try:
                os.symlink(directory, name, dir_fd=fd)
            except FileExistsError:
                if not (admin / name).is_symlink() or not directory.samefile(admin / name):
                    raise factory.FactoryError("Restore the original launch store binding for this worktree") from None
            os.fsync(fd)
        if not os.path.samestat(os.fstat(fd), admin.stat()):
            raise factory.FactoryError("Git admin identity changed before launch; inspect the original checkout")
        yield


def review_snapshot(row):
    # This is a local format check, never release evidence or task ownership.
    fields = {"pr_number", "head_sha", "ci", "reviews", "review_bodies", "inline_comments",
              "inline_comment_bodies", "merge_state"}
    if (not isinstance(row, dict) or not fields <= row.keys() <= fields | {"watch"}
            or not isinstance(row["inline_comments"], dict) or not isinstance(row.get("watch", {}), dict)):
        return False
    number, head = row["pr_number"], row["head_sha"]
    if type(number) is not int or number <= 0 or not isinstance(head, str) or not re.fullmatch(release_evidence.SHA, head):
        return False
    try:
        release_evidence.feedback(row, {"number": number, "head": head})
    except factory.FactoryError:
        return False
    return True


def public_review_snapshot(path):
    try:
        device = path.parent.stat().st_dev
        identity = metadata_identity(path, device)
        document = factory.decode_json(factory.read_record(path, max_bytes=1024**2))
        return metadata_identity(path, device) == identity and review_snapshot(document)
    except (OSError, ValueError, factory.FactoryError):
        return False  # The caller reports the original ownership refusal.


def records(directory):
    # Original task snapshots only. Foreign lines can also launch against this checkout.
    items = []
    for path in directory.iterdir():
        if path.suffix != ".json" or not TASK.fullmatch(path.stem):
            continue
        try:
            raw = factory.read_record_bytes(path, private=True)
            row = factory.decode_json(raw.decode("utf-8"))
        except OSError as exc:
            if public_review_snapshot(path):
                continue
            raise factory.FactoryError(f"Cannot rule out another owner; inspect {path}: {exc}") from exc
        except ValueError as exc:
            raise factory.FactoryError(f"Cannot rule out another owner; inspect {path}: {exc}") from exc
        if review_snapshot(row):
            continue
        if not isinstance(row, dict) or row.get("task") != path.stem or not isinstance(row.get("assignments"), dict):
            raise factory.FactoryError(f"Restore the original task record at {path}")
        items.append((row, raw))
    return items


def checkout_inodes(cwd, admin):
    return [[info.st_dev, info.st_ino] for info in
            (cwd.lstat(), admin.lstat(), (cwd / ".git").lstat(), (admin / "gitdir").lstat())]


def metadata_identity(path, device):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_dev != device
            or info.st_nlink != 1 or info.st_mode & 0o022):
        raise factory.FactoryError(f"Inspect unexpected Factory or Git ownership metadata at {path}")
    return info.st_dev, info.st_ino


def binding(record, repo, cache):
    item = record["provisioning"]
    if not item or item["state"] != "provisioned" or record["admission"]["repository"] != str(repo):
        raise factory.FactoryError("Restore the provisioned task and its admitted primary repository")
    work = Path(item["request"]["path"])
    common = factory.git_common_dir(repo)
    device = real_directory(repo).st_dev
    paths = [work, common, common / "worktrees", cache.parent, cache]
    ids = []
    for path in paths:
        info = real_directory(path, device)
        ids.append((info.st_dev, info.st_ino))
    for marker in (work / ".git", cache.parent / "locked", cache.parent / "gitdir", common / factory.REPOSITORY_MARKER):
        ids.append(metadata_identity(marker, device))
    if (common != repo / ".git" or factory.provisioning_owned(record, item) != cache.parent
            or factory.provisioning_git(work, "symbolic-ref", "HEAD").rstrip("\n") != "refs/heads/" + item["request"]["branch"]):
        raise factory.FactoryError("Inspect worktree, branch and reciprocal Git pointers; identity differs")
    witness = checkout_binding(work, cache.parent, record["admission"]["repository_identity"])
    if not witness or not factory.original_checkout(item, witness):
        raise factory.FactoryError("Missing or changed original writer checkout at provisioning; retain the cache")
    if stat.S_IMODE(cache.stat().st_mode) != 0o700:
        raise factory.FactoryError(f"Inspect cache permissions at {cache}; expected private mode 0700")
    head = witness["head"]
    if not record.get("inputs") or head != record["inputs"]["candidate"]:
        raise factory.FactoryError("Inspect the changed checkout HEAD against the current task candidate")
    return (work, item["owner_token"], head, ids)


def checkout_binding(cwd, admin, repository_identity) -> dict | None:
    common = Path(repository_identity["common_dir"])
    device = real_directory(common).st_dev
    if admin.parent != common / "worktrees":
        raise factory.FactoryError(f"Restore the original linked Git directory for worktree {cwd}")
    try:
        work = real_directory(cwd, device)
    except FileNotFoundError:
        # Only ENOENT at both original names under their canonical parents proves absence.
        real_directory(cwd.parent, device)
        if os.path.lexists(admin.parent):
            real_directory(admin.parent, device)
        factory.check_deregistered(common.parent, cwd, admin)
        try:
            admin.lstat()
        except FileNotFoundError:
            return None
        raise factory.FactoryError(f"Worktree {cwd} is missing but its Git directory survives") from None
    linked = real_directory(admin, device)
    pointer = metadata_identity(cwd / ".git", device)
    backlink = metadata_identity(admin / "gitdir", device)
    if (factory.read_record_bytes(cwd / ".git") != os.fsencode(f"gitdir: {admin}\n")
            or factory.read_record_bytes(admin / "gitdir") != os.fsencode(cwd / ".git") + b"\n"
            or factory.git_dir(cwd) != admin or factory.repository_identity(cwd) != repository_identity):
        raise factory.FactoryError(f"Restore the reciprocal Git pointers and repository identity for worktree {cwd}")
    try:
        metadata_identity(admin / "locked", device)
        marker = factory.read_record_bytes(admin / "locked").decode()
        if marker and (not marker.endswith("\n") or marker.endswith("\r\n")):
            raise factory.FactoryError(f"Inspect changed worktree lock contents at {admin}")
        marker = marker.removesuffix("\n")
    except FileNotFoundError:
        marker = None
    head = factory.git_commit(str(cwd), "HEAD")
    inodes = [[work.st_dev, work.st_ino], [linked.st_dev, linked.st_ino], list(pointer), list(backlink)]
    if checkout_inodes(cwd, admin) != inodes:
        raise factory.FactoryError(f"Physical checkout changed during readback at {cwd}")
    return dict(cwd=str(cwd), git_dir=str(admin), repository=repository_identity, head=head, marker=marker, inodes=inodes)


def terminal_attempt(record, directory, work, admin, name, assignment, *, ordinary_release=False, native_aging=False):
    reader = None
    job = record["task"] + "/" + name
    req = assignment["request"]
    cmd = req["command"]
    # This maintenance operation never executes a saved worker command.
    harness = factory.worker_hint(cmd)
    path = directory / f"{record['task']}.{name}.{harness}.json"
    closed = assignment.get("closed") or {}
    if (assignment["state"] != "launched" or closed.get("state") not in ("exited", "failed")
            or not factory.valid_closed(assignment)):
        raise factory.FactoryError(f"{job}: need a tidied, observed Rex process exit; inspect the original attempt")
    if harness is None:
        raise factory.FactoryError(f"{job}: native assignment or exact worktree is ambiguous")
    parser = importlib.import_module(harness + "_worker").parser()
    parser.allow_abbrev = False
    args = factory.evidence_worker_args(harness, parser, name, record, assignment, replay=assignment.get("native_collected") is True)
    cwd = Path(req["cwd"])
    launch = assignment.get("launch_checkout")
    root = cwd
    if not args.write:
        if isinstance(launch, dict) and launch.get("head") == req["inputs"]["candidate"]:
            root = Path(launch["cwd"])
        elif ordinary_release and launch is None:
            root = work
        else:
            raise factory.FactoryError(f"{job}: missing original reader worktree ownership at launch")
        if cwd != root and (not cwd.is_relative_to(root) or not cwd.is_dir() or cwd.resolve() != cwd
                            or Path(factory.git_worktree(cwd)) != root or factory.git_dir(cwd) != args.worktree_git_dir):
            raise factory.FactoryError(f"{job}: restore the original reader working directory at {cwd}")
    if root == work:
        bound = args.worktree_git_dir == admin
        if not args.write and launch is not None:
            observed = checkout_binding(work, admin, record["admission"]["repository_identity"])
            if observed and (ordinary_release or (
                    native_aging and closed["state"] == "exited" and assignment.get("native_collected") is True
                    and observed["head"] == record["inputs"]["candidate"])):
                observed = {**observed, "head": req["inputs"]["candidate"]}
            if not observed or launch != observed:
                raise factory.FactoryError(f"{job}: changed original reader worktree ownership at {cwd}")
    else:
        # A present sibling has reciprocal links. A retired sibling needs its original release.
        reader_admin = args.worktree_git_dir
        if args.write or reader_admin.parent != admin.parent or reader_admin == admin:
            raise factory.FactoryError(f"{job}: only a reader bound to a distinct linked checkout may use another cwd")
        observed = checkout_binding(root, reader_admin, record["admission"]["repository_identity"])
        if observed is not None and (reader_admin.samefile(admin) or observed != launch):
            raise factory.FactoryError(f"{job}: missing or changed original reader worktree ownership at {cwd}")
        if observed is None and (closed["state"] != "exited" or assignment.get("native_collected") is not True):
            raise factory.FactoryError(f"{job}: missing strict reader collection and successful Rex exit")
        reader = (root, reader_admin, observed)
        bound = True
    options = [word.partition("=")[0] for word in cmd if word.startswith("--")]
    brief = directory / f"{record['task']}.{name}.md"
    if (Path(assignment["brief"]) != brief or hashlib.sha256(
            factory.read_record(brief, private=True).encode()).hexdigest() != req["instructions_sha256"]):
        raise factory.FactoryError(f"{job}: restore the original frozen assignment brief")
    if (len(options) != len(set(options)) or args.job != job or args.receipt != path
            or args.role not in (None, req["role"]) or not bound or args.events != path.with_suffix(".jsonl")
            or args.cwd != Path("{cwd}") or args.repo != Path("{repository}")
            or args.repo_identity != "{repository_identity}" or args.brief != Path("{brief}")
            or args.brief_sha256 != "{brief_sha256}" or args.candidate != "{candidate}"
            or args.requirements != "{requirements}"):
        raise factory.FactoryError(f"{job}: restore the exact native worktree, inputs and receipt binding")
    raw = factory.read_record_bytes(path, private=True)
    receipt = factory.decode_json(raw)
    expected = {"kind": harness + "-execution", "job": job, "inputs": req["inputs"],
                "brief_sha256": req["instructions_sha256"], "cwd": req["cwd"], "worktree_git_dir": str(args.worktree_git_dir),
                "events": str(args.events), "requested_model": args.model, "requested_effort": args.effort,
                "binary": str(getattr(args, "agy" if harness == "antigravity" else harness)),
                "repository_identity": record["admission"]["repository_identity"]}
    if harness != "codex":
        expected.update(role=req["role"], seconds=args.seconds)
    if harness in ("claude", "grok"):
        expected.update(native_session=args.native_session, max_turns=args.turns)
    if harness == "codex" or args.write:
        expected["writer"] = args.write
    elif "writer" in receipt:
        expected["writer"] = False
    if args.write and harness != "codex":
        expected["sandbox"] = str(args.sandbox)
    code = closed["exit"]["exit_code"]
    success = closed["state"] == "exited"
    if (not isinstance(receipt, dict) or any(
            k not in receipt or type(receipt[k]) is not type(v) or receipt[k] != v for k, v in expected.items())
            or success != (code == 0) or receipt.get("state") != ("completed" if success else "failed")
            or success and (type(receipt.get("exit_code")) is not int or receipt["exit_code"] != 0)):
        raise factory.FactoryError(f"{job}: inspect inconsistent terminal receipt and Rex exit")
    version = receipt.get("schema_version")
    versions = {"codex": (4, 5, 6), "claude": (1, 2, 3, 4, 5), "grok": (2, 3, 4), "antigravity": (1, 2, 3, 4)}
    if type(version) is not int:
        raise factory.FactoryError(f"{job}: restore the original versioned native receipt")
    if version == getattr(factory, "SCRATCH_RECEIPT_VERSION", None):
        # The runtime owning #256 supplies both the scope and the exact PID check.
        # This maintenance command never classifies or accesses session scratch.
        gone = getattr(factory, "process_proven_gone")
        scope = getattr(factory, "process_scope")()
        if (not isinstance(scope, str) or not scope or type(receipt.get("exit_code")) is not int
                or not gone(receipt.get("native_process"), scope, receipt["exit_code"])):
            raise factory.FactoryError(f"{job}: recorded native process is active or cannot be verified")
    elif version not in versions[harness] or "native_process" in receipt:
        raise factory.FactoryError(f"{job}: use the runtime owning this native process/receipt version")
    selected_writer = False
    if success:
        result = assignment.get("result")
        if (not assignment["native_collected"] or not result or str(path) not in result["artifacts"]
                or receipt.get("candidate") != result["candidate"] or not isinstance(receipt.get("report"), dict)
                or result["inputs"] != req["inputs"] or receipt["report"].get("outcome") != result["outcome"]):
            raise factory.FactoryError(f"{job}: collect the original terminal native result")
        if args.write:
            selected_writer = (result["outcome"] == "completed" and factory.result_current(record, result)
                               and (receipt.get("commit") or {}).get("state") == "created")
        elif receipt["candidate"] != req["inputs"]["candidate"]:
            raise factory.FactoryError(f"{job}: reader candidate differs from the original request")
        # Replay the original collected result, including its exact artifacts and native context.
        import integration
        integration.collected_native(name, record, assignment, current=False, observe_exit=False)

    start, finish = receipt.get("created_at"), receipt.get("finished_at")
    if (not isinstance(start, (int, float)) or not isinstance(finish, (int, float))
            or isinstance(start, bool) or isinstance(finish, bool)
            or not 0 < start <= finish <= closed["at"]):
        raise factory.FactoryError(f"{job}: restore consistent attempt timestamps")
    return (start, closed["at"]), reader, selected_writer, (path, raw)


def terminal_attempts(record, directory, work, admin, *, native_aging):
    intervals = []
    readers = {}
    selected_writer = False
    receipts = {}
    for name, assignment in record["assignments"].items():
        interval, reader, selected, (path, raw) = terminal_attempt(
            record, directory, work, admin, name, assignment, native_aging=native_aging)
        intervals.append(interval)
        if reader:
            readers[name] = reader
        selected_writer |= selected
        receipts[path] = raw
    if not selected_writer:
        raise factory.FactoryError("Need a collected writer whose verified commit produced the selected task candidate and requirements")
    return intervals, readers, receipts


def matches_admin(admin, value, identity):
    path = Path(value)
    if not path.is_absolute():
        raise factory.FactoryError("Cannot verify a relative Git admin binding")
    try:
        return path.samefile(admin)
    except FileNotFoundError:
        # An expired canonical sibling under the original repository's worktrees
        # directory is distinct. A missing alias elsewhere proves nothing.
        if (factory.valid_repository_identity(identity)
                and path.parent == Path(identity["common_dir"]) / "worktrees" and not path.is_symlink()
                and factory.repository_identity(Path(identity["common_dir"]).parent) == identity):
            return False
        raise factory.FactoryError(f"Cannot verify missing Git admin binding at {path}") from None


def uses_checkout(work, admin, cwd):
    path = Path(cwd)
    if not path.is_absolute():
        raise factory.FactoryError("Cannot resolve an unbound working directory")
    if path.is_relative_to(work):
        return True
    target = linked_admin(path.resolve())
    return target is not None and target.samefile(admin)


def check_launch_stores(directory, admin):
    store = directory.stat()
    for link in admin.iterdir():
        if not link.name.startswith("factory-launch-store-"):
            continue
        if (link.name != f"factory-launch-store-{store.st_dev}-{store.st_ino}"
                or not link.is_symlink() or not link.samefile(directory)):
            raise factory.FactoryError("Another or unknown state store published launch intent for this Git directory")


def assignment_uses_checkout(assignment, work, admin, repository, label):
    req = assignment["request"]
    command = req["command"]
    if not isinstance(command, list) or not all(isinstance(arg, str) for arg in command):
        raise factory.FactoryError(f"Cannot rule out reuse; restore {label}")
    match = uses_checkout(work, admin, req["cwd"])
    bound = False
    for i, arg in enumerate(command):
        option, sep, value = arg.partition("=")
        if option != "--worktree-git-dir":
            continue
        if not sep and i + 1 < len(command):
            value = command[i + 1]
        if bound or not Path(value).is_absolute():
            raise factory.FactoryError(f"Restore unbound worktree request for {label}")
        bound = True
        match |= matches_admin(admin, value, repository)
    if not bound and not Path(req["cwd"]).is_dir():
        raise factory.FactoryError(f"Cannot rule out reuse by {label}; restore the original cwd")
    return match


def receipt_uses_checkout(receipt, work, admin):
    return (uses_checkout(work, admin, receipt["cwd"])
            or "checkout" in receipt and uses_checkout(work, admin, receipt["checkout"])
            or matches_admin(admin, receipt["worktree_git_dir"], receipt.get("repository_identity")))


def check_release_users(directory, owner, work, admin, witness):
    """Called under the physical launch lock, before removing the inode that carries it."""
    check_launch_stores(directory, admin)
    intervals = []
    user_records = {}
    for row, raw in records(directory):
        task = row["task"]
        repo = (row.get("admission") or {}).get("repository_identity")
        item = row.get("provisioning")
        if task != owner["task"] and item and (uses_checkout(work, admin, item["request"]["path"])
                or item.get("git_dir") and matches_admin(admin, item["git_dir"], repo)):
            raise factory.FactoryError(f"Reconcile conflicting provisioning by {task}")
        users = [name for name, assignment in row["assignments"].items()
                 if assignment_uses_checkout(assignment, work, admin, repo, f"{task}/{name}")]
        if not users:
            continue
        path = directory / (task + ".json")
        user_records[path] = raw
        checked = factory.validate_task(path, row)
        for name in users:
            assignment = checked["assignments"][name]
            req = assignment["request"]
            local = task == owner["task"] and Path(req["cwd"]).is_relative_to(work)
            launch = assignment.get("launch_checkout")
            if not local and (launch != witness or not Path(req["cwd"]).is_relative_to(work)):
                raise factory.FactoryError(f"Reconcile foreign use by {task}/{name} before release")
            harness = factory.worker_hint(req["command"])
            receipt_path = directory / f"{task}.{name}.{harness}.json"
            reset = factory.reset_patch(receipt_path)
            lost = (assignment.get("closed") or {}).get("state") == "lost"
            if local and harness and assignment["state"] == "launched" and (lost or os.path.lexists(reset)):
                command = req["command"]
                tail = command[4:] if command[1:3] == ["-E", "-s"] else command[2:]
                args = factory.parse_saved_args(importlib.import_module(harness + "_worker").parser(), tail)
                raw_receipt = factory.read_record_bytes(receipt_path, private=True)
                receipt = factory.decode_json(raw_receipt)
                if (args.receipt != receipt_path or args.job != f"{task}/{name}" or args.worktree_git_dir != admin
                        or receipt.get("kind") != harness + "-execution" or receipt.get("state") not in ("starting", "failed")
                        or receipt.get("job") != args.job or receipt.get("brief_sha256") != req["instructions_sha256"]
                        or receipt.get("cwd") != req["cwd"] or receipt.get("worktree_git_dir") != str(admin)
                        or receipt.get("repository_identity") != repo or receipt.get("inputs") != req["inputs"]):
                    raise factory.FactoryError(f"Restore the original stopped attempt for {task}/{name} before release")
                if not lost:
                    metadata_identity(reset, directory.stat().st_dev)
                user_records[receipt_path] = raw_receipt
                continue
            # A strictly collected owner reader can predate launch-time physical binding.
            # Preserve its stopped result for release without certifying it for cache retirement.
            ordinary = local and "--write" not in req["command"]
            interval, _, _, (_, raw_receipt) = terminal_attempt(checked, directory, work, admin, name, assignment,
                                                                 ordinary_release=ordinary)
            user_records[receipt_path] = raw_receipt
            intervals.append(interval)
    for path in directory.glob("*.*.*.json"):
        if path in user_records or len(path.name.split(".")) != 4:
            continue
        receipt = factory.decode_json(factory.read_record(path, private=True))
        if not isinstance(receipt, dict):
            raise factory.FactoryError(f"Inspect the unrecognized artifact {path.name} before release")
        if ("worktree_git_dir" in receipt or path.name.split(".")[-2] in factory.NATIVE_HARNESSES
                or str(receipt.get("kind", "")).endswith("-execution")) and receipt_uses_checkout(receipt, work, admin):
            raise factory.FactoryError(f"Reconcile orphaned native receipt {path.name} before release")
    process_check(work, admin, owner["task"], intervals, attended_release=True)
    return user_records


def check_reuse(directory, owner, original_raw, work, admin, readers):
    task = owner["task"]

    deliveries = {}
    for path in directory.iterdir():
        parts = path.name.split(".")
        if len(parts) != 4 or parts[3] != "json":
            continue
        assignment = owner["assignments"].get(parts[1])
        if parts[0] == task and assignment and factory.worker_hint(assignment["request"]["command"]) == parts[2]:
            continue  # terminal_attempts validated this exact receipt against the frozen request.
        raw = factory.read_record_bytes(path, private=True)
        receipt = factory.decode_json(raw)
        if parts[0] == task and parts[2] == "delivered" and TASK.fullmatch(parts[1]):
            # terminal_attempts already verified every closed Rex exit under the owner locks.
            if release_evidence.delivered_receipt(owner, parts[1])["receipt_digest"] != release_evidence.digest(receipt):
                raise factory.FactoryError(f"Delivery receipt changed during replay at {path}")
            deliveries[path] = raw
            continue
        if parts[2] not in factory.NATIVE_HARNESSES and not (
                isinstance(receipt, dict) and ("worktree_git_dir" in receipt
                                               or str(receipt.get("kind", "")).endswith("-execution"))):
            if parts[0] == task:
                raise factory.FactoryError(f"Inspect the unrecognized owner artifact {path.name}")
            continue
        if (parts[0] == task or str(receipt["job"]).partition("/")[0] == task
                or receipt_uses_checkout(receipt, work, admin)):
            raise factory.FactoryError(f"Reconcile conflicting or orphaned native receipt {path.name}")

    rows = {row["task"]: (row, raw) for row, raw in records(directory)}
    if os.path.lexists(directory / (task + ".worktree-release.json")):
        raise factory.FactoryError(f"Inspect the surviving writer checkout and its owner release receipt for {task}")
    if rows[task][1] != original_raw:
        raise factory.FactoryError("Owner record changed during census; retry")
    check_launch_stores(directory, admin)
    for name, (row, _) in rows.items():
        if name == task:
            continue
        repository = (row.get("admission") or {}).get("repository_identity")
        provision = row.get("provisioning")
        if provision and (uses_checkout(work, admin, provision["request"]["path"])
                          or provision.get("git_dir") and matches_admin(admin, provision["git_dir"], repository)):
            raise factory.FactoryError(f"Reconcile conflicting provisioning by {name}")
        for identity, assignment in row["assignments"].items():
            if (assignment_uses_checkout(assignment, work, admin, repository, f"{name}/{identity}")
                    or any(str(admin / "go-build") in arg for arg in assignment["request"]["command"])):
                raise factory.FactoryError(f"Inspect worktree reuse by {name}/{identity}")

    # Only an actual owner release covers removal of a separate assignment cwd.
    # A worker finalizing its own inner checkout provides no proof about this sibling.
    releases = {}
    for name, (cwd, linked, observed) in readers.items():
        owners = []
        for row, _ in rows.values():
            item = row.get("provisioning")
            if item and (Path(item["request"]["path"]).resolve() == cwd
                         or item["git_dir"] and Path(item["git_dir"]).resolve() == linked):
                owners.append(row)
        if not owners and observed is not None and not (observed["marker"] or "").startswith("factory-"):
            continue
        if len(owners) != 1:
            raise factory.FactoryError(f"Need one original owner release or unambiguous live ownership for reader {cwd}")
        path = directory / (owners[0]["task"] + ".json")
        raw = rows[owners[0]["task"]][1]
        released = factory.validate_task(path, owners[0])
        item = released["provisioning"]
        receipt_path = path.with_suffix(".worktree-release.json")
        if (released["admission"]["repository_identity"] != owner["admission"]["repository_identity"]
                or released["admission"]["repository"] != owner["admission"]["repository"]
                or item["state"] != "provisioned" or item["request"]["path"] != str(cwd) or item["git_dir"] != str(linked)):
            raise factory.FactoryError(f"Inspect conflicting provisioning for reader checkout {cwd}")
        witness = owner["assignments"][name].get("launch_checkout")
        marker = f'factory-{released["task"]}-{item["id"]}-{item["owner_token"]}'
        if not witness or witness.get("marker") != marker or not factory.original_checkout(item, witness):
            raise factory.FactoryError(f"Missing original provisioner ownership for reader checkout {cwd}")
        releases[path] = raw
        if observed is not None:
            if os.path.lexists(receipt_path):
                raise factory.FactoryError(f"Inspect the surviving reader checkout {cwd} and its owner release receipt")
            continue
        proof, _ = factory.release_proof(receipt_path, released)
        assignment = owner["assignments"][name]
        if (proof["checkout"] != witness or proof["at"] < assignment["closed"]["at"]
                or proof["head"] != assignment["result"]["candidate"]):
            raise factory.FactoryError(f"Restore the original owner release outcome for deregistered reader {cwd}")
        raise factory.FactoryError(f"Owner release confirms Git removal of {cwd}; direct-user exclusion "
                                   "during release has no independent receipt. Retain the dependent cache.")
    return releases, deliveries


def boot_time():
    if sys.platform == "darwin":
        output = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.boottime"], capture_output=True,
                                text=True, check=True, timeout=10).stdout
        match = re.match(r"\{ sec = (\d+),", output.strip())
    elif sys.platform == "linux":
        match = re.search(r"^btime (\d+)$", Path("/proc/stat").read_text(), re.MULTILINE)
    else:
        match = None
    if not match:
        raise factory.FactoryError("Cannot verify host boot time against terminal evidence")
    return int(match[1])


def inventory(command, timeout):
    # subprocess.run doesn't expose the observer's PID. Only these exact children
    # may be excluded from birth-time coverage of the user's process inventory.
    with subprocess.Popen(command, env={"LC_ALL": "C", "TZ": "UTC", "PATH": os.defpath},
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        try:
            listing, diagnostics = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        if process.returncode:
            raise factory.FactoryError(f"Cannot verify the user process inventory with {command[0]}")
        return process.pid, listing, diagnostics


def process_check(work, admin, task, intervals, *, attended_release=False):
    """Only an attended checkout release can rely on independent exclusion for incomplete process scope."""
    scope_gap = None
    if any(not str(p).isascii() or not str(p).isprintable() or "\\" in str(p) for p in (work, admin)):
        raise factory.FactoryError("Cannot match the worktree path unambiguously in the process listing")
    boot = boot_time()
    if not 0 < boot <= time.time():
        raise factory.FactoryError("Cannot verify host boot time against the observation clock")
    earliest = min((start for start, closed in intervals if closed >= boot), default=None)
    uid = str(os.getuid())
    ps_pid, listing, diagnostics = inventory(["/bin/ps", "-ww", "-U", uid, "-o", "pid=,lstart=,command="], 30)
    if diagnostics or not listing.strip():
        raise factory.FactoryError("Inspect incomplete ps observation before cleaning")
    pids = set()
    for line in listing.decode().splitlines():
        fields = line.split(None, 6)
        if len(fields) != 7 or not fields[0].isdigit():
            raise factory.FactoryError("Cannot read process identity and start time from ps")
        pid = int(fields[0])
        pids.add(pid)
        if pid in (os.getpid(), ps_pid):
            continue
        started = calendar.timegm(time.strptime(" ".join(fields[1:6]), "%a %b %d %H:%M:%S %Y"))
        argv = fields[6]
        if any(key in argv for key in (str(work), str(admin), "--job " + task + "/")):
            raise factory.FactoryError(f"Inspect matching command of process {pid}")
        if earliest is not None and started + 1 > earliest:
            scope_gap = f"Process {pid} could be a detached cache user; its exit is unproven"
    if os.getpid() not in pids:
        raise factory.FactoryError("Cannot verify the current user process inventory with ps")
    lsof_pid, listing, warnings = inventory(["/usr/sbin/lsof", "-nP", "-u", uid, "-F0pftn"], 60)
    # On Mini, lsof reports an inaccessible Time Machine SMB mount on a different
    # device. Require its complete mount-table diagnostic and complete local PID coverage.
    backup = re.compile(rb"lsof: WARNING: can't stat\(\) smbfs file system (/Volumes/\.timemachine/[^\n]+)\n"
                        rb' +Output information may be incomplete\.\n +assuming "dev=([0-9a-fA-F]+)" from mount table\n')
    while warnings:
        match = backup.match(warnings)
        if not match or int(match[2], 16) in (work.stat().st_dev, admin.stat().st_dev):
            raise factory.FactoryError("Incomplete lsof observation; inspect the user's lsof -nP listing and mount access")
        warnings = warnings[match.end():]
    if not listing or not listing.endswith(b"\0\n"):
        raise factory.FactoryError("Incomplete lsof process/file listing")
    observed = set()
    pid = None
    for entry in listing.split(b"\0\n"):
        if not entry:
            continue
        fields = {field[:1]: field[1:] for field in entry.split(b"\0")}
        if b"p" in fields:
            pid = int(fields[b"p"])
            if earliest is not None and pid not in pids and pid != lsof_pid:
                scope_gap = f"Cannot verify the birth time of newly observed process {pid}"
            pids.add(pid)
            continue
        if pid is None or b"f" not in fields:
            raise factory.FactoryError("Cannot read lsof process/file identities")
        path = Path(os.fsdecode(fields.get(b"n", b"")))
        kind = fields.get(b"t", b"").upper()
        if ((kind in (b"REG", b"DIR", b"VDIR", b"VREG") and not path.is_absolute())
                or b"UNKNOWN" in kind or re.search(rb"\((readlink|lstat|stat): [^)]*\)$", fields.get(b"n", b""))):
            raise factory.FactoryError(f"Cannot verify an open path of process {pid}")
        if fields[b"f"] == b"cwd" and kind == b"DIR" and path.is_absolute():
            observed.add(pid)
        if pid != os.getpid() and (path.is_relative_to(work) or path.is_relative_to(admin)):
            raise factory.FactoryError(f"Process {pid} has an open path in the linked worktree or admin directory")
    for missing in pids - observed:
        try:
            os.kill(missing, 0)
        except ProcessLookupError:
            continue
        scope_gap = f"Cannot verify working directory of process {missing}"
    if scope_gap:
        if not attended_release:
            raise factory.FactoryError(scope_gap)
        print(f"{scope_gap}. Release requires independent exclusion of all checkout and cache users.", file=sys.stderr)
    return dict(observed_processes=len(observed), host_boot=boot, first_possible_start_this_boot=earliest)


def check_native_cache_path(cache):
    # Validate from the root down so each pathname is reached through a trusted parent.
    for path in (*reversed(cache.parents), cache):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            # A trusted sticky directory protects the next component once its owner is checked.
            if info.st_uid not in (0, os.getuid()) or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
                raise factory.FactoryError(f"Retain cache under replaceable directory {path}; inspect owner and permissions")
            if sys.platform != "darwin":
                continue
            DARWIN.acl_get_fd.restype = ctypes.c_void_p
            acl = ctypes.c_void_p(DARWIN.acl_get_fd(fd))
            if not acl:
                if ctypes.get_errno() == errno.ENOENT:  # No extended ACL on this open inode.
                    continue
                raise factory.FactoryError(f"Cannot verify directory ACL at {path}")
            try:
                entry, tag = ctypes.c_void_p(), ctypes.c_int()
                index = 0  # ACL_FIRST_ENTRY
                while DARWIN.acl_get_entry(acl, index, ctypes.byref(entry)) == 0:
                    if DARWIN.acl_get_tag_type(entry, ctypes.byref(tag)) or tag.value != 2:  # ACL_EXTENDED_DENY
                        raise factory.FactoryError(f"Retain cache with an unverified or granting ACL at {path}")
                    index = -1  # ACL_NEXT_ENTRY
                if ctypes.get_errno() != errno.EINVAL:  # Darwin signals end of entries with EINVAL.
                    raise factory.FactoryError(f"Cannot verify directory ACL at {path}")
            finally:
                DARWIN.acl_free(acl)
        finally:
            os.close(fd)


def check_mounts(repo, work, cache, *, native_mount=None):
    # A same-device submount can hide under a shard. Check before opening the cache.
    local = False
    if sys.platform == "darwin":
        table = subprocess.run(["/sbin/mount"], capture_output=True, text=True, timeout=10, check=True)
        if table.stderr or not table.stdout.strip():
            raise factory.FactoryError("Cannot verify the complete mount table before inspecting the cache")
        mounts = []
        for line in table.stdout.splitlines():
            match = re.fullmatch(r".+? on (/.*?) \((.+)\)", line)
            if not match or line.count(" on ") != 1:
                raise factory.FactoryError("Cannot verify local mount paths; inspect the mount table")
            mounts.append(Path(match[1]))
            if native_mount and os.fsencode(match[1]) + b"\0" == native_mount[1]:
                local = "local" in match[2].split(", ")
    elif sys.platform == "linux":
        mounts = []
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            fields = line.split()
            if len(fields) < 6:
                raise factory.FactoryError("Cannot verify the mount namespace")
            mounts.append(Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4])))
            if fields[0] == native_mount:
                local = line.partition(" - ")[2].split(" ", 1)[0] in {"ext2", "ext3", "ext4", "xfs", "btrfs", "tmpfs"}
    else:
        raise factory.FactoryError("Cannot verify local mounts on this platform")
    work_mounts = {m for m in mounts if work.is_relative_to(m)}
    repo_mounts = {m for m in mounts if repo.is_relative_to(m)}
    if not work_mounts or work_mounts != repo_mounts or any(p == work or p.is_relative_to(cache) or p.is_relative_to(repo) and p in cache.parents
                         for p in mounts):
        raise factory.FactoryError("Unexpected mount at the worktree, Git directory or cache; inspect before cleanup")
    if native_mount is not None:
        if not local:
            raise factory.FactoryError("Cannot establish a local filesystem for native Go aging")
        check_native_cache_path(cache)


def mount_id(fd):
    """Identify an open object's mount."""
    if sys.platform == "linux":
        match = re.search(r"^mnt_id:\s*(\d+)$", Path(f"/proc/self/fdinfo/{fd}").read_text(), re.MULTILINE)
        if match:
            return match[1]
    elif sys.platform == "darwin":
        # fgetattrlist(2): ATTR_CMN_FSID and ATTR_VOL_INFO | ATTR_VOL_MOUNTPOINT.
        # The mount name offset is relative to its reference, after the length and fsid_t.
        attributes = struct.pack("=HHIIIII", 5, 0, 4, 0x80001000, 0, 0, 0)
        buffer = ctypes.create_string_buffer(4096)
        result = DARWIN.fgetattrlist(fd, attributes, buffer, len(buffer), 0)
        if not result:
            length, fsid, offset, size = struct.unpack_from("=I8siI", buffer)
            start = 12 + offset
            if 20 <= start < start + size <= length <= len(buffer):
                mountpoint = buffer.raw[start:start + size]
                if mountpoint.startswith(b"/") and mountpoint.endswith(b"\0"):
                    return fsid, mountpoint
    raise factory.FactoryError("Cannot verify filesystem mount identity")


def cache_contents(fd, mount, *, clean=False, size_only=False, entries=None):
    """Size and retire Go build entries only through verified open parent directories."""
    base = os.fstat(fd)
    if mount_id(fd) != mount or stat.S_IMODE(base.st_mode) != 0o700:
        raise factory.FactoryError("Inspect cache mount or permissions; expected private mode 0700")
    if sys.platform == "darwin":
        attr, value = factory.TIME_MACHINE_EXCLUSION
        expected = bytes.fromhex(value)
        buffer = ctypes.create_string_buffer(len(expected))
        if DARWIN.fgetxattr(fd, attr.encode(), buffer, len(buffer), 0, 0) != len(expected) or buffer.raw != expected:
            raise factory.FactoryError("Restore the sticky Time Machine exclusion before cleanup")

    latest, blocks, changed = base.st_mtime_ns, base.st_blocks, base.st_ctime_ns

    def visit(parent, parts):
        nonlocal latest, blocks, changed
        names = os.listdir(parent)
        if not parts and "README" not in names:
            raise factory.FactoryError("Inspect missing native Go cache signature")
        if len(parts) == 2 and len(names) > 1:
            raise factory.FactoryError("Inspect ambiguous cached executable")
        for name in names:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            folder = stat.S_ISDIR(info.st_mode)
            if folder:
                valid = (not parts and re.fullmatch("[0-9a-f]{2}", name)
                         or len(parts) == 1 and re.fullmatch(parts[0] + "[0-9a-f]{62}-d", name))
            else:
                valid = (stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and (
                    not parts and name in ("README", "trim.txt", "testexpire.txt", "log.txt")
                    or len(parts) == 1 and re.fullmatch(parts[0] + "[0-9a-f]{62}-[ad]", name)
                    or len(parts) == 2 and info.st_mode & 0o111))
            if not valid or info.st_uid != os.getuid() or info.st_dev != base.st_dev or info.st_mode & 0o022:
                raise factory.FactoryError(f"Inspect unexpected cache entry {name}")
            if entries is not None and len(parts) == 1:
                entries.add(name)
            latest = max(latest, info.st_mtime_ns)
            changed = max(changed, info.st_ctime_ns)
            blocks += info.st_blocks
            if size_only and not clean and parts and not folder:
                continue  # The first census pass only needs these leaf files' metadata.
            child = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | (os.O_DIRECTORY if folder else 0),
                            dir_fd=parent)
            try:
                if mount_id(child) != mount or not os.path.samestat(info, os.fstat(child)):
                    raise factory.FactoryError(f"Mount or identity changed at cache entry {name}")
                if folder:
                    visit(child, (*parts, name))
                elif not parts and name == "README" and not os.read(child, 1024).startswith(
                        b"This directory holds cached build artifacts from the Go build system.\n"):
                    raise factory.FactoryError("Inspect unexpected native Go cache signature")
                if clean and (folder or parts or name == "log.txt"):
                    if not os.path.samestat(os.stat(name, dir_fd=parent, follow_symlinks=False), info):
                        raise factory.FactoryError(f"Cache entry changed before removal at {name}")
                    (os.rmdir if folder else os.unlink)(name, dir_fd=parent)
            finally:
                os.close(child)

    visit(fd, ())
    return latest / 1_000_000_000, (blocks + 1) // 2, changed


def confirm_window(task):
    if not sys.stdin.isatty():
        raise factory.FactoryError("Removal requires an attended exclusive maintenance window through readback")
    print(f"Confirm independent exclusion for {task}: direct checkout and cache users have stopped, all stores "
          "and descendants are reconciled, and new users are excluded through removal, publication and readback. "
          "Enter the task ID:", file=sys.stderr, flush=True)
    if sys.stdin.readline().strip() != task:
        raise factory.FactoryError("Exclusive maintenance window unconfirmed; retain the checkout and cache")


def native_build(go, cache, lock_fds):
    parent = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()).resolve(strict=True)
    if any(os.path.lexists(p / ".git") for p in (parent, *parent.parents)):
        raise factory.FactoryError("Supply a temporary directory outside Git worktrees")
    environment = dict(PATH=os.defpath, GOCACHE=str(cache), GOENV="off", GOWORK="off", GO111MODULE="off",
                       GOPROXY="off", GOSUMDB="off", GOTOOLCHAIN="local", CGO_ENABLED="0")

    def run_go(seconds, *arguments, stdout):
        # Only Factory holds the write end. Even a hard stop wakes the lock holder.
        reader, writer = os.pipe()
        with os.fdopen(reader, "rb") as lifetime, os.fdopen(writer, "wb") as owner:
            with subprocess.Popen([sys.executable, "-I", "-S", "-B", Path(__file__).with_name("go_command.py"),
                                   str(seconds), go, *arguments], cwd=parent, env=environment, stdin=lifetime,
                                  stdout=stdout, stderr=subprocess.PIPE, text=True, pass_fds=lock_fds,
                                  start_new_session=True) as keeper:
                try:
                    output, errors = keeper.communicate()
                except KeyboardInterrupt:
                    owner.close()
                    keeper.wait()
                    raise
                return subprocess.CompletedProcess(keeper.args, keeper.returncode, output, errors)

    # GOENV=off skips user configuration. The distribution can still set build flags and a cache program.
    config = run_go(30, "env", "GOCACHEPROG", "GOFLAGS", stdout=subprocess.PIPE)
    if config.returncode:
        raise factory.FactoryError(f"Go env exited {config.returncode}: {config.stderr.strip()}")
    if config.stdout != "\n\n":
        raise factory.FactoryError("Select a Go installation with empty GOCACHEPROG and GOFLAGS defaults")
    return run_go(GO_BUILD_SECONDS, "build", "-trimpath", ".", stdout=subprocess.DEVNULL)


def census(directory, repo, clean_task=None, *, trim_go=None):
    if trim_go is not None:
        if clean_task is not None or not trim_go.is_absolute() or not trim_go.is_file() or not os.access(trim_go, os.X_OK):
            raise factory.FactoryError("Select native aging alone with an absolute executable --trim-go PATH")
        trim_go = trim_go.resolve(strict=True)
    environment = dict(factory.git_environment(), GIT_NO_LAZY_FETCH="1", GIT_ALLOW_PROTOCOL="",
                       GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0",
                       GIT_TRACE2="0", GIT_TRACE2_PERF="0", GIT_TRACE2_EVENT="0")
    with factory.git_execution(list, environment):
        return _census(directory, repo, clean_task, trim_go)


def _census(directory, repo, clean_task, trim_go):
    if clean_task is not None and not TASK.fullmatch(clean_task):
        raise factory.FactoryError("Select one Factory task slug for explicit cleanup")
    directory, repo = Path(directory), Path(repo)
    real_directory(directory)
    device = real_directory(repo).st_dev
    common = repo / ".git"
    real_directory(common, device)
    if factory.git_common_dir(repo) != common:
        raise factory.FactoryError("Supply the canonical primary repository path")
    base = common / "worktrees"
    try:
        real_directory(base, device)
    except FileNotFoundError:
        return []
    primary = os.open(repo, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        mount = mount_id(primary)
    finally:
        os.close(primary)
    state_problem = None
    try:
        rows = records(directory)
    except (factory.FactoryError, OSError) as exc:
        rows, state_problem = [], str(exc)
    report = []
    base_fd = os.open(base, os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if mount_id(base_fd) != mount:
            raise factory.FactoryError("Unexpected mount at the linked Git admin directory")
        names = sorted(os.listdir(base_fd))
    finally:
        os.close(base_fd)
    for name in names:
        admin = base / name
        cache = admin / "go-build"
        result: dict = dict(cache=str(cache), size_kib=None, decision="retained")
        try:
            real_directory(admin, device)
            try:
                real_directory(cache, device)
            except FileNotFoundError:
                continue
            owners = [(r, raw) for r, raw in rows if (r.get("provisioning") or {}).get("git_dir") == str(admin)]
            if clean_task and rows and not any(r["task"] == clean_task for r, _ in owners):
                continue
            work = Path(owners[0][0]["provisioning"]["request"]["path"]) if len(owners) == 1 else repo
            check_mounts(repo, work, cache, native_mount=mount if trim_go is not None else None)
            cache_fd = os.open(cache, os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                _, result["size_kib"], _ = cache_contents(cache_fd, mount, size_only=True)
            finally:
                os.close(cache_fd)
            if state_problem:
                raise factory.FactoryError(state_problem)
            if len(owners) != 1:
                raise factory.FactoryError("Need one provisioned task with this exact Git directory; inspect owner or reader checkout")
            first, original_raw = owners[0]
            task = result["task"] = first["task"]
            path = directory / (task + ".json")
            current = factory.validate_task(path, first)
            original = binding(current, repo, cache)
            work = original[0]
            if trim_go is not None and (trim_go.is_relative_to(work) or trim_go.is_relative_to(repo)):
                raise factory.FactoryError("Select a trusted Go installation outside the admitted repository and worktree")
            result.update(worktree=str(work), owner_record=str(path))
            # Preview and native aging exclude only the owner and physical resources.
            # Explicit retirement also holds the line lock.
            lock_paths = (directory / (task + ".lock"), admin, cache)
            if clean_task == task:
                lock_paths = (factory.execution_lock_path(directory, factory.task_line(current)), *lock_paths)
            with ExitStack() as locks:
                fds = [locks.enter_context(inode_lock(p, directory=p in (admin, cache))) for p in lock_paths]
                if factory.read_record_bytes(path, private=True) != original_raw:
                    raise factory.FactoryError("Owner or assignments changed during census; retry")
                if binding(current, repo, cache) != original:
                    raise factory.FactoryError("Line or linked worktree identity changed during census; retry")
                check_mounts(repo, work, cache)
                retirement_sample = None
                if trim_go is None:
                    retirement_sample = cache_contents(fds[-1], mount)
                    result.update(last_cache_write=retirement_sample[0], size_kib=retirement_sample[1])
                native_aging = trim_go is not None
                intervals, readers, receipts = terminal_attempts(current, directory, work, admin, native_aging=native_aging)
                terminal = max(closed for _, closed in intervals)
                result.update(last_terminal=terminal, attempts=len(current["assignments"]))
                if retirement_sample and retirement_sample[1] < MIN_KIB:
                    raise factory.FactoryError("Below the 1 GiB allocated-size floor")
                evidence = check_reuse(directory, current, original_raw, work, admin, readers)
                if retirement_sample:
                    if time.time() - max(terminal, retirement_sample[0]) < IDLE_SECONDS:
                        raise factory.FactoryError("Wait for 24 hours after the last terminal observation and cache write")
                    result.update(process_check(work, admin, task, intervals))
                if not os.path.samestat(cache.lstat(), os.fstat(fds[-1])):
                    raise factory.FactoryError("Cache identity changed before cleanup; retry census")
                if clean_task == task:
                    confirm_window(task)
                if clean_task == task or trim_go is not None:
                    if (factory.read_record_bytes(path, private=True) != original_raw
                            or terminal_attempts(current, directory, work, admin, native_aging=native_aging)
                            != (intervals, readers, receipts)):
                        raise factory.FactoryError("Owner, receipt or cache changed during observation; rerun the census")
                    if check_reuse(directory, current, original_raw, work, admin, readers) != evidence:
                        raise factory.FactoryError("Release evidence changed before cleanup; retry census")
                    for cwd, reader_admin, original_reader in readers.values():
                        if checkout_binding(cwd, reader_admin, current["admission"]["repository_identity"]) != original_reader:
                            raise factory.FactoryError(f"Reader worktree identity changed before cleanup at {cwd}; retry census")
                if clean_task == task or trim_go is not None:
                    check_mounts(repo, work, cache, native_mount=mount if trim_go is not None else None)
                    for owner_path, raw in (receipts | evidence[0] | evidence[1]).items():
                        if factory.read_record_bytes(owner_path, private=True) != raw:
                            raise factory.FactoryError(f"Reader owner changed during observation at {owner_path}")
                before = set()
                if trim_go is not None:
                    result["last_cache_write"], result["size_kib"], _ = cache_contents(fds[-1], mount, entries=before)
                if (factory.read_record_bytes(path, private=True) != original_raw or binding(current, repo, cache) != original or any(
                        not os.path.samestat(p.lstat(), os.fstat(fd)) for p, fd in zip(lock_paths, fds))):
                    raise factory.FactoryError("Worktree or lock identity changed immediately before cleanup; retry census")
                if trim_go is not None and not before:
                    result.update(decision="retained", reason="No native Go entries to age",
                                  removed_entries=0, retained_entries=0)
                elif trim_go is not None:
                    result.update(decision="failed")
                    try:
                        build = native_build(trim_go, cache, fds)
                        result["go_exit_code"] = build.returncode
                        if build.returncode:
                            raise factory.FactoryError(f"Go build exited {build.returncode}: {build.stderr.strip()}")
                    finally:
                        after = set()
                        _, after_kib, _ = cache_contents(fds[-1], mount, entries=after)
                        if binding(current, repo, cache) != original or any(
                                not os.path.samestat(p.lstat(), os.fstat(fd)) for p, fd in zip(lock_paths, fds)):
                            raise factory.FactoryError("Checkout or lock identity changed during native aging")
                        result.update(after_kib=after_kib, removed_entries=len(before - after), retained_entries=len(before & after))
                    result.update(decision="cleaned" if result["removed_entries"] else "retained",
                                  reason="Native Go build completed; compared cache entries before and after")
                elif clean_task == task:
                    final = cache_contents(fds[-1], mount)
                    if (final != retirement_sample or final[1] < MIN_KIB
                            or time.time() - max(terminal, final[0]) < IDLE_SECONDS):
                        raise factory.FactoryError("Cache changed during observation; rerun the census")
                    result.update(decision="failed")
                    cache_contents(fds[-1], mount, clean=True)
                    if not os.path.samestat(os.fstat(fds[-1]), cache.lstat()):
                        raise factory.FactoryError("Cache identity changed during cleanup")
                    _, result["after_kib"], _ = cache_contents(fds[-1], mount)
                    if result["after_kib"] >= MIN_KIB:
                        raise factory.FactoryError("Cache still exceeds the size floor; inspect the remaining entries")
                    result.update(decision="cleaned", reason="Attended cleanup completed and cache readback passed")
                else:
                    result.update(decision="eligible", reason="Owner, terminal exits, process, age and size checks passed. Retirement requires attended writer exclusion.")
        except (factory.FactoryError, OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
            if result["decision"] == "failed":
                result["reason"] = (f"Native Go aging failed ({exc}); inspect the cache before retrying" if trim_go is not None
                                    else f"Go cache cleanup may be partial ({exc}); rerun the census")
            else:
                result.update(decision="retained", reason=str(exc))
        report.append(result)
    return report
