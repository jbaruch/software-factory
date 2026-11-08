"""Native Grok metadata discovery and final-event decoding."""
import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import tomllib

from factory import decode_json, RESULT_OUTCOMES
from codex_worker import native_environment, stop_child

API_KEY_ENVIRONMENT = ("XAI_API_KEY", "GROK_CODE_XAI_API_KEY")
READ_OPTIONS = ("--tools", "read_file", "--deny", "Write", "--deny", "Edit", "--deny", "Bash",
                "--deny", "MCPTool(*)", "--deny", "WebFetch", "--deny", "WebSearch",
                "--deny", "Task", "--deny", "Workflow")


class GrokProbeError(Exception):
    pass


def require_execution_boundary():
    """Hold execution until the native boundary meets the assignment contract."""
    raise GrokProbeError(
        "Grok execution is held: native sandbox permissions do not confine reads to the assigned checkout, "
        "and configuration inspection does not freeze executable integrations. "
        "Select another eligible route; retain existing Grok artifacts for inspection. "
        "Re-enable only after a compatible boundary passes live conformance.")


def validate_configuration(value, cwd):
    """Validate retained configuration evidence without rereading mutable files."""
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 2 or value.get("kind") != "grok-subscription-configuration"
            or value.get("cwd") != str(Path(cwd).resolve())
            or value.get("route") != "cached-session-without-routing-overrides"
            or value.get("sandbox") != "strict" or value.get("tools") != ["read_file"]
            or value.get("no_executable_customizations") is not True
            or not isinstance(value.get("grok_home"), str) or "\0" in value["grok_home"]
            or not Path(value["grok_home"]).is_absolute()
            or not isinstance(value.get("cli_version"), str) or not value["cli_version"].strip()
            or not isinstance(value.get("layers"), list)
            or any(type(value.get(key)) not in (int, float) or not 0 <= value[key] <= sys.float_info.max
                   for key in ("observed_at", "probe_seconds"))):
        raise GrokProbeError("Unsupported Grok configuration evidence; inspect the original observation and update its reader before execution or collection")
    for layer in value["layers"]:
        if (not isinstance(layer, dict) or not isinstance(layer.get("path"), str)
                or "\0" in layer["path"] or not Path(layer["path"]).is_absolute()
                or not isinstance(layer.get("role"), str) or not layer["role"].strip()
                or not isinstance(layer.get("sha256"), str) or len(layer["sha256"]) != 64
                or any(character not in "0123456789abcdef" for character in layer["sha256"])):
            raise GrokProbeError("Malformed Grok configuration layer evidence; inspect its original path, role and SHA-256 before execution or collection")


def validate_discovery(value, binary, cwd):
    """Apply the native metadata contract to both fresh and retained evidence."""
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or value.get("kind") != "grok-native-metadata"
            or value.get("route") != "grok-native" or value.get("binary") != str(binary)
            or value.get("cwd") != str(Path(cwd).resolve())
            or value.get("readiness") != "unverified" or "quota_observation" not in value
            or value["quota_observation"] is not None
            or any(type(value.get(key)) not in (int, float) or not 0 <= value[key] <= sys.float_info.max
                   for key in ("observed_at", "probe_seconds"))
            or not isinstance(value.get("authentication"), dict)
            or value["authentication"].get("method") != "cached_token"):
        raise GrokProbeError("Unsupported Grok discovery evidence; inspect the original metadata and update its reader before execution or collection")
    validate_configuration(value.get("configuration"), cwd)
    metadata(dict(protocolVersion=1, agentCapabilities=value.get("capabilities"),
                  _meta=dict(agentVersion=value.get("agent_version"),
                             modelState=dict(currentModelId=value.get("current_model"),
                                             availableModels=value.get("models")))),
             dict(_meta=value["authentication"]))


