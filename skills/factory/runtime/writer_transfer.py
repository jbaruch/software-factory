"""One-hop writer provenance transfer: export from source host, import on destination host.

The exported bundle authenticates source bytes through an operator-supplied digest; it is not
a host signature and cannot protect against a dishonest source host.  The destination proves
Git objects, repository, requirements and all new approvals independently.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

import factory

BUNDLE_SCHEMA = 1
BUNDLE_MAX_BYTES = 50 * 1024 * 1024  # 50 MiB hard limit on the archive file.
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def _require(condition, message):
    if not condition:
        raise factory.FactoryError(message)


def _read_private_bounded(path, max_bytes=BUNDLE_MAX_BYTES):
    """Read a private regular file without following symlinks, bounded in size."""
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise factory.FactoryError("Bundle path is not a regular file; use a plain file")
        if info.st_mode & 0o077:
            raise factory.FactoryError("Bundle file has insecure permissions; it must be 0600")
        if info.st_uid != os.getuid():
            raise factory.FactoryError("Bundle file is not owned by the current user")
        if info.st_nlink != 1:
            raise factory.FactoryError("Bundle file has multiple hard links; use a plain regular file")
        content = stream.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise factory.FactoryError("Bundle file exceeds the size limit; inspect the original source")
        return content


def _write_exclusive_private(path, content: bytes):
    """Write bytes to path exclusively at mode 0600; raises FileExistsError on collision."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _blob_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _acceptance_digest(acceptance_text: str) -> str:
    """Digest of the acceptance capture with 'Updated <timestamp>' lines normalised out."""
    normalised = re.sub(r"(?m)^Updated [^\n]+\n", "Updated <normalised>\n", acceptance_text)
    return hashlib.sha256(normalised.encode()).hexdigest()


def _git_object_type(repository, sha):
    """Return the object type string, or None if not found."""
    try:
        return factory.provisioning_git(repository, "cat-file", "-t", sha).strip()
    except factory.FactoryError:
        return None


def _git_commit_tree(repository, sha):
    try:
        return factory.provisioning_git(repository, "rev-parse", sha + "^{tree}").strip()
    except factory.FactoryError:
        return None


