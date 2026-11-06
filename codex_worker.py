"""One explicit native Codex assignment; retain its original events and compact result."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time

import factory


class WorkerError(ValueError):
    pass


def outcome_schema():
    return dict(type="object", additionalProperties=False,
                properties=dict(outcome=dict(type="string", enum=sorted(factory.RESULT_OUTCOMES)),
                                artifacts=dict(type="array", items=dict(type="string"))),
                required=["outcome", "artifacts"])


def parse_events(events) -> dict:
    """A native completion is a carrier for a specialist report, never task acceptance."""
    thread, turns, completed, message, usage = None, 0, 0, None, None
    for event in events:
        if not isinstance(event, dict):
            raise WorkerError("Native output is not an event object; inspect the retained stream")
        kind = event.get("type")
        if kind == "thread.started":
            if thread is not None or not isinstance(event.get("thread_id"), str) or not event["thread_id"]:
                raise WorkerError("Native worker returned an ambiguous thread; inspect its original events")
            thread = event["thread_id"]
        elif kind == "turn.started":
            turns += 1
        elif kind in {"turn.failed", "error"}:
            raise WorkerError("Native worker failed; inspect its original events before another attempt")
        elif kind == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                raise WorkerError("Native item is malformed; inspect the retained stream")
            if item.get("type") == "agent_message":
                message = item.get("text")
        elif kind == "turn.completed":
            completed += 1
            usage = event.get("usage")
    if thread is None or turns != 1 or completed != 1:
        raise WorkerError("Native completion is missing or ambiguous; reconcile the existing attempt")
    if not isinstance(message, str):
        raise WorkerError("Native final message is missing; inspect its original events")
    try:
        report = json.loads(message)
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


async def stop_child(process):
    if process.returncode is not None:
        return
    try:
        process.send_signal(signal.SIGINT)
    except ProcessLookupError:
        return  # The owned process already exited.
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.wait()


async def execute(args, *, spawn=asyncio.create_subprocess_exec, clock=time.time, timer=time.monotonic):
    if args.receipt.exists() or args.events.exists():
        raise factory.FactoryError("Native attempt already has artifacts; collect or inspect it, never relaunch it blindly")
    binary = args.codex.resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK) or args.seconds <= 0:
        raise factory.FactoryError("Supply an executable Codex CLI and a positive execution allowance")
    if os.environ.get("CODEX_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        raise factory.FactoryError("Remove API-key overrides from this worker environment and use native ChatGPT sign-in")
    instructions = args.brief.read_text()
    digest = hashlib.sha256(instructions.encode()).hexdigest()
    if digest != args.brief_sha256:
        raise factory.FactoryError("Assignment brief changed; inspect the original task before launching")
    factory.check_checkout(str(args.repo), str(args.cwd), args.candidate)
    schema = args.receipt.with_suffix(".schema.json")
    # The receipt is this native effect's before-start intent, separate from the Rex launch.
    receipt = dict(schema_version=1, kind="codex-execution", job=args.job, state="starting",
                   inputs=dict(candidate=args.candidate, requirements=args.requirements),
                   brief_sha256=digest, cwd=str(args.cwd), requested_model=args.model,
                   requested_effort=args.effort, binary=str(binary), writer=args.write,
                   created_at=clock(), finished_at=None,
                   elapsed_seconds=None, exit_code=None, thread_id=None, report=None, usage=None,
                   candidate=None, events=str(args.events), error=None)
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
            process = await spawn(str(binary), "--no-daemon", "exec", "--json", "--color", "never",
                                  "--output-schema", str(schema), "--model", args.model,
                                  "--sandbox", "workspace-write" if args.write else "read-only",
                                  "-c", "model_reasoning_effort=" + json.dumps(args.effort),
                                  "--cd", str(args.cwd), instructions,
                                  stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE)

            async def consume():
                while line := await process.stdout.readline():
                    text = line.decode("utf-8")
                    stream.write(text)
                    stream.flush()
                    print(text, end="", flush=True)
                    event = json.loads(text)
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
        candidate = factory.git_commit(str(args.cwd), "HEAD")
        factory.check_checkout(str(args.repo), str(args.cwd), candidate)
        if not args.write and candidate != args.candidate:
            raise factory.FactoryError("Read-only assignment's candidate changed; do not reuse its verification")
        receipt.update(candidate=candidate, state="completed")
    except (factory.FactoryError, OSError, ValueError, KeyError, TypeError, TimeoutError) as exc:
        receipt.update(state="failed", error=("Native execution allowance expired; inspect the existing attempt"
                                               if isinstance(exc, TimeoutError) else str(exc)))
    finally:
        if process is not None:
            await stop_child(process)
            receipt["exit_code"] = process.returncode
        receipt.update(finished_at=clock(), elapsed_seconds=timer() - started)
        factory.save(args.receipt, receipt)
    return receipt


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    for field in ("codex", "repo", "cwd", "brief", "events", "receipt"):
        command.add_argument("--" + field, type=Path, required=True)
    for field in ("job", "candidate", "requirements", "brief-sha256", "model", "effort"):
        command.add_argument("--" + field, required=True)
    command.add_argument("--write", action="store_true")
    command.add_argument("--seconds", type=int, default=600, help="Initial execution allowance; no automatic retry")
    return command


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        receipt = asyncio.run(execute(args))
        if receipt["state"] != "completed":
            print("codex-worker: " + receipt["error"], file=sys.stderr)
            return 1
    except (factory.FactoryError, OSError, ValueError) as exc:
        print("codex-worker: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
