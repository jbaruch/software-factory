"""One explicit native Codex assignment; retain its original events and compact result."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import time

sys.dont_write_bytecode = True

import factory

NATIVE_LINE_LIMIT = 1024 * 1024


class WorkerError(ValueError):
    pass


def native_object(pairs):
    # Observed CLI web-search telemetry repeats its item and execution IDs.
    # Neither ID supplies evidence or authority; retain the raw line and omit both.
    if sum(key == "id" for key, _ in pairs) > 1 and dict(pairs).get("type") == "web_search":
        return factory.unique_object([(key, value) for key, value in pairs if key != "id"])
    return factory.unique_object(pairs)


def outcome_schema():
    return dict(type="object", additionalProperties=False,
                properties=dict(outcome=dict(type="string", enum=sorted(factory.RESULT_OUTCOMES)),
                                artifacts=dict(type="array", items=dict(type="string"))),
                required=["outcome", "artifacts"])


def parse_events(events) -> dict:
    """A native completion is a carrier for a specialist report, never task acceptance."""
    thread, phase, message, usage = None, "before-thread", None, None
    for event in events:
        if not isinstance(event, dict):
            raise WorkerError("Native output is not an event object; inspect the retained stream")
        kind = event.get("type")
        if kind == "thread.started":
            if phase != "before-thread" or not isinstance(event.get("thread_id"), str) or not event["thread_id"]:
                raise WorkerError("Native worker returned an ambiguous thread; inspect its original events")
            thread = event["thread_id"]
            phase = "before-turn"
        elif kind == "turn.started":
            if phase != "before-turn":
                raise WorkerError("Native lifecycle is out of order; reconcile the existing attempt")
            phase = "active"
        elif kind in {"turn.failed", "error"}:
            raise WorkerError("Native worker failed; inspect its original events before another attempt")
        elif isinstance(kind, str) and kind.startswith("item."):
            item = event.get("item")
            # The native CLI emits nonfatal startup diagnostics after thread start.
            # They carry no report, turn or completion evidence.
            if phase == "before-turn" and kind == "item.completed" and isinstance(item, dict) and item.get("type") == "error":
                continue
            if phase != "active":
                raise WorkerError("Native lifecycle is out of order; reconcile the existing attempt")
            if kind == "item.completed":
                if not isinstance(item, dict):
                    raise WorkerError("Native item is malformed; inspect the retained stream")
                if item.get("type") == "agent_message":
                    message = item.get("text")
        elif kind == "turn.completed":
            if phase != "active":
                raise WorkerError("Native lifecycle is out of order; reconcile the existing attempt")
            phase = "finished"
            usage = event.get("usage")
    if phase != "finished":
        raise WorkerError("Native completion is missing or ambiguous; reconcile the existing attempt")
    if not isinstance(message, str):
        raise WorkerError("Native final message is missing; inspect its original events")
    try:
        report = factory.decode_json(message)
    except (ValueError, TypeError):
        raise WorkerError("Native final message is not a structured result; inspect its original events") from None
    if (not isinstance(report, dict) or set(report) != {"outcome", "artifacts"}
            or not isinstance(report["outcome"], str) or report["outcome"] not in factory.RESULT_OUTCOMES
            or not isinstance(report["artifacts"], list) or not report["artifacts"]
            or any(not isinstance(ref, str) or not ref.strip() for ref in report["artifacts"])):
        raise WorkerError("Native result needs a supported outcome and original evidence references")
    if usage is not None and (not isinstance(usage, dict)
            or any(type(usage.get(key)) is not int or usage[key] < 0
                   for key in ("input_tokens", "output_tokens"))):
        raise WorkerError("Native usage is malformed; retain unknown consumption instead of inventing it")
    return dict(thread_id=thread, report=report, usage=usage)


def read_events(path):
    """Read versioned local records without changing the original native lines."""
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        raise WorkerError("Cannot read native events; restore the original stream and its access before collection") from None
    events = []
    for line in lines:
        try:
            record = factory.decode_json(line)
        except (ValueError, UnicodeError):
            raise WorkerError("Malformed native events; inspect the retained stream before collecting or retrying") from None
        if (not isinstance(record, dict) or type(record.get("schema_version")) is not int
                or record["schema_version"] != 1 or record.get("kind") != "codex-event-line"
                or not isinstance(record.get("data"), str)):
            raise WorkerError("Unsupported native event record; inspect the original stream and update the adapter")
        try:
            record["data"].encode("utf-8")  # Invalid native bytes remain escaped evidence, never valid events.
            events.append(factory.decode_json(record["data"], object_pairs_hook=native_object))
        except (ValueError, UnicodeError):
            raise WorkerError("Malformed native events; inspect the retained stream before collecting or retrying") from None
    return events


async def stop_child(process):
    if process.returncode is not None:
        return
    try:
        process.send_signal(signal.SIGINT)
    except ProcessLookupError:
        await process.wait()
        return  # The owned process already exited.
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass  # The owned child exited before the signal; still observe its exit below.
        await process.wait()


def commit_changes(args, receipt):
    """Commit an opted-in writer's finished work outside the native sandbox."""
    if factory.git_commit(".", "HEAD") != args.candidate:
        raise factory.FactoryError("Writer changed Git HEAD before the factory commit; inspect the original candidate and retained work without retrying")
    if factory.repository_identity(".") != receipt["repository_identity"] or factory.git_dir(".") != args.worktree_git_dir:
        raise factory.FactoryError("Writer repository/worktree changed; restore the original checkout before inspecting its commit")
    root = Path(factory.git_worktree("."))
    environment = factory.command_environment()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    receipt["commit"] = dict(state="preparing", parent=args.candidate, message=args.commit_message,
                             staged_tree=None, candidate=None)
    factory.save(args.receipt, receipt)
    # Even status can execute configured clean filters; retain intent first.
    status = factory.git_run(".", "status", "--porcelain=v1", "--untracked-files=all", "--ignored", environment=environment)
    if status.returncode or any(line.startswith("!!") for line in status.stdout.splitlines()):
        raise factory.FactoryError("Writer left ignored outputs or unreadable Git status; inspect and remove only its disposable outputs before authorizing a correction")
    paths = factory.git_run(".", "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    if paths.returncode:
        raise factory.FactoryError("Cannot inspect writer source paths; restore Git access before staging changes")
    for name in paths.stdout.split("\0"):
        if not name:
            continue
        try:
            info = (root / name).lstat()
        except FileNotFoundError:
            continue  # Tracked deletions are part of the writer's change.
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise factory.FactoryError("Writer contains linked or non-regular source; inspect the retained changes and use independent regular files")
    staged = factory.git_run(".", "add", "--all", "--", ".", environment=environment)
    if staged.returncode:
        raise factory.FactoryError("Cannot stage writer changes; inspect the retained commit intent, index and worktree before correcting it")
    entries = factory.git_run(".", "ls-files", "--stage", "-z")
    if entries.returncode:
        raise factory.FactoryError("Cannot inspect staged writer paths; restore Git access and inspect the retained attempt")
    try:
        for entry in entries.stdout.split("\0"):
            if not entry:
                continue
            metadata, name = entry.split("\t", 1)
            mode, _, stage = metadata.split()
            info = (root / name).lstat()
            if mode not in {"100644", "100755"} or stage != "0" or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise factory.FactoryError("Writer staged linked or non-regular source; inspect the retained changes and use independent regular files")
    except ValueError:
        raise factory.FactoryError("Cannot decode the writer index; inspect Git compatibility and the retained changes before committing") from None
    tree = factory.git_run(".", "write-tree", environment=environment)
    if tree.returncode:
        raise factory.FactoryError("Cannot record the staged writer tree; inspect the retained index and commit intent")
    receipt["commit"].update(state="committing", staged_tree=tree.stdout.strip())
    factory.save(args.receipt, receipt)
    unchanged = factory.git_run(".", "diff", "--cached", "--quiet", "--exit-code", args.candidate)
    if unchanged.returncode == 0:
        receipt["commit"].update(state="unchanged", candidate=args.candidate)
        factory.save(args.receipt, receipt)
        return
    if unchanged.returncode != 1:
        raise factory.FactoryError("Cannot compare staged writer changes; inspect the retained index before another action")
    try:
        committed = factory.git_run(".", "commit", "--message", args.commit_message, environment=environment)
    except factory.FactoryError:
        raise factory.FactoryError("Writer commit outcome is uncertain; inspect the retained commit intent, Git HEAD and hooks before another action. No commit was retried") from None
    if committed.returncode:
        raise factory.FactoryError("Writer commit failed; inspect the retained commit intent, Git HEAD and configured hooks/signing before authorizing a correction. No commit was retried")
    candidate = factory.git_commit(".", "HEAD")
    parent = factory.git_run(".", "show", "-s", "--format=%P", candidate)
    if parent.returncode or parent.stdout.strip() != args.candidate:
        raise factory.FactoryError("Writer commit parent differs; inspect the original worktree and commit intent before continuing")
    receipt["commit"].update(state="created", candidate=candidate)
    factory.save(args.receipt, receipt)


def check_commit_receipt(receipt, args, repository):
    """Version 4 has no factory commit; version 5 declares its nullable intent."""
    commit = receipt.get("commit")
    if receipt["schema_version"] == 4:
        if "commit" in receipt or args.commit_message is not None:
            raise factory.FactoryError("Legacy receipt cannot supply a factory commit; inspect the original attempt without rewriting or relaunching it")
        return
    expected = args.commit_message is not None and receipt["report"]["outcome"] == "completed"
    if "commit" not in receipt or (commit is None and expected):
        raise factory.FactoryError("Factory commit evidence is missing; inspect the original receipt and Git HEAD before collection")
    if commit is None:
        return
    if (not expected or not args.write or not isinstance(commit, dict)
            or set(commit) != {"state", "parent", "message", "staged_tree", "candidate"}
            or commit.get("state") not in {"created", "unchanged"}
            or commit.get("parent") != receipt["inputs"]["candidate"]
            or commit.get("candidate") != receipt["candidate"]
            or commit.get("message") != args.commit_message
            or not isinstance(commit.get("staged_tree"), str)
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit["staged_tree"])
            or (commit["state"] == "unchanged") != (commit["candidate"] == commit["parent"])):
        raise factory.FactoryError("Factory commit evidence differs from this assignment; inspect its original intent and Git history")
    if commit["state"] == "created":
        parent = factory.git_run(repository, "show", "-s", "--format=%P", commit["candidate"])
        if parent.returncode or parent.stdout.strip() != commit["parent"]:
            raise factory.FactoryError("Factory commit parent differs from the assignment; inspect its original Git history before collection")


