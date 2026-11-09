"""One explicit native Antigravity assignment, reviewer or writer, with retained original events."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.dont_write_bytecode = True

import factory
import factory_commit
import native_antigravity
import review_evidence
from codex_worker import NATIVE_LINE_LIMIT, native_environment, outcome_schema, stop_child


def read_events(path):
    """The original native lines, exactly as the decoder reads them."""
    try:
        lines = []
        for line in path.read_text().splitlines():
            record = factory.decode_json(line)
            if (not isinstance(record, dict) or type(record.get("schema_version")) is not int
                    or record["schema_version"] != 1 or record.get("kind") != "antigravity-event-line"
                    or not isinstance(record.get("data"), str)):
                raise native_antigravity.ExecutionError("Unsupported Antigravity event record; inspect the original stream and update its reader")
            record["data"].encode("utf-8")
            lines.append(record["data"])
        return lines
    except (OSError, UnicodeError, ValueError):
        raise native_antigravity.ExecutionError("Cannot decode retained Antigravity events; restore or inspect the original stream without relaunching") from None


def command(binary, model, effort):
    """Print mode reading one stream-json user message from stdin, so briefs avoid argv limits."""
    return [str(binary), "--print=", "--input-format", "stream-json", "--output-format", "stream-json",
            "--json-schema", json.dumps(outcome_schema()), "--model", model, "--effort", effort,
            "--dangerously-skip-permissions"]


def message(text):
    return json.dumps(dict(event="user", message=dict(role="user", content=[dict(type="text", text=text)]))) + "\n"


async def execute(args, *, spawn=asyncio.create_subprocess_exec, probe=native_antigravity.probe,
                  clock=time.time, timer=time.monotonic, git_scope=None, scan=None):
    git_scope = git_scope or factory_commit.isolated_git
    scan = scan or factory_commit.scan_staged
    if args.receipt.exists() or args.events.exists():
        raise factory.FactoryError("Antigravity attempt already has artifacts; inspect or collect it without relaunching")
    if any(name in os.environ for name in native_antigravity.API_ENVIRONMENT):
        raise factory.FactoryError("Unset Gemini API/ADC environment overrides before native subscription execution")
    if (type(args.seconds) is not int or not 1 <= args.seconds <= factory.MAX_NATIVE_SECONDS
            or not args.model.strip() or args.effort not in native_antigravity.EFFORTS):
        raise factory.FactoryError("Supply a discovered Antigravity model, a supported effort and a bounded allowance before execution")
    factory.check_commit_message(args.commit_message, write=args.write)
    if args.write and (args.commit_message is None or args.sandbox is None or not args.sandbox.is_absolute()):
        raise factory.FactoryError("An Antigravity writer needs --commit-message and the absolute Codex sandbox binary for the factory commit")
    try:
        binary = args.agy.resolve(strict=True)
        if not args.agy.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError("Invalid native binary")
        identity = factory.decode_json(args.repo_identity)
        instructions = args.brief.read_text()
    except (OSError, ValueError, UnicodeError):
        raise factory.FactoryError("Restore the original Antigravity executable, repository identity and brief before another attempt") from None
    digest = hashlib.sha256(instructions.encode()).hexdigest()
    if (digest != args.brief_sha256 or instructions.split("\n", 1)[0] != "# Assignment: " + args.role
            or not factory.valid_repository_identity(identity) or not args.worktree_git_dir.is_absolute()):
        raise factory.FactoryError("Antigravity assignment binding changed; inspect its original brief, role and repository pin")
    factory.role_spec(args.role)
    if args.write:
        factory.check_role_write(args.role)
    else:
        review_evidence.validate(instructions, candidate=args.candidate, requirements=args.requirements,
                                 base=args.base, role=args.role)
    factory.check_checkout(str(args.repo), str(args.cwd), args.candidate, expected_identity=identity,
                           expected_git_dir=args.worktree_git_dir)
    factory.check_checkout(".", ".", args.candidate, expected_identity=identity, expected_git_dir=args.worktree_git_dir)
    if any(binary.is_relative_to(root) for root in (
            Path(factory.git_worktree(".")), factory.git_common_dir("."), args.worktree_git_dir)):
        raise factory.FactoryError("Keep the native Antigravity installation outside the assigned checkout and Git metadata")
    if args.write:
        try:
            sandbox = args.sandbox.resolve(strict=True)
        except OSError:
            raise factory.FactoryError("Restore the Codex sandbox binary before an Antigravity writer attempt") from None
        factory_commit.prepare_writer(args, sandbox, (
            Path(factory.git_worktree(".")), factory.git_common_dir("."), args.worktree_git_dir))
        factory_commit.prepare_commit(args, scan=scan)
    try:
        observation = probe(binary, args.cwd)
        native_antigravity.validate_usage(observation, binary)
    except native_antigravity.UsageError as exc:
        raise factory.FactoryError(str(exc)) from None
    # Version 1 reviews; version 2 writes.
    receipt = dict(schema_version=2 if args.write else 1, kind="antigravity-execution", state="starting", job=args.job,
        inputs=dict(candidate=args.candidate, requirements=args.requirements), repository_identity=identity,
        worktree_git_dir=str(args.worktree_git_dir), cwd=str(args.cwd), brief_sha256=digest, role=args.role,
        binary=str(binary), requested_model=args.model, requested_effort=args.effort, discovery=observation,
        seconds=args.seconds, created_at=clock(), finished_at=None, elapsed_seconds=None, exit_code=None,
        report=None, conversation_id=None, usage=None, duration_seconds=None, turns=None,
        candidate=None, events=str(args.events), error=None)
    if args.write:
        # Version 2 adds the writer flag, its Codex sandbox tool and the factory commit evidence.
        receipt.update(writer=True, sandbox=str(args.sandbox), commit=None)
    fd = os.open(args.events, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        factory.save(args.receipt, receipt, exclusive=True)
    except OSError:
        os.close(fd)
        raise
    started, process, lines = timer(), None, []
    try:
        with os.fdopen(fd, "w") as stream:
            prompt_text = instructions + ("\n" + factory_commit.WRITER_CONTRACT + "\n" if args.write else "")
            # A private file provides EOF without argv limits or pipe backpressure.
            with tempfile.TemporaryFile() as prompt:
                prompt.write(message(prompt_text).encode("utf-8"))
                prompt.seek(0)
                process = await spawn(*command(binary, args.model, args.effort), cwd=".", env=native_environment(),
                                      limit=NATIVE_LINE_LIMIT, stdin=prompt, stdout=asyncio.subprocess.PIPE)
            async def consume():
                while True:
                    try:
                        line = await process.stdout.readline()
                    except ValueError:
                        raise factory.FactoryError("Antigravity event exceeded 1 MiB; inspect the retained stream and reduce output before a new attempt") from None
                    if not line:
                        break
                    text = line.decode("utf-8", errors="surrogateescape")
                    stream.write(json.dumps(dict(schema_version=1, kind="antigravity-event-line", data=text)) + "\n")
                    stream.flush()
                    try:
                        line.decode("utf-8")
                    except UnicodeError:
                        raise factory.FactoryError("Malformed Antigravity event; inspect the retained original stream before collection") from None
                    lines.append(text)
                    print(text, end="", flush=True)
                return await process.wait()
            receipt["exit_code"] = await asyncio.wait_for(consume(), timeout=args.seconds)
            os.fsync(stream.fileno())
        if receipt["exit_code"] != 0:
            raise factory.FactoryError("Native Antigravity exited unsuccessfully; inspect its retained events and original Rex pane")
        native = native_antigravity.parse_execution(lines, expected_model=args.model, expected_cwd=str(args.cwd),
                                                    expected_schema=outcome_schema())
        receipt.update(report=native["report"], conversation_id=native["conversation_id"], usage=native["usage"],
                       duration_seconds=native["duration_seconds"], turns=native["turns"])
        if args.write:
            # The native process has exited; the factory alone stages, scans and commits its work.
            candidate = factory_commit.commit_completed(argparse.Namespace(**{**vars(args), "sandbox": sandbox}),
                                                        receipt, identity, git_scope=git_scope, scan=scan)
        else:
            factory.check_checkout(".", ".", args.candidate, expected_identity=identity, expected_git_dir=args.worktree_git_dir)
            if factory.git_commit(".", "HEAD") != args.candidate:
                raise factory.FactoryError("Read-only Antigravity candidate changed; inspect its original checkout without reusing the report")
            candidate = args.candidate
        receipt.update(state="completed", candidate=candidate)
    except TimeoutError:
        receipt.update(state="failed", error="Antigravity execution allowance expired; inspect the original attempt without retrying")
    except OSError:
        receipt.update(state="failed", error="Antigravity process/event I/O failed; restore local access/storage and inspect the retained attempt")
    except (factory.FactoryError, native_antigravity.ExecutionError, ValueError) as exc:
        receipt.update(state="failed", error=str(exc))
    finally:
        if process is not None:
            await stop_child(process)
            receipt["exit_code"] = process.returncode
        receipt.update(finished_at=clock(), elapsed_seconds=timer() - started)
        factory.save(args.receipt, receipt)
    return receipt


def parser():
    command_line = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    for field in ("agy", "repo", "cwd", "brief", "events", "receipt", "worktree-git-dir"):
        command_line.add_argument("--" + field, type=Path, required=True)
    for field in ("job", "candidate", "requirements", "brief-sha256", "model", "effort", "repo-identity", "role"):
        command_line.add_argument("--" + field, required=True)
    command_line.add_argument("--seconds", type=int, default=600)
    command_line.add_argument("--base")
    command_line.add_argument("--write", action="store_true", help="Implementation specialist with every native tool in its worktree")
    command_line.add_argument("--commit-message", help="With --write, the factory commits a completed result using this message")
    command_line.add_argument("--sandbox", type=Path, help="With --write, the Codex CLI used as the factory commit's local sandbox")
    return command_line


def main(argv=None):
    try:
        receipt = asyncio.run(execute(parser().parse_args(argv)))
        if receipt["state"] != "completed":
            print("antigravity-worker: " + receipt["error"], file=sys.stderr)
            return 1
    except (factory.FactoryError, OSError, ValueError) as exc:
        print("antigravity-worker: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
