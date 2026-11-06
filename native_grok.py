"""Observe native Grok cached sign-in and model metadata without requesting a turn."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time

from factory import decode_json

API_KEY_ENVIRONMENT = ("XAI_API_KEY", "GROK_CODE_XAI_API_KEY")


class GrokProbeError(Exception):
    pass


async def exchange(process, identity, method, params):
    request = dict(jsonrpc="2.0", id=identity, method=method, params=params)
    try:
        process.stdin.write((json.dumps(request, allow_nan=False) + "\n").encode())
        await process.stdin.drain()
    except OSError:
        raise GrokProbeError("Cannot write native Grok metadata; inspect the owned process, sign-in and CLI compatibility before refreshing") from None
    deadline = asyncio.get_running_loop().time() + 20
    while True:
        try:
            line = await asyncio.wait_for(process.stdout.readline(),
                timeout=max(0, deadline - asyncio.get_running_loop().time()))
        except TimeoutError:
            raise GrokProbeError(f"Native Grok {method} timed out; inspect native sign-in/connection before refreshing") from None
        except (OSError, ValueError):
            raise GrokProbeError("Cannot read native Grok metadata; inspect its channel and CLI compatibility before refreshing") from None
        if not line:
            raise GrokProbeError("Native Grok closed its metadata channel; inspect native sign-in and CLI configuration")
        try:
            message = decode_json(line)
        except (ValueError, UnicodeDecodeError):
            raise GrokProbeError("Malformed native Grok JSON; inspect the channel and update the adapter before scheduling") from None
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise GrokProbeError("Unsupported native Grok protocol; update the metadata adapter before scheduling")
        if "method" in message:
            if "id" in message or not isinstance(message["method"], str):
                raise GrokProbeError("Native Grok requested client execution during metadata discovery; inspect CLI hooks/configuration before scheduling")
            continue  # Notifications supply no authentication or model authority.
        if type(message.get("id")) is not int or message["id"] != identity:
            raise GrokProbeError("Native Grok response identity differs; inspect the owned channel and CLI compatibility")
        if "error" in message:
            raise GrokProbeError(f"Native Grok rejected {method}; inspect native sign-in/subscription and CLI configuration")
        if not isinstance(message.get("result"), dict):
            raise GrokProbeError("Native Grok returned no metadata result; update the adapter before scheduling")
        return message["result"]


def metadata(initialized, authenticated) -> dict:
    info, account = initialized.get("_meta"), authenticated.get("_meta")
    if (initialized.get("protocolVersion") != 1 or type(initialized.get("protocolVersion")) is not int
            or not isinstance(info, dict) or not isinstance(account, dict)
            or not isinstance(info.get("agentVersion"), str) or not info["agentVersion"].strip()
            or not isinstance(initialized.get("agentCapabilities"), dict)):
        raise GrokProbeError("Native Grok initialization metadata is unavailable; inspect the installed CLI and adapter compatibility")
    if account.get("auth_mode") != "Oidc":
        raise GrokProbeError("Native Grok did not report session-based sign-in; use native grok login before refreshing this subscription route")
    if ("subscription_tier" not in account or account["subscription_tier"] is not None
            and (not isinstance(account["subscription_tier"], str) or not account["subscription_tier"].strip())
            or "gate" not in account or type(account.get("backend_billed")) is not bool):
        raise GrokProbeError("Native Grok account metadata is incomplete; inspect native sign-in/subscription and update the adapter")
    gate = account["gate"]
    if gate is not None:
        if (not isinstance(gate, dict) or not isinstance(gate.get("message"), str) or not gate["message"].strip()
                or gate.get("label") is not None and not isinstance(gate["label"], str)):
            raise GrokProbeError("Native Grok access gate is malformed; inspect native subscription status and adapter compatibility")
        gate = dict(message=gate["message"], label=gate.get("label"))
    state = info.get("modelState")
    if (not isinstance(state, dict) or not isinstance(state.get("currentModelId"), str)
            or not state["currentModelId"].strip() or not isinstance(state.get("availableModels"), list)):
        raise GrokProbeError("Native Grok model catalog is unavailable; refresh native metadata without inventing model choices")
    seen = set()
    for model in state["availableModels"]:
        if (not isinstance(model, dict) or not isinstance(model.get("modelId"), str)
                or not model["modelId"].strip() or model["modelId"] in seen
                or not isinstance(model.get("_meta", {}), dict)):
            raise GrokProbeError("Native Grok model identities are malformed; inspect the original catalog and update the adapter")
        seen.add(model["modelId"])
        efforts = model.get("_meta", {}).get("reasoningEfforts")
        if efforts is not None:
            if not isinstance(efforts, list) or any(not isinstance(item, dict)
                    or not isinstance(item.get("value"), str) or not item["value"].strip() for item in efforts):
                raise GrokProbeError("Native Grok effort choices are malformed; inspect its model catalog before selecting effort")
            if len({item["value"] for item in efforts}) != len(efforts):
                raise GrokProbeError("Native Grok effort choices are ambiguous; inspect its model catalog before selecting effort")
    authentication = dict(method="cached_token", auth_mode=account["auth_mode"],
        subscription_tier=account["subscription_tier"], gate=gate, backend_billed=account["backend_billed"])
    return dict(agent_version=info["agentVersion"], authentication=authentication,
        models=state["availableModels"], current_model=state["currentModelId"],
        capabilities=initialized["agentCapabilities"])


async def probe(binary, cwd, *, spawn=asyncio.create_subprocess_exec, clock=time.time, timer=time.monotonic) -> dict:
    if any(name in os.environ for name in API_KEY_ENVIRONMENT):
        raise GrokProbeError("Unset XAI_API_KEY and GROK_CODE_XAI_API_KEY before native subscription discovery")
    if not Path(binary).is_absolute():
        raise GrokProbeError("Supply an absolute native Grok executable path before refreshing metadata")
    try:
        binary, cwd = Path(binary).resolve(strict=True), Path(cwd).resolve(strict=True)
    except OSError:
        raise GrokProbeError("Cannot access native Grok or --cwd; restore their paths/access before refreshing metadata") from None
    if not binary.is_file() or not os.access(binary, os.X_OK) or not cwd.is_dir():
        raise GrokProbeError("Supply an executable native Grok file and an existing --cwd directory")
    started = timer()
    try:
        process = await spawn(str(binary), "--no-auto-update", "--no-subagents", "--disable-web-search", "--permission-mode", "dontAsk",
            "--sandbox", "read-only", "agent", "--no-leader", "stdio", cwd=str(cwd),
            env={**os.environ, "GROK_WORKTREE_AUTO_GC": "0"}, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        raise GrokProbeError("Cannot start native Grok metadata; restore executable access and local process resources") from None
    try:
        initialized = await exchange(process, 0, "initialize", dict(protocolVersion=1,
            clientCapabilities=dict(fs=dict(readTextFile=False, writeTextFile=False), terminal=False)))
        methods = initialized.get("authMethods")
        if (not isinstance(methods, list) or any(not isinstance(item, dict)
                or not isinstance(item.get("id"), str) or not item["id"].strip() for item in methods)
                or "cached_token" not in {item["id"] for item in methods}):
            raise GrokProbeError("Native Grok has no cached sign-in method; run native grok login before refreshing metadata")
        if len({item["id"] for item in methods}) != len(methods):
            raise GrokProbeError("Native Grok authentication methods are ambiguous; inspect CLI compatibility before refreshing")
        authenticated = await exchange(process, 1, "authenticate", dict(methodId="cached_token", _meta=dict(headless=True)))
        facts = metadata(initialized, authenticated)
        return dict(schema_version=1, kind="grok-native-metadata", route="grok-native", binary=str(binary), cwd=str(cwd),
            observed_at=clock(), probe_seconds=timer() - started, **facts, quota_observation=None, readiness="unverified",
            note="Native cached sign-in and model metadata only; no model turn requested. Quota and worker lifecycle remain unverified.")
    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--grok", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(asyncio.run(probe(args.grok, args.cwd)), indent=2, allow_nan=False))
    except GrokProbeError as exc:
        print("native-grok: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