def _git_parent_sha(repository, sha):
    try:
        return factory.provisioning_git(repository, "log", "--pretty=%P", "-n", "1", sha).strip()
    except factory.FactoryError:
        return None


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_writer(path, *, assignment_id, to_host, to_task, out_path, whoami_host_id):
    """Export a settled writer as source history.  Runs under the task lock; writes no task record.

    Returns (source_host_id, sha256_hex) of the written bundle.
    """
    # Load the task record raw (before load_task would migrate it) to enforce schema 11.
    try:
        raw = factory.read_record(path)
    except OSError:
        raise factory.FactoryError("Cannot read source task record") from None
    try:
        record_raw = factory.decode_json(raw)
    except ValueError:
        raise factory.FactoryError("Cannot decode source task record") from None
    _require(isinstance(record_raw, dict), "Invalid source task record")
    _require(record_raw.get("schema_version") == 11,
             "Export requires schema 11 source; load_task would migrate older records")
    _require("writer_origin" not in record_raw, "Source already has writer_origin; it cannot be re-exported")

    # Now use load_task for full validation (it will return schema 11 record after migration check).
    record = factory.load_task(path)
    _require(record["schema_version"] == 11, "Source record migrated unexpectedly")
    _require("writer_origin" not in record, "Source already has writer_origin")

    _require(record["hold"] is not None,
             "Source task is not held; pause it before exporting")
    _require(record["admission"] is not None, "Source task must be admitted before export")

    source_host = record["rex"]["host_id"] if record["rex"] is not None else None
    _require(source_host is not None, "Source task has no pinned Rex identity; it was never launched")
    _require(source_host != to_host,
             "Source and destination host are the same; transfer requires a different host")

    # Every non-abandoned assignment must be terminally observed; none may be launching.
    for key, assignment in record["assignments"].items():
        if assignment["state"] == "abandoned":
            continue
        _require(assignment["state"] == "launched",
                 f"Assignment {key} is not in a launched state")
        _require(assignment.get("closed") is not None,
                 f"Assignment {key} has no terminal observation; observe it before exporting")
        closed_state = assignment["closed"]["state"]
        _require(closed_state in factory.TERMINAL_STATES,
                 f"Assignment {key} is not terminal (state={closed_state})")
        _require(assignment["state"] != "launching",
                 f"Assignment {key} is still launching; wait for it to complete")

    # Find the selected writer assignment.
    writer_assignment = record["assignments"].get(assignment_id)
    _require(writer_assignment is not None, f"Unknown writer assignment {assignment_id!r}")
    _require("--write" in writer_assignment["request"]["command"],
             f"Assignment {assignment_id} is not a writer (no --write flag)")

    # Apply --from-writer checks (abbreviated; full validation is in ready_runner).
    import integration as _integration
    _require(writer_assignment["state"] == "launched",
             "Writer assignment must be in launched state")
    _require(writer_assignment["result"] is not None,
             "Writer assignment has no result; collect it before exporting")
    _require(writer_assignment["result"]["outcome"] == "completed",
             "Writer assignment outcome is not completed")
    _require(writer_assignment.get("native_collected") is True,
             "Writer assignment native result is not collected")

    harness = factory.native_harness(writer_assignment["request"]["command"])
    _require(harness is not None, "Writer assignment is not an original native worker")
    assert harness is not None  # narrowed above by _require

    # Verify collected native (non-current OK for export).
    _integration.collected_native(assignment_id, record, writer_assignment, current=False)

    # Compute contributor contexts for ALL --write assignments (failed and earlier included).
    contributors = []
    for key, assignment in record["assignments"].items():
        if "--write" not in assignment["request"]["command"]:
            continue
        context = _integration.contributor_context(key, record, assignment)
        contributors.append({"id": key, "context": context})
    contributors.sort(key=lambda x: x["id"])

    # Read archival blobs.
    def _read_blob(path_str):
        try:
            content = Path(path_str).read_bytes()
        except OSError:
            raise factory.FactoryError(f"Cannot read archival blob {path_str}") from None
        return content

    # Receipt path from args.
    writer_command = writer_assignment["request"]["command"]
    harness_name = harness
    if harness_name in ("claude", "grok"):
        import importlib
        worker = importlib.import_module(harness_name + "_worker")
        args = worker.parser().parse_args(writer_command[4:])
        receipt_path = str(args.receipt)
        events_path = str(args.events)
    elif harness_name == "codex":
        import codex_worker
        offset = 4 if writer_command[1:3] == ["-E", "-s"] else 2
        args = codex_worker.parser().parse_args(writer_command[offset:])
        receipt_path = str(args.receipt)
        events_path = str(args.events)
    elif harness_name == "antigravity":
        import antigravity_worker
        args = antigravity_worker.parser().parse_args(writer_command[4:])
        receipt_path = str(args.receipt)
        events_path = str(args.events)
    else:
        raise factory.FactoryError("Unsupported harness for export: " + harness_name)

    receipt_bytes = _read_blob(receipt_path)
    events_bytes = _read_blob(events_path)
    brief_bytes = _read_blob(writer_assignment["brief"])
    assignment_json_bytes = json.dumps(writer_assignment, indent=2, sort_keys=True).encode()

    # Acceptance digest.
    acceptance_capture = record.get("acceptance")
    acceptance_digest = _acceptance_digest(acceptance_capture) if isinstance(acceptance_capture, str) else None

    # Effective limits and judge rounds.
    limits = factory.allowance_limits(record)
    used = {}
    judge_rounds = 0
    if limits is not None:
        for purpose in limits:
            used[purpose] = sum(1 for a in record["assignments"].values()
                                if a["request"].get("purpose") == purpose)
        judge_rounds = sum(1 for ev in record.get("allowance_events", []) if "judge" in ev)

    # GitHub origin.
    repo = record["admission"]["repository"]
    gh_origin = factory.github_origin(repo)
    _require(gh_origin is not None, "Cannot determine GitHub OWNER/REPO from repository remote.origin.url")

    # Writer result for the bundle.
    writer_result = writer_assignment["result"]
    candidate = writer_result["candidate"]
    original_inputs = writer_result["inputs"]
    # Parent and tree from the candidate commit.
    parent = _git_parent_sha(repo, candidate)
    tree = _git_commit_tree(repo, candidate)
    _require(parent is not None and tree is not None,
             "Cannot read parent/tree from candidate commit; ensure it is locally reachable")

    # Control revision = len(control_events).
    control_revision = len(record.get("control_events", []))

    bundle = {
        "schema": BUNDLE_SCHEMA,
        "source": {
            "task": record["task"],
            "line": factory.task_line(record),
            "assignment": assignment_id,
            "host_id": source_host,
            "github_origin": gh_origin,
            "hold": record["hold"],
            "control_revision": control_revision,
        },
        "destination": {
            "host_id": to_host,
            "task": to_task,
        },
        "candidate": candidate,
        "parent": parent,
        "tree": tree,
        "requirements": original_inputs["requirements"],
        "acceptance_digest": acceptance_digest,
        "contributors": contributors,
        "limits": limits,
        "used": used,
        "judge_rounds": judge_rounds,
        "blobs": {
            "assignment": {
                "digest": _blob_digest(assignment_json_bytes),
                "content": assignment_json_bytes.decode("utf-8"),
            },
            "receipt": {
                "path": receipt_path,
                "digest": _blob_digest(receipt_bytes),
                "content": receipt_bytes.decode("utf-8"),
            },
            "events": {
                "path": events_path,
                "digest": _blob_digest(events_bytes),
                "content": events_bytes.decode("utf-8"),
            },
            "brief": {
                "path": writer_assignment["brief"],
                "digest": _blob_digest(brief_bytes),
                "content": brief_bytes.decode("utf-8"),
            },
        },
    }

    bundle_bytes = json.dumps(bundle, indent=2, sort_keys=True).encode("utf-8")
    bundle_sha256 = hashlib.sha256(bundle_bytes).hexdigest()

    out_path = Path(out_path)
    _require(not str(out_path).startswith(str(factory.default_state_dir())),
             "Output file must be outside the state directory")

    if out_path.exists():
        # Idempotent replay: same bytes → same digest → return without error.
        existing = out_path.read_bytes()
        if hashlib.sha256(existing).hexdigest() == bundle_sha256:
            return source_host, bundle_sha256
        raise factory.FactoryError(
            "Output file already exists with different content; use a new output path or remove the conflicting file")

    _write_exclusive_private(out_path, bundle_bytes)
    return source_host, bundle_sha256


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_writer(path, *, file_path, from_host, sha256, whoami_host_id, state_dir, repository):
    """Import a writer bundle on the destination host.

    Operator supplies --from-host and --sha256 from the source export output; never read from file.
    Returns the updated task record.
    """
    _require(isinstance(from_host, str) and SLUG_RE.fullmatch(from_host.replace("-", "x")),
             "Supply a valid --from-host value from the export output")
    _require(isinstance(sha256, str) and SHA256_RE.fullmatch(sha256),
             "Supply a valid --sha256 value from the export output")

    # Bounded private no-follow read; digest before parsing.
    file_path = Path(file_path)
    try:
        raw = _read_private_bounded(file_path)
    except (OSError, factory.FactoryError) as exc:
        raise factory.FactoryError("Cannot read bundle file: " + str(exc)) from None

    observed_sha256 = hashlib.sha256(raw).hexdigest()
    _require(observed_sha256 == sha256,
             "Bundle SHA-256 does not match --sha256; use the exact value from the export output")

    # Parse.
    try:
        bundle = factory.decode_json(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise factory.FactoryError("Cannot decode bundle file; use the unmodified export output") from None

    _require(isinstance(bundle, dict), "Invalid bundle format")
    _require(bundle.get("schema") == BUNDLE_SCHEMA,
             f"Unsupported bundle schema {bundle.get('schema')!r}; use a compatible export")

    # Strict field checks.
    for section in ("source", "destination", "blobs"):
        _require(isinstance(bundle.get(section), dict),
                 f"Bundle missing or invalid {section!r} section")

    source = bundle["source"]
    dest = bundle["destination"]

    _require(isinstance(source.get("host_id"), str) and source["host_id"],
             "Bundle source host_id is missing or empty")
    _require(source["host_id"] == from_host,
             "Bundle source host_id does not match --from-host; supply the exact value from the export output")
    _require(isinstance(dest.get("host_id"), str) and dest["host_id"],
             "Bundle destination host_id is missing or empty")
    _require(whoami_host_id == dest["host_id"],
             "Connected Rex host differs from the bundle's destination host_id")
    _require(source["host_id"] != dest["host_id"],
             "Source and destination host are the same; transfer requires a different host")

    task_name = path.stem
    _require(dest.get("task") == task_name,
             f"Bundle destination task {dest.get('task')!r} does not match --task {task_name!r}")

    for key in ("candidate", "requirements", "parent", "tree"):
        _require(isinstance(bundle.get(key), str) and bundle[key],
                 f"Bundle missing or invalid {key!r}")
    _require(COMMIT_RE.fullmatch(bundle["candidate"]), "Bundle candidate is not a valid commit SHA")
    _require(COMMIT_RE.fullmatch(bundle["requirements"]), "Bundle requirements is not a valid commit SHA")
    _require(COMMIT_RE.fullmatch(bundle["parent"]), "Bundle parent is not a valid commit SHA")
    _require(COMMIT_RE.fullmatch(bundle["tree"]), "Bundle tree is not a valid tree SHA")

    _require(isinstance(bundle.get("contributors"), list),
             "Bundle contributors must be a list")
    for c in bundle["contributors"]:
        _require(isinstance(c, dict) and isinstance(c.get("id"), str) and isinstance(c.get("context"), str),
                 "Bundle contributor entry must have string id and context")

    # Load the destination task.
    record = factory.load_task(path)

    # Identical replay: already imported with same sha256 → return without error.
    if "writer_origin" in record:
        existing_wo = record["writer_origin"]
        _require(existing_wo.get("sha256") == sha256,
                 "Destination already has a different writer_origin; conflicting replay is not allowed")
        return record

    _require(record["admission"] is not None,
             "Admit the destination task before importing writer provenance")
    _require(not record["assignments"],
             "Destination task already has assignments; import requires a fresh admitted task")
    _require(record["rex"] is None,
             "Destination task already has a Rex pin; import requires an unpinned task")
    _require(record["provisioning"] is None,
             "Destination task already has provisioning; import requires a clean admitted task")

    # check_repository.
    factory.check_repository(record["admission"])

    # GitHub origin must match (case-insensitive, non-null).
    dest_repo = record["admission"]["repository"]
    dest_gh_origin = factory.github_origin(dest_repo)
    _require(dest_gh_origin is not None,
             "Destination task repository has no GitHub origin; transfer requires a GitHub repository")
    assert dest_gh_origin is not None  # narrowed above by _require
    source_gh_origin = source.get("github_origin")
    _require(isinstance(source_gh_origin, str) and source_gh_origin,
             "Bundle source github_origin is missing or empty")
    assert isinstance(source_gh_origin, str)  # narrowed above by _require
    _require(dest_gh_origin.lower() == source_gh_origin.lower(),
             f"Destination GitHub origin {dest_gh_origin!r} does not match source {source_gh_origin!r}")

    # Inputs must equal the bundle's candidate/requirements.
    task_inputs = record["inputs"]
    _require(task_inputs is not None,
             "Destination task has no inputs; run inputs before importing")
    _require(task_inputs.get("candidate") == bundle["candidate"],
             "Destination task candidate does not match bundle; run inputs with the exact bundle candidate")
    _require(task_inputs.get("requirements") == bundle["requirements"],
             "Destination task requirements do not match bundle; run inputs with the exact bundle requirements")

    # Acceptance digest check (when the source captured one).
    if bundle.get("acceptance_digest") is not None:
        dest_acceptance = record.get("acceptance")
        _require(isinstance(dest_acceptance, str),
                 "Destination task has no captured acceptance; select inputs to capture it, then import")
        assert isinstance(dest_acceptance, str)  # narrowed above by _require
        _require(_acceptance_digest(dest_acceptance) == bundle["acceptance_digest"],
                 "Destination acceptance digest does not match the source; verify the same acceptance issues")

    # Local Git object checks with GIT_NO_LAZY_FETCH=1 (via provisioning_git).
    for obj_sha, obj_type in [(bundle["candidate"], "commit"), (bundle["requirements"], "commit"),
                               (bundle["parent"], "commit"), (bundle["tree"], "tree")]:
        observed_type = _git_object_type(dest_repo, obj_sha)
        _require(observed_type == obj_type,
                 f"Git object {obj_sha[:12]}... ({obj_type}) not found in destination repository; "
                 "fetch the candidate commit before importing")

    # Verify that the candidate's parent and tree match the bundle.
    actual_parent = _git_parent_sha(dest_repo, bundle["candidate"])
    actual_tree = _git_commit_tree(dest_repo, bundle["candidate"])
    _require(actual_parent == bundle["parent"],
             "Candidate commit parent does not match bundle; use the exact candidate from the source export")
    _require(actual_tree == bundle["tree"],
             "Candidate commit tree does not match bundle; use the exact candidate from the source export")

    # Each destination effective limit no higher than source's.
    source_limits = bundle.get("limits")
    if source_limits is not None and isinstance(source_limits, dict):
        dest_limits = factory.allowance_limits(record)
        if dest_limits is not None:
            for purpose, src_lim in source_limits.items():
                dest_lim = dest_limits.get(purpose, 0)
                _require(dest_lim <= src_lim,
                         f"Destination {purpose} limit ({dest_lim}) exceeds source ({src_lim}); "
                         "lower the destination limit or raise the source before importing")

    # Build writer_origin.
    writer_origin = {
        "schema": BUNDLE_SCHEMA,
        "archive": None,  # Will be set after saving archive.
        "sha256": sha256,
        "source_host": source["host_id"],
        "source_task": source["task"],
        "source_assignment": source["assignment"],
        "destination_host": dest["host_id"],
        "candidate": bundle["candidate"],
        "requirements": bundle["requirements"],
        "contributors": bundle["contributors"],
        "used": bundle.get("used"),
        "judge_rounds": bundle.get("judge_rounds", 0),
    }

    # Save archive: TASK.writer-origin.bundle (not .json so state scanners skip it).
    archive_name = task_name + ".writer-origin.bundle"
    archive_path = path.parent / archive_name
    _save_archive(archive_path, raw, sha256, record)

    writer_origin["archive"] = str(archive_path)

    # Apply hold if not already held.
    hold_reason = f"writer-origin: source task {source['task']!r} on host {source['host_id']!r} is held"

    record["writer_origin"] = writer_origin
    record["schema_version"] = 12

    # Append hold control event unless already held.
    if record["hold"] is None:
        import time as _time
        import uuid as _uuid
        event_id = _uuid.uuid4().hex
        occurred = _time.time()
        record["control_events"].append(dict(
            id=event_id, expected_revision=None,
            previous_hold=None, hold=hold_reason, at=occurred))
        record["hold"] = hold_reason

    factory.save(path, record)
    return record


def _save_archive(archive_path, bundle_bytes: bytes, sha256: str, record):
    """Save the bundle archive exclusively; replay reuses an identical orphan."""
    if archive_path.exists():
        existing = archive_path.read_bytes()
        if hashlib.sha256(existing).hexdigest() == sha256:
            return  # Identical orphan from an interrupted previous run.
        raise factory.FactoryError(
            f"Conflicting archive already exists at {archive_path}; remove it and retry if it is a stale orphan")
    try:
        fd = os.open(str(archive_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(bundle_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(str(archive_path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError:
        # Race: check identity.
        existing = archive_path.read_bytes()
        if hashlib.sha256(existing).hexdigest() != sha256:
            raise factory.FactoryError("Conflicting archive written by a concurrent process") from None


def all_contributors(record):
    """Combined local --write contributor contexts plus any origin contributors.

    Returns sorted list of context strings.  Raises FactoryError if the origin archive is altered.
    """
    local = set()
    for assignment in record["assignments"].values():
        if "--write" in assignment["request"]["command"]:
            import integration as _integration
            local.add(_integration.contributor_context(
                list(record["assignments"].keys())[list(record["assignments"].values()).index(assignment)],
                record, assignment))

    origin_contexts = set()
    wo = record.get("writer_origin")
    if wo is not None:
        # Re-read the archive by digest; fail closed if missing or changed.
        archive_path = Path(wo["archive"])
        try:
            raw = archive_path.read_bytes()
        except OSError:
            raise factory.FactoryError(
                "Writer origin archive is missing; restore it before approvals can proceed") from None
        observed_sha256 = hashlib.sha256(raw).hexdigest()
        if observed_sha256 != wo["sha256"]:
            raise factory.FactoryError(
                "Writer origin archive has been altered; restore the original bundle before approvals can proceed")
        for c in wo.get("contributors", []):
            origin_contexts.add(c["context"])

    return local | origin_contexts
