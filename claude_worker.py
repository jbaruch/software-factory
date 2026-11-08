"""One explicit read-only native Claude assignment with retained original events."""
import argparse
import asyncio
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
import native_claude
import review_evidence
from codex_worker import NATIVE_LINE_LIMIT, stop_child

RETAINED_STREAM_LIMIT = 16 * 1024 * 1024  # Serialized envelope bytes, including newlines.


def read_events(path):
    try:
        events = []
        with StringIO(factory.read_record(path, private=True, max_bytes=RETAINED_STREAM_LIMIT)) as stream:
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


async def execute(args, *, spawn=asyncio.create_subprocess_exec, probe=native_claude.probe, clock=time.time, timer=time.monotonic):
    if args.receipt.exists() or args.events.exists():
        raise factory.FactoryError("Claude attempt already has artifacts; inspect or collect it without relaunching")
    if (type(args.seconds) is not int or not 1 <= args.seconds <= factory.MAX_NATIVE_SECONDS
            or type(args.turns) is not int or not 1 <= args.turns <= 1000
            or args.model != "claude-sonnet-5-5" or args.effort != "low"):
        raise factory.FactoryError("Supply discovered Claude model/effort choices and positive bounded seconds/turns before execution")
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
    review_evidence.validate(instructions, candidate=args.candidate, requirements=args.requirements,
                             base=args.base, role=args.role)
    factory.check_checkout(str(args.repo), str(args.cwd), args.candidate, expected_identity=identity,
                           expected_git_dir=args.worktree_git_dir)
    factory.check_checkout(".", ".", args.candidate, expected_identity=identity, expected_git_dir=args.worktree_git_dir)
    if any(binary.is_relative_to(root) for root in (
            Path(factory.git_worktree(".")), factory.git_common_dir("."), args.worktree_git_dir)):
        raise factory.FactoryError("Keep the native Claude installation outside the assigned checkout and Git metadata")
    try:
        observation = probe(binary, args.cwd)
        native_claude.validate_auth(observation, binary, args.cwd)
    except native_claude.ClaudeProbeError as exc:
        raise factory.FactoryError(str(exc)) from None
    receipt = dict(schema_version=1, kind="claude-execution", state="starting", job=args.job,
        inputs=dict(candidate=args.candidate, requirements=args.requirements), repository_identity=identity,
        worktree_git_dir=str(args.worktree_git_dir), cwd=str(args.cwd), brief_sha256=digest, role=args.role,
        binary=str(binary), requested_model=args.model, requested_effort=args.effort,
        discovery=observation,
        native_session=args.native_session, seconds=args.seconds, max_turns=args.turns,
        created_at=clock(), finished_at=None, elapsed_seconds=None, exit_code=None,
        report=None, usage=None, model_usage=None, turns=None, rate_limits=None,
        candidate=None, events=str(args.events), error=None)
    fd = os.open(args.events, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        factory.save(args.receipt, receipt, exclusive=True)
    except OSError:
        os.close(fd)
        raise
    started, process, events = timer(), None, []
    try:
        with os.fdopen(fd, "w") as stream:
            environment = native_claude.environment()
            # A private file provides EOF without argv limits or pipe backpressure.
            with tempfile.TemporaryFile() as prompt:
                prompt.write(instructions.encode("utf-8"))
                prompt.seek(0)
                process = await spawn(str(binary), *native_claude.read_command(args.model, args.effort,
                    args.native_session, args.turns), cwd=".", env=environment, limit=NATIVE_LINE_LIMIT,
                    stdin=prompt, stdout=asyncio.subprocess.PIPE)
            async def consume():
                retained_bytes = 0
                while True:
                    try:
                        line = await process.stdout.readline()
                    except ValueError:
                        raise factory.FactoryError("Claude event exceeded 1 MiB; inspect the retained stream and reduce output before a new attempt") from None
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
                    print(text, end="", flush=True)
                return await process.wait()
            receipt["exit_code"] = await asyncio.wait_for(consume(), timeout=args.seconds)
            os.fsync(stream.fileno())
        if receipt["exit_code"] != 0:
            raise factory.FactoryError("Native Claude exited unsuccessfully; inspect its retained events and original Rex pane")
        native = native_claude.parse_execution(events, session_id=args.native_session, cwd=str(args.cwd), model=args.model, max_turns=args.turns)
        receipt.update({key: native[key] for key in ("report", "usage", "model_usage", "turns", "rate_limits")})
        factory.check_checkout(".", ".", args.candidate, expected_identity=identity, expected_git_dir=args.worktree_git_dir)
        if factory.git_commit(".", "HEAD") != args.candidate:
            raise factory.FactoryError("Read-only Claude candidate changed; inspect its original checkout without reusing the report")
        receipt.update(state="completed", candidate=args.candidate)
    except TimeoutError:
        receipt.update(state="failed", error="Claude execution allowance expired; inspect the original attempt without retrying")
    except OSError:
        receipt.update(state="failed", error="Claude process/event I/O failed; restore local access/storage and inspect the retained attempt")
    except (factory.FactoryError, native_claude.ClaudeProbeError, ValueError) as exc:
        receipt.update(state="failed", error=str(exc))
    finally:
        if process is not None:
            await stop_child(process)
            receipt["exit_code"] = process.returncode
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