def environment():
    """Keep the selected native profile without forwarding host credentials."""
    if any(key.startswith("GROK_") and key not in {"GROK_HOME", "GROK_WORKTREE_AUTO_GC"} for key in os.environ):
        raise GrokProbeError("Unset unsupported Grok environment overrides before subscription execution; only GROK_HOME selects a profile")
    result = {**native_environment(), "GROK_WORKTREE_AUTO_GC": "0"}
    if "GROK_HOME" in os.environ:
        result["GROK_HOME"] = os.environ["GROK_HOME"]
    return result


def read_options(cwd):
    home = Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok"))).expanduser()
    if not home.is_absolute():
        home = Path(cwd) / home
    home = home.resolve()
    if any(character in str(home) for character in "*?[]()\\"):
        raise GrokProbeError("Select a Grok profile path without permission-pattern characters before read-only execution")
    return (*READ_OPTIONS, "--allow", "Read(./**)", "--deny", "Read(~/.grok/**)", "--deny", f"Read({home}/**)")


async def subscription_configuration(binary, cwd, *, spawn=asyncio.create_subprocess_exec):
    """Refuse routing overrides rather than reproduce Grok's config merge."""
    require_execution_boundary()
    if any(key in os.environ for key in (*API_KEY_ENVIRONMENT, "GROK_AUTH", "GROK_CONFIG", "GROK_CONFIG_PATH",
            "GROK_MODELS_BASE_URL", "GROK_MODELS_LIST_URL", "GROK_AUTH_PROVIDER_COMMAND")):
        raise GrokProbeError("Unset Grok API, endpoint, auth-provider and config-overlay environment overrides before subscription execution")
    process, started = None, time.monotonic()
    try:
        process = await spawn(str(binary), "--no-auto-update", *read_options(cwd), "inspect", "--json", cwd=str(cwd), env=environment(),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        if process.returncode or stderr:
            raise GrokProbeError("Cannot inspect native Grok configuration; inspect the selected profile and native inspect command before execution")
        result = parse_configuration(decode_json(stdout), cwd)
        observation = {**result, "observed_at": time.time(), "probe_seconds": time.monotonic() - started}
        validate_configuration(observation, cwd)
        return observation
    except (OSError, ValueError, UnicodeError, TimeoutError):
        raise GrokProbeError("Cannot establish Grok subscription configuration; inspect its native configuration layers before execution") from None
    finally:
        if process is not None:
            await stop_child(process)


def parse_configuration(report, cwd) -> dict:
    if (not isinstance(report, dict) or report.get("cwd") != str(Path(cwd).resolve())
            or report.get("projectTrusted") is not True
            or not isinstance(report.get("grokVersion"), str) or not report["grokVersion"].strip()
            or not isinstance(report.get("configSources"), dict)
            or not isinstance(report["configSources"].get("layers"), list)
            or report.get("experiments") != []):
        raise GrokProbeError("Native Grok configuration is untrusted, unsupported or experimental; inspect and explicitly trust only the assigned repository before execution")
    if (any(report.get(key) != [] for key in ("hooks", "mcpServers", "lspServers"))
            or not isinstance(report.get("plugins"), list)
            or any(not isinstance(plugin, dict) or plugin.get("enabled") is not False for plugin in report["plugins"])):
        raise GrokProbeError("Grok executable hooks, plugins, MCP or LSP configuration is unavailable for read-only assignments; select a profile and checkout without executable customization")
    layers = []
    for layer in report["configSources"]["layers"]:
        if (not isinstance(layer, dict) or not isinstance(layer.get("path"), str)
                or not Path(layer["path"]).is_absolute() or not isinstance(layer.get("role"), str)):
            raise GrokProbeError("Native Grok configuration layer is opaque; inspect its effective authentication and model routing before execution")
        try:
            raw = Path(layer["path"]).read_bytes()
            config = tomllib.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeError):
            raise GrokProbeError("Cannot inspect a native Grok configuration layer; restore readable TOML before subscription execution") from None
        if any(key in config for key in ("model", "endpoints", "auth_provider", "auth", "grok_com_config",
                "experiments", "campaigns", "version_overrides")):
            raise GrokProbeError("Grok authentication/model overrides are unsupported for subscription execution; select an unmodified native subscription profile")
        layers.append(dict(path=layer["path"], role=layer["role"], sha256=hashlib.sha256(raw).hexdigest()))
    home = Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok"))).expanduser()
    if not home.is_absolute():
        home = Path(cwd) / home
    return dict(schema_version=2, kind="grok-subscription-configuration", cwd=str(Path(cwd).resolve()),
                grok_home=str(home.resolve()), cli_version=report["grokVersion"], layers=layers,
                route="cached-session-without-routing-overrides", sandbox="strict", tools=["read_file"],
                no_executable_customizations=True)


