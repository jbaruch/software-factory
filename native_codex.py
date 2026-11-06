"""Read native Codex account, model catalog and rate limits without a model turn."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time


class CodexProbeError(Exception):
    pass


async def exchange(process, identity, method, params):
    process.stdin.write((json.dumps(dict(id=identity, method=method, params=params)) + "\n").encode())
    await process.stdin.drain()
    deadline = asyncio.get_running_loop().time() + 20
    while True:
        try:
            line = await asyncio.wait_for(process.stdout.readline(),
                                          timeout=max(0, deadline - asyncio.get_running_loop().time()))
        except TimeoutError:
            raise CodexProbeError(f"Native Codex {method} timed out; inspect native sign-in/connection before refreshing") from None
        if not line:
            raise CodexProbeError("Native Codex closed its metadata channel; inspect native sign-in/configuration")
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise CodexProbeError("Malformed native Codex protocol; inspect its connection and update the adapter before scheduling") from None
        if not isinstance(message, dict):
            raise CodexProbeError("Native Codex returned malformed metadata; update the adapter before scheduling")
        if message.get("id") != identity or "method" in message:
            continue
        if "error" in message:
            raise CodexProbeError(f"Native Codex rejected {method}; inspect native sign-in/configuration")
        if not isinstance(message.get("result"), dict):
            raise CodexProbeError("Native Codex returned no metadata result; do not assume capacity")
        return message["result"]


async def probe(binary, *, spawn=asyncio.create_subprocess_exec, clock=time.time, timer=time.monotonic) -> dict:
    try:
        binary = Path(binary).resolve(strict=True)
    except OSError:
        raise CodexProbeError("Cannot access --codex; supply an existing executable native Codex CLI") from None
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise CodexProbeError("Supply the absolute path to an executable native Codex CLI")
    started = timer()
    process = await spawn(str(binary), "app-server", "--listen", "stdio://",
                          stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                          stderr=asyncio.subprocess.DEVNULL)
    try:
        initialized = await exchange(process, 0, "initialize", {"clientInfo": {
            "name": "software_factory", "title": "Software Factory", "version": "0.1.0"}})
        process.stdin.write(b'{"method":"initialized","params":{}}\n')
        await process.stdin.drain()
        account = await exchange(process, 1, "account/read", {"refreshToken": False})
        limits = await exchange(process, 2, "account/rateLimits/read", {})
        models, cursor = [], None
        for page in range(20):
            catalog = await exchange(process, 3 + page, "model/list",
                                     dict(limit=100, includeHidden=False, cursor=cursor))
            if not isinstance(catalog.get("data"), list):
                raise CodexProbeError("Native Codex catalog is unavailable; do not invent model candidates")
            models.extend(catalog["data"])
            cursor = catalog.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor:
                raise CodexProbeError("Native Codex returned an invalid catalog cursor; update the adapter")
        else:
            raise CodexProbeError("Native Codex catalog exceeded the probe's page limit; inspect pagination")
        observed = clock()
        return dict(schema_version=1, route="codex-native", binary=str(binary), observed_at=observed,
                    probe_seconds=timer() - started, initialization=initialized,
                    account=account, rate_limits=limits, models=models, readiness="unverified",
                    note="Metadata only: model catalogs may be cached; inference and lifecycle access remain unverified.")
    finally:
        # This is the private child created above, never the shared daemon or a user session.
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass  # The owned child exited between the returncode check and signal.
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if not args.codex.is_absolute():
            raise CodexProbeError("Supply an absolute native Codex executable path")
        print(json.dumps(asyncio.run(probe(args.codex)), indent=2, allow_nan=False))
    except (CodexProbeError, OSError, TimeoutError, ValueError) as exc:
        print("codex-probe: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
