"""One explicit read-only native Grok assignment with retained original events."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

sys.dont_write_bytecode = True

import factory
import native_grok
import review_evidence
from codex_worker import NATIVE_LINE_LIMIT, outcome_schema, stop_child


def read_events(path):
    try:
        records = path.read_text().splitlines()
        events = []
        for line in records:
            record = factory.decode_json(line)
            if (not isinstance(record, dict) or type(record.get("schema_version")) is not int
                    or record["schema_version"] != 1 or record.get("kind") != "grok-event-line"
                    or not isinstance(record.get("data"), str)):
                raise native_grok.GrokProbeError("Unsupported Grok event record; inspect the original stream and update its reader")
            record["data"].encode("utf-8")
            events.append(factory.decode_json(record["data"]))
        return events
    except (OSError, UnicodeError, ValueError):
        raise native_grok.GrokProbeError("Cannot decode retained Grok events; restore or inspect the original stream without relaunching") from None


async def execute(args, *, spawn=asyncio.create_subprocess_exec, probe=native_grok.probe,
                  configuration=native_grok.subscription_configuration, clock=time.time, timer=time.monotonic):
    if args.receipt.exists() or args.events.exists():
        raise factory.FactoryError("Grok attempt already has artifacts; inspect or collect it without relaunching")
    if any(name in os.environ for name in native_grok.API_KEY_ENVIRONMENT):
        raise factory.FactoryError("Unset XAI_API_KEY and GROK_CODE_XAI_API_KEY before native subscription execution")
    if (type(args.seconds) is not int or not 1 <= args.seconds <= factory.MAX_NATIVE_SECONDS
            or type(args.turns) is not int or not 1 <= args.turns <= 1000
            or not args.model.strip() or not args.effort.strip()):
        raise factory.FactoryError("Supply discovered Grok model/effort choices and positive bounded seconds/turns before execution")
    try:
        if str(uuid.UUID(args.native_session)) != args.native_session:
            raise ValueError("Noncanonical session")
        binary = args.grok.resolve(strict=True)
        if not args.grok.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError("Invalid native binary")
        identity = factory.decode_json(args.repo_identity)
        instructions = args.brief.read_text()
    except (OSError, ValueError, UnicodeError):
        raise factory.FactoryError("Restore the original Grok executable, native session, repository identity and brief before another attempt") from None
    digest = hashlib.sha256(instructions.encode()).hexdigest()
    if (digest != args.brief_sha256 or instructions.split("\n", 1)[0] != "# Assignment: " + args.role
            or not factory.valid_repository_identity(identity) or not args.worktree_git_dir.is_absolute()):
        raise factory.FactoryError("Grok assignment binding changed; inspect its original brief, role and repository pin")
    factory.role_spec(args.role)
    review_evidence.validate(instructions, candidate=args.candidate, requirements=args.requirements,
                             base=args.base, role=args.role)
    factory.check_checkout(str(args.repo), str(args.cwd), args.candidate, expected_identity=identity,
                           expected_git_dir=args.worktree_git_dir)
    factory.check_checkout(".", ".", args.candidate, expected_identity=identity, expected_git_dir=args.worktree_git_dir)
    if any(binary.is_relative_to(root) for root in (
            Path(factory.git_worktree(".")), factory.git_common_dir("."), args.worktree_git_dir)):
        raise factory.FactoryError("Keep the native Grok installation outside the assigned checkout and Git metadata")
    try:
        observation = await probe(binary, Path("."), configuration=configuration)
        native_grok.validate_discovery(observation, binary, args.cwd)
        config = observation["configuration"]
    except native_grok.GrokProbeError as exc:
        raise factory.FactoryError(str(exc)) from None
    authentication = observation["authentication"]
    model = next((item for item in observation["models"] if item["modelId"] == args.model), None)
    if (authentication["auth_mode"] != "Oidc" or authentication["subscription_tier"] is None
            or authentication["gate"] is not None or model is None
            or args.effort not in {item["value"] for item in model.get("_meta", {}).get("reasoningEfforts", [])}):
        raise factory.FactoryError("Grok subscription eligibility or model/effort is unavailable; refresh native metadata and select an eligible route before another attempt")
    receipt = dict(schema_version=2, kind="grok-execution", state="starting", job=args.job,
        inputs=dict(candidate=args.candidate, requirements=args.requirements), repository_identity=identity,
        worktree_git_dir=str(args.worktree_git_dir), cwd=str(args.cwd), brief_sha256=digest, role=args.role,
        binary=str(binary), requested_model=args.model, requested_effort=args.effort,
        discovery=observation, configuration=config,
        native_session=args.native_session, seconds=args.seconds, max_turns=args.turns,
        created_at=clock(), finished_at=None, elapsed_seconds=None, exit_code=None,
        report=None, request_id=None, usage=None, model_usage=None, turns=None,
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
            environment = native_grok.environment()
            process = await spawn(str(binary), "--no-auto-update", "--no-subagents", "--disable-web-search",
                "--permission-mode", "bypassPermissions", *native_grok.READ_OPTIONS, "--max-turns", str(args.turns),
                "--model", args.model, "--reasoning-effort", args.effort, "--session-id", args.native_session,
                "--output-format", "streaming-json", "--json-schema", json.dumps(outcome_schema()),
                "--prompt-file", str(args.brief), cwd=".", env=environment, limit=NATIVE_LINE_LIMIT,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE)
            async def consume():
                while True:
                    try:
                        line = await process.stdout.readline()
                    except ValueError:
                        raise factory.FactoryError("Grok event exceeded 1 MiB; inspect the retained stream and reduce output before a new attempt") from None
                    if not line:
                        break
                    text = line.decode("utf-8", errors="surrogateescape")
                    stream.write(json.dumps(dict(schema_version=1, kind="grok-event-line", data=text)) + "\n")
                    stream.flush()
                    try:
                        line.decode("utf-8")
                        events.append(factory.decode_json(text))
                    except (ValueError, UnicodeError):
                        raise factory.FactoryError("Malformed Grok event; inspect the retained original stream before collection") from None
                    print(text, end="", flush=True)
                return await process.wait()
            receipt["exit_code"] = await asyncio.wait_for(consume(), timeout=args.seconds)
            os.fsync(stream.fileno())
        if receipt["exit_code"] != 0:
            raise factory.FactoryError("Native Grok exited unsuccessfully; inspect its retained events and original Rex pane")
        native = native_grok.parse_execution(events, session_id=args.native_session, cwd=str(args.cwd), max_turns=args.turns)
        receipt.update({key: native[key] for key in ("report", "request_id", "usage", "model_usage", "turns")})
        factory.check_checkout(".", ".", args.candidate, expected_identity=identity, expected_git_dir=args.worktree_git_dir)
        if factory.git_commit(".", "HEAD") != args.candidate:
            raise factory.FactoryError("Read-only Grok candidate changed; inspect its original checkout without reusing the report")
        receipt.update(state="completed", candidate=args.candidate)
    except TimeoutError:
        receipt.update(state="failed", error="Grok execution allowance expired; inspect the original attempt without retrying")
    except OSError:
        receipt.update(state="failed", error="Grok process/event I/O failed; restore local access/storage and inspect the retained attempt")
    except (factory.FactoryError, native_grok.GrokProbeError, ValueError) as exc:
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
    for field in ("grok", "repo", "cwd", "brief", "events", "receipt", "worktree-git-dir"):
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
            print("grok-worker: " + receipt["error"], file=sys.stderr)
            return 1
    except (factory.FactoryError, OSError, ValueError) as exc:
        print("grok-worker: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
