"""One explicit native Claude assignment, reviewer or writer, with retained original events."""
import argparse
import asyncio
import contextlib
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import sys
import time
import tempfile
import uuid

sys.dont_write_bytecode = True

import factory
import factory_commit
import native_claude
import release_evidence
import review_evidence
from codex_worker import NATIVE_LINE_LIMIT, cancelled, stop_child

RETAINED_STREAM_LIMIT = 16 * 1024 * 1024  # Serialized envelope bytes, including newlines.


def read_events(path, content=None):
    try:
        events = []
        if content is None:
            content = factory.read_record(path, private=True, max_bytes=RETAINED_STREAM_LIMIT)
        elif len(content.encode("utf-8")) > RETAINED_STREAM_LIMIT:
            raise ValueError("Oversized retained stream")
        with StringIO(content) as stream:
            while True:
                line = stream.readline(NATIVE_LINE_LIMIT * 6 + 1)
                if not line:
                    break
                if len(line) > NATIVE_LINE_LIMIT * 6:
                    raise ValueError("Oversized envelope")
                record = factory.decode_json(line)
                if (not isinstance(record, dict) or type(record.get("schema_version")) is not int
                        or record["schema_version"] != 1 or record.get("kind") != "claude-event-line"
                        or not isinstance(record.get("data"), str)
                        or len(record["data"].encode("utf-8")) > NATIVE_LINE_LIMIT):
                    raise ValueError("Unsupported envelope")
                events.append(factory.decode_json(record["data"]))
        return events
    except (OSError, UnicodeError, ValueError):
        raise native_claude.ClaudeProbeError("Cannot decode retained Claude events within the retained-stream byte limit; restore or inspect the original stream without relaunching") from None


