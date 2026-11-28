"""One-hop writer provenance transfer: export from source host, import on destination host.

The exported bundle authenticates source bytes through an operator-supplied digest; it is not
a host signature and cannot protect against a dishonest source host.  The destination proves
Git objects, repository, requirements and all new approvals independently.
"""
from contextlib import contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import stat

import factory

BUNDLE_SCHEMA_VERSION = 1
BUNDLE_MAX_BYTES = 50 * 1024 * 1024  # 50 MiB hard limit on the archive file.
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def _require(condition, message):
    if not condition:
        raise factory.FactoryError(message)


@contextmanager
def _private_bundle(path):
    """Keep the verified descriptor open alongside its bounded contents."""
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
        content = stream.read(BUNDLE_MAX_BYTES + 1)
        if len(content) > BUNDLE_MAX_BYTES:
            raise factory.FactoryError("Bundle file exceeds the size limit; inspect the original source")
        yield content, descriptor


def _write_exclusive_private(path, content: bytes):
    """Write bytes to path exclusively at mode 0600; raises FileExistsError on collision."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    _sync_parent(path)


def _sync_parent(path):
    directory = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _blob_digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _acceptance_digest(record, requirements):
    capture = record.get("acceptance", {}).get(requirements)
    if capture is None:
        return None
    for ref in factory.acceptance_issues(record["admission"]):
        issue = re.escape("{}/{}#{}".format(*ref.groups()))
        header = re.compile(rf"(?m)^(### {issue}: [^\n]*\n)Updated [^\n]+\n\n")
        # A body can quote an issue header. Keep ambiguous text byte-exact.
        if len(header.findall(capture)) == 1:
            capture = header.sub(r"\1Updated <normalised>\n\n", capture)
    return _blob_digest(capture.encode())


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

def _writer_evidence_paths(path, key, assignment):
    command = assignment["request"]["command"]
    harness = (factory.worker_hint(command) if assignment.get("native_collected") is True or factory.launch_runtime(assignment)
               else factory.native_harness(command))
    if harness is None:
        raise factory.FactoryError(f"Restore the original native writer command for {key!r}.")
    events, receipt = factory.native_output_paths(path, path.stem, key, harness)
    brief = path.parent / f"{path.stem}.{key}.md"

    # Use the worker's interpretation of argv, including the historical Codex prefix.
    parser = importlib.import_module(harness + "_worker").parser()
    command = factory.native_argument_view(command)
    argv = command[4:] if command[1:3] == ["-E", "-s"] else command[2:]

    def damaged_command(_message=None):
        raise factory.FactoryError(f"Restore the original native writer command for {key!r}.")

    parser.allow_abbrev = False
    parser.error = damaged_command  # argparse diagnostics can echo a damaged saved argument.
    parser.print_help = damaged_command
    args = parser.parse_args(argv)
    _require(args.receipt == receipt and args.events == events and str(args.brief) == "{brief}"
             and assignment["brief"] == str(brief),
             f"Restore the original receipt, events and brief bindings for writer {key!r}.")
    return harness, {"receipt": receipt, "events": events, "brief": brief}


def _source_snapshot(path):
    try:
        return factory.read_record(path, exact=True)
    except OSError as exc:
        cause = exc.strerror or "expected a readable regular file"
        raise factory.FactoryError(
            f"Cannot read source task record {str(path)!r} ({cause}). "
            "Restore access to the original record before retrying.") from None
    except UnicodeDecodeError:
        raise factory.FactoryError(
            f"Invalid UTF-8 in source task record {str(path)!r}. Restore the original record before retrying.") from None


def export_writer(path, *, assignment_id, to_host, to_task, out_path):
    """Export a settled writer as source history.  Runs under the task lock; writes no task record.

    Returns (source_host_id, sha256_hex) of the written bundle.
    """
    snapshot = _source_snapshot(path)
    try:
        record = factory.decode_json(snapshot)
    except ValueError as exc:
        raise factory.FactoryError(
            f"Invalid JSON in source task record {str(path)!r} ({exc}). "
            "Restore the original record before retrying.") from None
    _require(isinstance(record, dict),
             f"Expected a JSON object in source task record {str(path)!r}. Restore the original record before retrying.")
    _require(record.get("schema_version") in (11, 13), "Export requires an ordinary schema 11 or 13 source")
    _require("writer_origin" not in record, "Source already has writer_origin; it cannot be re-exported")
    factory.validate_task_record(path, record)

    _require(record["hold"] is not None,
             "Source task is not held; pause it before exporting")
    _require(record["admission"] is not None, "Source task must be admitted before export")

    source_host = record["rex"]["host_id"] if record["rex"] is not None else None
    _require(isinstance(source_host, str) and SLUG_RE.fullmatch(source_host),
             "Export requires a pinned source Rex host ID that import can verify")
    _require(SLUG_RE.fullmatch(to_host) and SLUG_RE.fullmatch(to_task), "Supply a valid destination host and task")
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

    # Find the selected writer assignment.
    writer_assignment = record["assignments"].get(assignment_id)
    _require(writer_assignment is not None, f"Unknown writer assignment {assignment_id!r}")
    _require(factory.native_write_option(writer_assignment["request"]["command"]),
             f"Assignment {assignment_id} is not a writer (no --write flag)")

    # Re-read the original native evidence under the source host binding.
    import integration
    _require(writer_assignment["state"] == "launched",
             "Writer assignment must be in launched state")
    _require(writer_assignment["result"] is not None,
             "Writer assignment has no result; collect it before exporting")
    _require(writer_assignment["result"]["outcome"] == "completed",
             "Writer assignment outcome is not completed")
    _require(writer_assignment.get("native_collected") is True,
             "Writer assignment native result is not collected")

    factory.check_repository(record["admission"])
    harness, paths = _writer_evidence_paths(path, assignment_id, writer_assignment)
    historical = factory.native_harness(writer_assignment["request"]["command"]) is None

    # Read the originals before a provider can collapse read failures.
    def read_original(name, path):
        try:
            private = name == "receipt" or harness == "claude" or (historical and name == "brief")
            return factory.read_record(path, private=private, max_bytes=BUNDLE_MAX_BYTES)
        except OSError as exc:
            cause = (os.strerror(exc.errno) if exc.errno is not None else
                     "expected a readable regular file with the required permissions and size")
            raise factory.FactoryError(
                f"Cannot read archival file {str(path)!r} ({cause}). "
                "Restore access to the original file before retrying.") from None
        except UnicodeDecodeError:
            raise factory.FactoryError(
                f"Invalid UTF-8 in archival file {str(path)!r}. Restore the original file before retrying.") from None

    assignment_json = json.dumps(writer_assignment, indent=2, sort_keys=True)
    blobs = {"assignment": {"content": assignment_json, "digest": _blob_digest(assignment_json.encode())}}
    captured = {name: read_original(name, file) for name, file in paths.items()}
    for name, content in captured.items():
        blobs[name] = {"path": str(paths[name]), "content": content, "digest": _blob_digest(content.encode())}
    _require(blobs["brief"]["digest"] == writer_assignment["request"]["instructions_sha256"],
             f"Brief {str(paths['brief'])!r} differs from the frozen digest. Restore the original file before retrying.")
    proven, args, receipt, native = integration.collected_native(
        assignment_id, record, writer_assignment, current=False, captured=captured)
    target = factory.settled_writer_target(record, writer_assignment, args, receipt, native)
    selected_context = integration.native_context(proven, receipt)

    # Compute contributor contexts for ALL --write assignments (failed and earlier included).
    contributors = []
    for key, assignment in record["assignments"].items():
        command = assignment["request"]["command"]
        if not factory.native_write_option(command):
            if factory.local_launch_harness(assignment) is None:
                # The destination will have no local assignment to replay at its approval gate.
                integration.verify_omitted_reader(key, record, assignment)
            continue
        if key != assignment_id:
            _writer_evidence_paths(path, key, assignment)
        context = integration.contributor_context(key, record, assignment)
        _require(isinstance(context, str) and context.strip(), f"Unknown source contributor context: {key}")
        if key == assignment_id:
            _require(context == selected_context,
                     f"Contributor context for {key!r} differs from the verified receipt. Restore the original evidence.")
            context = selected_context
        contributors.append({"id": key, "context": context})
    contributors.sort(key=lambda x: x["id"])

    for name, file in paths.items():
        _require(read_original(name, file) == blobs[name]["content"],
                 f"Archival file {str(file)!r} changed during validation. Restore the original file before retrying.")

    # Effective limits and judge rounds.
    allowances = factory.task_view(record)["allowances"]
    limits = {purpose: counts["limit"] for purpose, counts in allowances.items()}
    used = {purpose: counts["used"] for purpose, counts in allowances.items()}
    judge_rounds = sum("judge" in event for event in record["allowance_events"])

    # GitHub origin.
    repo = record["admission"]["repository"]
    gh_origin = factory.github_origin(repo)
    _require(gh_origin is not None, "Cannot determine GitHub OWNER/REPO from repository remote.origin.url")

    # Writer result for the bundle.
    candidate = target["candidate"]
    # Parent and tree from the candidate commit.
    parent = _git_parent_sha(repo, candidate)
    tree = _git_commit_tree(repo, candidate)
    _require(parent is not None and tree is not None,
             "Cannot read parent/tree from candidate commit; ensure it is locally reachable")

    # Control revision = len(control_events).
    control_revision = len(record.get("control_events", []))

    bundle = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
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
        "requirements": target["requirements"],
        "acceptance_digest": _acceptance_digest(record, target["requirements"]),
        "contributors": contributors,
        "limits": limits,
        "used": used,
        "judge_rounds": judge_rounds,
        "blobs": blobs,
    }

    bundle_bytes = json.dumps(bundle, indent=2, sort_keys=True).encode("utf-8")
    _require(len(bundle_bytes) <= BUNDLE_MAX_BYTES, "Bundle exceeds the archive size limit")
    bundle_sha256 = _blob_digest(bundle_bytes)

    out_path = Path(out_path)
    _require(not out_path.resolve().is_relative_to(path.parent.resolve()),
             "Output file must be outside the state directory")

    replay = out_path.exists()
    if replay:
        with _private_bundle(out_path) as (existing, _):
            _require(_blob_digest(existing) == bundle_sha256,
                     "Output file already exists with different content; use a new output path or remove the conflicting file")

    _require(_source_snapshot(path) == snapshot,
             f"Source task record {str(path)!r} changed during export. Inspect the held source before retrying.")
    if not replay:
        _write_exclusive_private(out_path, bundle_bytes)
    return source_host, bundle_sha256


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_writer(path, *, file_path, from_host, sha256, whoami_host_id):
    """Import a writer bundle on the destination host.

    Operator supplies --from-host and --sha256 from the source export output; never read from file.
    Returns the updated task record.
    """
    _require(isinstance(from_host, str) and SLUG_RE.fullmatch(from_host),
             "Supply a valid --from-host value from the export output")
    _require(isinstance(sha256, str) and SHA256_RE.fullmatch(sha256),
             "Supply a valid --sha256 value from the export output")

    # Bounded private no-follow read; digest before parsing.
    file_path = Path(file_path)
    try:
        with _private_bundle(file_path) as (raw, _):
            observed_sha256 = _blob_digest(raw)
    except (OSError, factory.FactoryError) as exc:
        raise factory.FactoryError("Cannot read bundle file: " + str(exc)) from None

    _require(observed_sha256 == sha256,
             "Bundle SHA-256 does not match --sha256; use the exact value from the export output")

    # Parse.
    try:
        bundle = factory.decode_json(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise factory.FactoryError("Cannot decode bundle file; use the unmodified export output") from None

    _require(isinstance(bundle, dict) and set(bundle) == {
        "schema_version", "source", "destination", "candidate", "parent", "tree", "requirements", "acceptance_digest",
        "contributors", "limits", "used", "judge_rounds", "blobs"}, "Invalid writer bundle fields")
    source, dest, blobs = bundle["source"], bundle["destination"], bundle["blobs"]
    _require(isinstance(source, dict) and set(source) == {
        "host_id", "task", "assignment", "line", "hold", "control_revision", "github_origin"}
        and isinstance(source["hold"], str) and source["hold"].strip()
        and isinstance(source["line"], str) and factory.LINE.fullmatch(source["line"])
        and type(source["control_revision"]) is int and source["control_revision"] >= 0,
        "Invalid writer bundle source history")
    _require(isinstance(dest, dict) and set(dest) == {"task", "host_id"}, "Invalid bundle destination")

    archive_path = path.absolute().with_suffix(".writer-origin.bundle")
    writer_origin = _origin_fields(bundle, archive_path, sha256)
    validate_origin(writer_origin, path)
    _require(isinstance(blobs, dict) and set(blobs) == {"assignment", "receipt", "events", "brief"},
             "Invalid bundle archival blobs")
    for name, blob in blobs.items():
        fields = {"content", "digest"} if name == "assignment" else {"content", "digest", "path"}
        _require(isinstance(blob, dict) and set(blob) == fields and isinstance(blob["content"], str)
                 and ("path" not in fields or isinstance(blob["path"], str))
                 and _blob_digest(blob["content"].encode()) == blob["digest"], "Invalid archival blob: " + name)
    _require(bundle["acceptance_digest"] is None or isinstance(bundle["acceptance_digest"], str)
             and SHA256_RE.fullmatch(bundle["acceptance_digest"]), "Invalid acceptance digest")
    _require(source["host_id"] == from_host,
             "Bundle source host_id does not match --from-host; supply the exact value from the export output")
    _require(whoami_host_id == dest["host_id"],
             "Connected Rex host differs from the bundle's destination host_id")

    task_name = path.stem
    _require(dest.get("task") == task_name,
             f"Bundle destination task {dest.get('task')!r} does not match --task {task_name!r}")

    for key in ("parent", "tree"):
        _require(isinstance(bundle[key], str) and COMMIT_RE.fullmatch(bundle[key]), f"Invalid bundle {key}")
    source_limits = bundle["limits"]
    _require(isinstance(source_limits, dict) and set(source_limits) == set(writer_origin["used"])
             and all(type(limit) is int and limit >= 0 for limit in source_limits.values()),
             "Invalid bundle allowance limits")

    # Load the destination task.
    record = factory.load_task(path)

    if "writer_origin" in record:
        existing_wo = record["writer_origin"]
        _require(existing_wo.get("sha256") == sha256,
                 "Destination already has a different writer_origin; conflicting replay is not allowed")
        verified_origin(record)
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
        _require(_acceptance_digest(record, bundle["requirements"]) == bundle["acceptance_digest"],
                 "Destination acceptance digest does not match the source; verify the same acceptance issues")

    # Local Git object checks with GIT_NO_LAZY_FETCH=1 (via provisioning_git).
    objects = [(bundle["candidate"], "commit"), (bundle["parent"], "commit"), (bundle["tree"], "tree")]
    if COMMIT_RE.fullmatch(bundle["requirements"]):
        objects.append((bundle["requirements"], "commit"))
    for obj_sha, obj_type in objects:
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
    dest_limits = factory.allowance_limits(record)
    assert dest_limits is not None  # The destination is admitted.
    for purpose, limit in dest_limits.items():
        _require(limit <= source_limits[purpose],
                 f"Destination {purpose} limit ({limit}) exceeds source ({source_limits[purpose]}); "
                 "lower the destination limit or raise the source before importing")

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

    try:
        _save_archive(archive_path, raw, sha256)
        factory.save(path, record)
    except OSError as exc:
        cause = os.strerror(exc.errno) if exc.errno is not None else "storage error"
        raise factory.FactoryError(
            f"Cannot persist writer transfer for {str(path)!r} ({cause}). Restore access or space, "
            f"inspect any archive at {str(archive_path)!r}, then retry the identical bundle.") from None
    return record


def _save_archive(archive_path, bundle_bytes: bytes, sha256: str):
    """Save the bundle archive exclusively; replay reuses an identical private orphan."""
    try:
        _write_exclusive_private(archive_path, bundle_bytes)
    except FileExistsError:
        with _private_bundle(archive_path) as (content, descriptor):
            if _blob_digest(content) != sha256:
                raise factory.FactoryError(
                    f"Conflicting archive at {archive_path}; inspect the orphan before retrying") from None
            os.fsync(descriptor)
        _sync_parent(archive_path)


def _origin_fields(bundle, archive_path, sha256) -> dict:
    source = bundle["source"]
    return dict(schema_version=bundle["schema_version"], archive=str(archive_path), sha256=sha256,
                source_host=source["host_id"], source_task=source["task"], source_assignment=source["assignment"],
                destination_host=bundle["destination"]["host_id"], candidate=bundle["candidate"],
                requirements=bundle["requirements"], contributors=bundle["contributors"],
                used=bundle["used"], judge_rounds=bundle["judge_rounds"])


def validate_origin(origin, path):
    """Validate the flat schema before task loading uses the inherited allowance counters."""
    fields = {"schema_version", "archive", "sha256", "source_host", "source_task", "source_assignment",
              "destination_host", "candidate", "requirements", "contributors", "used", "judge_rounds"}
    if (not isinstance(origin, dict) or set(origin) != fields
            or type(origin["schema_version"]) is not int or origin["schema_version"] != BUNDLE_SCHEMA_VERSION
            or origin["archive"] != str(path.absolute().with_suffix(".writer-origin.bundle"))
            or not isinstance(origin["sha256"], str) or not SHA256_RE.fullmatch(origin["sha256"])
            or any(not isinstance(origin[k], str) or not SLUG_RE.fullmatch(origin[k])
                   for k in ("source_host", "destination_host", "source_task", "source_assignment"))
            or origin["source_host"] == origin["destination_host"]
            or not factory.valid_inputs(origin)
            or not isinstance(origin["used"], dict) or set(origin["used"]) != {"correction", "investigation"}
            or any(type(n) is not int or n < 0 for n in origin["used"].values())
            or type(origin["judge_rounds"]) is not int or not 0 <= origin["judge_rounds"] <= factory.JUDGE_ROUNDS
            or not isinstance(origin["contributors"], list)
            or any(not isinstance(c, dict) or set(c) != {"id", "context"}
                   or not isinstance(c["id"], str) or not SLUG_RE.fullmatch(c["id"])
                   or not isinstance(c["context"], str) or not c["context"].strip() for c in origin["contributors"])):
        raise factory.FactoryError("Invalid writer_origin; restore the original imported task record")
    ids = [c["id"] for c in origin["contributors"]]
    if ids != sorted(set(ids)) or origin["source_assignment"] not in ids:
        raise factory.FactoryError("Invalid writer_origin contributor history; restore the original imported record")


def verified_origin(record) -> dict:
    """Recheck archived source history at a gate, including for an already loaded record."""
    origin = record.get("writer_origin")
    if record.get("schema_version") != 12 or not isinstance(origin, dict) or not isinstance(origin.get("archive"), str):
        raise factory.FactoryError("Invalid writer_origin; restore the original imported record")
    archive = Path(origin["archive"])
    validate_origin(origin, archive.parent / (record["task"] + ".json"))
    # Approval callers hold a record in memory. Its local assignment briefs name the task directory.
    if any(Path(item["brief"]).parent != archive.parent for item in record["assignments"].values()):
        raise factory.FactoryError("Writer origin archive is outside the task directory; restore the imported record")
    try:
        with _private_bundle(archive) as (raw, _):
            if _blob_digest(raw) != origin["sha256"]:
                raise factory.FactoryError("Writer origin archive has been altered; restore the original bundle")
    except OSError as exc:
        raise factory.FactoryError(f"Cannot read writer origin archive; restore the original bundle: {exc}") from None
    try:
        bundle = factory.decode_json(raw)
        expected = _origin_fields(bundle, archive, origin["sha256"])
        destination_task = bundle["destination"]["task"]
    except (ValueError, KeyError, TypeError):
        raise factory.FactoryError("Malformed writer origin archive; restore the original bundle") from None
    if (destination_task != record["task"] or origin != expected
            or record["rex"] is not None and record["rex"]["host_id"] != expected["destination_host"]):
        raise factory.FactoryError("Task writer_origin differs from its archive; restore the original imported record")
    return expected