def parse_execution(events, *, session_id, cwd, max_turns):
    """Decode headless Grok's final event; text fragments carry no result."""
    final = None
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise GrokProbeError("Malformed native Grok event; inspect its original stream before collection")
        if final is not None:
            raise GrokProbeError("Native Grok emitted data after its final event; inspect the original stream before collection")
        if event["type"] == "error":
            raise GrokProbeError("Native Grok reported an execution error; inspect the original stream without retrying")
        if event["type"] == "end":
            final = event
    if (final is None or final.get("stopReason") != "end_turn"
            or not isinstance(session_id, str) or not session_id.strip()
            or not isinstance(cwd, str) or not Path(cwd).is_absolute()
            or final.get("sessionId") != session_id or final.get("cwd") != cwd
            or not isinstance(final.get("requestId"), str) or not final["requestId"].strip()
            or type(max_turns) is not int or max_turns < 1
            or type(final.get("num_turns")) is not int or not 1 <= final["num_turns"] <= max_turns):
        raise GrokProbeError("Native Grok completion differs from the frozen session, directory or turn bound; inspect its original stream before collection")
    report = final.get("structuredOutput")
    if (not isinstance(report, dict) or set(report) != {"outcome", "artifacts"}
            or not isinstance(report["outcome"], str) or report["outcome"] not in RESULT_OUTCOMES
            or not isinstance(report["artifacts"], list)
            or any(not isinstance(value, str) or not value.strip() for value in report["artifacts"])):
        raise GrokProbeError("Native Grok has no supported final structured report; inspect its original stream and update the adapter before collection")
    usage, models = final.get("usage"), final.get("modelUsage")
    if usage is not None and (not isinstance(usage, dict)
            or any(type(value) is not int or value < 0 or value > sys.float_info.max for value in usage.values())):
        raise GrokProbeError("Native Grok token usage is malformed; inspect the original final event before collection")
    if models is not None:
        if (not isinstance(models, dict) or any(not isinstance(model, str) or not model.strip()
                or not isinstance(values, dict) or any(type(value) not in (int, float)
                    or value < 0 or value > sys.float_info.max
                    or type(value) is float and not math.isfinite(value) for value in values.values())
                for model, values in models.items())):
            raise GrokProbeError("Native Grok model usage is malformed; inspect the original final event before collection")
    return dict(report=report, session_id=session_id, request_id=final["requestId"],
                usage=usage, model_usage=models, turns=final["num_turns"])


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


async def probe(binary, cwd, *, spawn=asyncio.create_subprocess_exec,
                configuration=subscription_configuration, clock=time.time, timer=time.monotonic) -> dict:
    require_execution_boundary()
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
    config = await configuration(binary, cwd)
    validate_configuration(config, cwd)
    try:
        process = await spawn(str(binary), "--no-auto-update", "--no-subagents", "--disable-web-search", "--permission-mode", "dontAsk",
            "--sandbox", "strict", *read_options(cwd), "agent", "--no-leader", "stdio", cwd=str(cwd),
            env=environment(), stdin=asyncio.subprocess.PIPE,
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
        observation = dict(schema_version=1, kind="grok-native-metadata", route="grok-native", binary=str(binary), cwd=str(cwd),
            observed_at=clock(), probe_seconds=timer() - started, configuration=config, **facts, quota_observation=None, readiness="unverified",
            note="Native cached sign-in and model metadata only; no model turn requested. Quota and worker lifecycle remain unverified.")
        validate_discovery(observation, binary, cwd)
        return observation
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