async def execute(args, *, spawn=asyncio.create_subprocess_exec, probe=native_claude.probe, clock=time.time, timer=time.monotonic,
                  git_scope=None, scan=None):
    git_scope = git_scope or factory_commit.isolated_git
    scan = scan or factory_commit.scan_staged
    if args.receipt.exists() or args.events.exists():
        raise factory.FactoryError("Claude attempt already has artifacts; inspect or collect it without relaunching")
    if (type(args.seconds) is not int or not 1 <= args.seconds <= factory.MAX_NATIVE_SECONDS
            or type(args.turns) is not int or not 1 <= args.turns <= 1000
            or not isinstance(args.model, str) or not args.model.strip() or args.effort not in native_claude.EFFORTS):
        raise factory.FactoryError("Supply a discovered Claude model, a supported effort and positive bounded seconds/turns before execution")
    factory.check_commit_message(args.commit_message, write=args.write)
    if args.write and (args.commit_message is None or args.sandbox is None or not args.sandbox.is_absolute()):
        raise factory.FactoryError("A Claude writer needs --commit-message and the absolute Codex sandbox binary for the factory commit")
    try:
        if str(uuid.UUID(args.native_session)) != args.native_session:
            raise ValueError("Noncanonical session")
        binary = args.claude.resolve(strict=True)
        if not args.claude.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError("Invalid native binary")
        identity = factory.decode_json(args.repo_identity)
        instructions = factory.read_record(args.brief, private=True)
    except (OSError, ValueError, UnicodeError):
        raise factory.FactoryError("Restore the original Claude executable, native session, repository identity and brief before another attempt") from None
    digest = hashlib.sha256(instructions.encode()).hexdigest()
    if (digest != args.brief_sha256 or instructions.split("\n", 1)[0] != "# Assignment: " + args.role
            or not factory.valid_repository_identity(identity) or not args.worktree_git_dir.is_absolute()):
        raise factory.FactoryError("Claude assignment binding changed; inspect its original brief, role and repository pin")
    factory.role_spec(args.role)
    coordination = None
    if any(getattr(args, key) is not None for key in ("decision_store", "decision", "question")):
        import coordination_evidence
        if args.write or not coordination_evidence.binding(args.decision, args.question, args.role):
            raise factory.FactoryError("Bind the original decision and question before a coordination worker starts")
        coordination = coordination_evidence.read(instructions, args.decision, args.question, args.role,
                                                  dict(candidate=args.candidate, requirements=args.requirements))
        if args.role == "judge":
            coordination_evidence.validate_claude_judge(coordination, args.model, args.effort)
    release_evidence.check_brief(args, instructions)
    if args.role == "release":
        release_evidence.require_max_effort(args.effort, native_claude.EFFORTS)
    if args.write:
        factory.check_role_write(args.role)
    elif coordination is None:
        review_evidence.validate(instructions, candidate=args.candidate, requirements=args.requirements,
                                 base=args.base, role=args.role)
    report_source = factory.check_worker_cwd(args, identity)
    if any(binary.is_relative_to(root) for root in (
            Path(factory.git_worktree(".")), factory.git_common_dir("."), args.worktree_git_dir)):
        raise factory.FactoryError("Keep the native Claude installation outside the assigned checkout and Git metadata")
    if args.write:
        try:
            sandbox = args.sandbox.resolve(strict=True)
        except OSError:
            raise factory.FactoryError("Restore the Codex sandbox binary before a Claude writer attempt") from None
        factory_commit.prepare_writer(args, sandbox, (
            Path(factory.git_worktree(".")), factory.git_common_dir("."), args.worktree_git_dir))
        factory_commit.prepare_commit(args, scan=scan)
    try:
        observation = probe(binary, args.cwd)
        native_claude.validate_auth(observation, binary, args.cwd)
    except native_claude.ClaudeProbeError as exc:
        raise factory.FactoryError(str(exc)) from None
    # Version 2 writes, version 3 answers one bounded coordination question and version 4 reviews in a
    # throwaway checkout where it may run commands (version 1 reviewed in place with Read only).
    # Version 5 binds the complete event history on success or on cancellation before native creation.
    reviewer = not args.write and coordination is None
    receipt: dict = dict(schema_version=factory.SCRATCH_RECEIPT_VERSION, kind="claude-execution", state="starting", job=args.job,
        scratch=None, scratch_identity=None, scratch_removed=None, native_process=None,
        interpreter=factory.interpreter_identity(sys.executable), retained_files={},
        inputs=dict(candidate=args.candidate, requirements=args.requirements), repository_identity=identity,
        worktree_git_dir=str(args.worktree_git_dir), cwd=str(args.cwd), brief_sha256=digest, role=args.role,
        binary=str(binary), requested_model=args.model, requested_effort=args.effort,
        discovery=observation,
        native_session=args.native_session, seconds=args.seconds, max_turns=args.turns,
        created_at=clock(), finished_at=None, elapsed_seconds=None, exit_code=None,
        report=None, usage=None, model_usage=None, turns=None, rate_limits=None,
        candidate=None, events=str(args.events), error=None)
    if args.write:
        # Version 2 adds the writer flag, its Codex sandbox tool and the factory commit evidence.
        receipt.update(writer=True, sandbox=str(args.sandbox), commit=None)
    if reviewer:
        receipt.update(checkout=str(factory.reader_checkout_path(args.receipt)), events_digest=None)
    fd = os.open(args.events, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        factory.save(args.receipt, receipt, exclusive=True)
    except OSError:
        os.close(fd)
        raise
    started, process, events = timer(), None, []
    before_native_start = True
    checkouts, workdir = contextlib.ExitStack(), Path(".")
    reader_admin_stat = None
    try:
        if reviewer:
            workdir, reader_admin_stat, report_source = checkouts.enter_context(factory.reader_checkout(
                args.repo, args.candidate, Path(receipt["checkout"]), identity=identity))
        with os.fdopen(fd, "w") as stream:
            environment = factory.worker_environment(native_claude.environment(), workdir,
                scratch=checkouts.enter_context(factory.scratch_directory(args.receipt, receipt)))
            # A private file provides EOF without argv limits or pipe backpressure.
            with tempfile.TemporaryFile() as prompt:
                prompt.write(instructions.encode("utf-8"))
                prompt.seek(0)
                command = (native_claude.write_command(args.model, args.effort, args.native_session, args.turns) if args.write
                           else native_claude.read_command(args.model, args.effort, args.native_session, args.turns, coordination))
                # After handing creation to the OS, a missing handle cannot prove no child ran.
                await asyncio.sleep(0)
                before_native_start = False
                process = await spawn(str(binary), *command, cwd=str(workdir), env=environment, limit=NATIVE_LINE_LIMIT,
                    stdin=prompt, stdout=asyncio.subprocess.PIPE)
                factory.record_native_process(args.receipt, receipt, process)
            async def consume():
                retained_bytes = 0
                while True:
                    try:
                        line = await process.stdout.readline()
                    except ValueError:
                        raise factory.FactoryError(f"Claude event exceeded {NATIVE_LINE_LIMIT // (1024 * 1024)} MiB; inspect the retained stream and reduce output before a new attempt") from None
                    if not line:
                        break
                    text = line.decode("utf-8", errors="surrogateescape")
                    envelope = json.dumps(dict(schema_version=1, kind="claude-event-line", data=text)) + "\n"
                    size = len(envelope.encode("ascii"))
                    if size > RETAINED_STREAM_LIMIT - retained_bytes:
                        raise factory.FactoryError("Claude retained-stream byte limit exceeded; inspect the original attempt and reduce output before a new attempt")
                    stream.write(envelope)
                    retained_bytes += size
                    stream.flush()
                    try:
                        line.decode("utf-8")
                        events.append(factory.decode_json(text))
                    except (ValueError, UnicodeError):
                        raise factory.FactoryError("Malformed Claude event; inspect the retained original stream before collection") from None
                    shown = factory.pane_line("claude", events[-1])
                    if shown is not None:
                        print(shown, flush=True)
                return await process.wait()
            deadline = factory.native_clock() + args.seconds
            receipt["exit_code"] = await factory.within_budget(consume(), deadline=deadline)
            os.fsync(stream.fileno())
        if receipt["exit_code"] != 0:
            raise factory.FactoryError("Native Claude exited unsuccessfully; inspect its retained events and original Rex pane")
        native = native_claude.parse_execution(events, session_id=args.native_session,
                                               cwd=receipt["checkout"] if reviewer else str(args.cwd), model=args.model,
                                               max_turns=args.turns, writer=args.write, coordination=coordination,
                                               checkout=reviewer)
        receipt.update({key: native[key] for key in ("report", "usage", "model_usage", "turns", "rate_limits")})
        factory.check_tool_evidence("claude", events, args, coordination)
        if reviewer or args.write:
            factory.retain_report_files(receipt, args, workdir, deadline=deadline, source=report_source,
                                        reader_admin_stat=reader_admin_stat)
        if args.write:
            # The native process has exited; the factory alone stages, scans and commits its work.
            candidate = factory_commit.commit_completed(argparse.Namespace(**{**vars(args), "sandbox": sandbox}),
                                                        receipt, identity, git_scope=git_scope, scan=scan)
        else:
            factory.check_checkout(".", ".", args.candidate, expected_identity=identity, expected_git_dir=args.worktree_git_dir)
            if factory.git_commit(".", "HEAD") != args.candidate:
                raise factory.FactoryError("Read-only Claude candidate changed; inspect its original checkout without reusing the report")
            candidate = args.candidate
        if reviewer:
            factory.check_report_index(workdir, reader_admin_stat=reader_admin_stat)
            receipt["events_digest"] = release_evidence.digest(events)
        receipt.update(state="completed", candidate=candidate)
    except TimeoutError:
        receipt.update(state="failed", error="Claude execution allowance expired; inspect the original attempt without retrying")
    except asyncio.CancelledError:
        receipt.update(state="failed", error=cancelled())
        if reviewer and before_native_start:
            receipt["events_digest"] = release_evidence.digest(events)
    except OSError:
        receipt.update(state="failed", error="Claude process/event I/O failed; restore local access/storage and inspect the retained attempt")
    except (factory.FactoryError, native_claude.ClaudeProbeError, ValueError) as exc:
        receipt.update(state="failed", error=str(exc))
    finally:
        if process is not None:
            await stop_child(process)
            receipt["exit_code"] = process.returncode
        try:
            checkouts.close()  # Only after the native process has stopped.
        except factory.FactoryError as exc:
            receipt.update(state="failed", error="Cannot remove the reader checkout " + receipt["checkout"]
                           + "; remove it with git worktree remove --force --force before another attempt: " + str(exc))
        receipt.update(finished_at=clock(), elapsed_seconds=timer() - started)
        factory.save(args.receipt, receipt)
    return receipt


def parser():
    command = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    for field in ("claude", "repo", "cwd", "brief", "events", "receipt", "worktree-git-dir"):
        command.add_argument("--" + field, type=Path, required=True)
    for field in ("job", "candidate", "requirements", "brief-sha256", "model", "effort", "repo-identity", "role", "native-session"):
        command.add_argument("--" + field, required=True)
    command.add_argument("--seconds", type=int, default=600)
    command.add_argument("--turns", type=int, default=30)
    command.add_argument("--base")
    command.add_argument("--write", action="store_true", help="Implementation specialist with edit tools in its worktree")
    command.add_argument("--commit-message", help="With --write, the factory commits a completed result using this message")
    command.add_argument("--sandbox", type=Path, help="With --write, the Codex CLI used as the factory commit's local sandbox")
    command.add_argument("--decision-store", type=Path, help="Original Jev decision store; frozen before the native launch")
    command.add_argument("--decision", help="Exact original Jev decision ID")
    command.add_argument("--question", help="One unresolved question from that decision")
    factory.add_judgment_arguments(command)
    return command


def main(argv=None):
    try:
        receipt = asyncio.run(execute(parser().parse_args(argv)))
        if receipt["state"] != "completed":
            print("claude-worker: " + receipt["error"], file=sys.stderr)
            return 1
    except (factory.FactoryError, OSError, ValueError) as exc:
        print("claude-worker: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