async def execute(args, *, spawn=asyncio.create_subprocess_exec, clock=time.time, timer=time.monotonic):
    if args.receipt.exists() or args.events.exists():
        raise factory.FactoryError("Native attempt already has artifacts; collect or inspect it, never relaunch it blindly")
    try:
        binary = args.codex.resolve(strict=True)
    except OSError:
        raise factory.FactoryError(f"Cannot access native CLI {str(args.codex)!r}; inspect/restore the saved executable before authorizing another attempt") from None
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise factory.FactoryError(f"Native CLI {str(binary)!r} is not executable; inspect/restore the saved executable before authorizing another attempt")
    if type(args.seconds) is not int or not 1 <= args.seconds <= factory.MAX_NATIVE_SECONDS:
        raise factory.FactoryError(f"Supply a positive native execution allowance of at most {factory.MAX_NATIVE_SECONDS} seconds before creating an attempt")
    factory.check_commit_message(args.commit_message, write=args.write)
    if os.environ.get("CODEX_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        raise factory.FactoryError("Remove API-key overrides from this worker environment and use native ChatGPT sign-in")
    try:
        instructions = args.brief.read_text()
    except (OSError, UnicodeDecodeError):
        raise factory.FactoryError(f"Cannot read original brief {str(args.brief)!r}; inspect/restore the saved assignment input before authorizing another attempt") from None
    digest = hashlib.sha256(instructions.encode()).hexdigest()
    if digest != args.brief_sha256:
        raise factory.FactoryError("Assignment brief changed; inspect the original task before launching")
    try:
        identity = factory.decode_json(args.repo_identity)
    except (ValueError, TypeError):
        raise factory.FactoryError("Cannot decode repository identity; inspect the original assignment before another attempt") from None
    if not factory.valid_repository_identity(identity):
        raise factory.FactoryError("Invalid repository identity; inspect the original assignment before another attempt")
    if not args.worktree_git_dir.is_absolute():
        raise factory.FactoryError("Restore the saved absolute worktree Git directory before launching")
    factory.check_checkout(str(args.repo), str(args.cwd), args.candidate, expected_identity=identity,
                           expected_git_dir=args.worktree_git_dir)
    factory.check_checkout(".", ".", args.candidate, expected_identity=identity,
                           expected_git_dir=args.worktree_git_dir)
    schema = args.receipt.with_suffix(".schema.json")
    # The receipt is this native effect's before-start intent, separate from the Rex launch.
    receipt = dict(schema_version=5, kind="codex-execution", job=args.job, state="starting",
                   inputs=dict(candidate=args.candidate, requirements=args.requirements),
                   repository_identity=identity,
                   worktree_git_dir=str(args.worktree_git_dir),
                   brief_sha256=digest, cwd=str(args.cwd), requested_model=args.model,
                   requested_effort=args.effort, binary=str(binary), writer=args.write,
                   created_at=clock(), finished_at=None,
                   elapsed_seconds=None, exit_code=None, thread_id=None, report=None, usage=None,
                   candidate=None, events=str(args.events), error=None, commit=None)
    # Claim the effect before writing any artifact; a concurrent caller cannot replace this receipt.
    fd = os.open(args.events, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        factory.save(schema, outcome_schema())
        factory.save(args.receipt, receipt)
    except OSError:
        os.close(fd)
        raise
    started, process = timer(), None
    events = []
    try:
        with os.fdopen(fd, "w") as stream:
            if args.commit_message is not None:
                instructions += "\nFactory commit mode: edit and test only; do not stage or commit Git changes. The factory commits after a completed report. Keep temporary outputs outside this checkout.\n"
            try:
                process = await spawn(str(binary), "--no-daemon", "exec", "--disable", "multi_agent", "--disable", "multi_agent_v2",
                                      "--json", "--color", "never",
                                      "--output-schema", str(schema), "--model", args.model,
                                      "--sandbox", "workspace-write" if args.write else "read-only",
                                      "-c", "model_reasoning_effort=" + json.dumps(args.effort),
                                      "--cd", ".", instructions,
                                      env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                      limit=NATIVE_LINE_LIMIT,
                                      stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE)
            except OSError:
                raise factory.FactoryError("Cannot start the saved native CLI; restore executable access/local process resources and inspect the retained attempt before authorizing another ID") from None

            async def consume():
                while True:
                    try:
                        line = await process.stdout.readline()
                    except ValueError:
                        raise factory.FactoryError("Native event exceeded the 1 MiB line limit; inspect the existing Rex pane and retained stream, then reduce command output or update the adapter before authorizing another attempt") from None
                    if not line:
                        break
                    text = line.decode("utf-8", errors="surrogateescape")
                    stream.write(json.dumps(dict(schema_version=1, kind="codex-event-line", data=text)) + "\n")
                    stream.flush()
                    try:
                        line.decode("utf-8")
                        event = factory.decode_json(text, object_pairs_hook=native_object)
                    except (UnicodeDecodeError, ValueError):
                        raise factory.FactoryError("Malformed native event; inspect the retained stream and update the adapter before another attempt") from None
                    print(text, end="", flush=True)
                    if not isinstance(event, dict):
                        raise factory.FactoryError("Native output is not an event object; inspect the retained stream")
                    events.append(event)
                    if (event.get("type") == "thread.started" and receipt["thread_id"] is None
                            and isinstance(event.get("thread_id"), str) and event["thread_id"]):
                        receipt["thread_id"] = event["thread_id"]
                return await process.wait()

            receipt["exit_code"] = await asyncio.wait_for(consume(), timeout=args.seconds)
            os.fsync(stream.fileno())
        if receipt["exit_code"] != 0:
            raise factory.FactoryError("Native Codex exited unsuccessfully; inspect the retained events and Rex pane")
        receipt.update(parse_events(events))
        if args.commit_message is not None and receipt["report"]["outcome"] == "completed":
            commit_changes(args, receipt)
        candidate = factory.git_commit(".", "HEAD")
        factory.check_checkout(".", ".", candidate, expected_identity=identity,
                               expected_git_dir=args.worktree_git_dir)
        if not args.write and candidate != args.candidate:
            raise factory.FactoryError("Read-only assignment's candidate changed; do not reuse its verification")
        receipt.update(candidate=candidate, state="completed")
    except TimeoutError:
        receipt.update(state="failed", error="Native execution allowance expired; inspect the existing attempt")
    except OSError:
        receipt.update(state="failed", error="Native process/event-stream I/O failed; inspect the retained stream, storage and Rex pane, restore access/space and reconcile this attempt before another launch")
    except (factory.FactoryError, ValueError, KeyError, TypeError) as exc:
        receipt.update(state="failed", error=str(exc))
    finally:
        if process is not None:
            await stop_child(process)
            receipt["exit_code"] = process.returncode
        receipt.update(finished_at=clock(), elapsed_seconds=timer() - started)
        factory.save(args.receipt, receipt)
    return receipt


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    for field in ("codex", "repo", "cwd", "brief", "events", "receipt", "worktree-git-dir"):
        command.add_argument("--" + field, type=Path, required=True)
    for field in ("job", "candidate", "requirements", "brief-sha256", "model", "effort", "repo-identity"):
        command.add_argument("--" + field, required=True)
    command.add_argument("--write", action="store_true")
    command.add_argument("--commit-message", help="With --write, let the factory stage/commit a completed result using this message")
    command.add_argument("--seconds", type=int, default=600, help="Initial execution allowance; no automatic retry")
    return command


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        receipt = asyncio.run(execute(args))
        if receipt["state"] != "completed":
            print("codex-worker: " + receipt["error"], file=sys.stderr)
            return 1
    except OSError:
        print("codex-worker: Cannot access execution artifacts; restore storage access/space and inspect the retained pane/attempt before retrying", file=sys.stderr)
        return 1
    except (factory.FactoryError, ValueError) as exc:
        print("codex-worker: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
