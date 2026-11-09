"""Observe sanitized native Claude authentication metadata without a model turn."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True  # Installed copies stay free of generated files.
import time
from typing import NoReturn

from codex_worker import native_environment
from factory import decode_json

AUTH_METHODS = ("none", "claude.ai", "oauth_token", "api_key", "api_key_helper", "third_party")
API_PROVIDERS = ("firstParty", "bedrock", "vertex", "foundry")
# Refuse even empty selectors instead of silently observing a different account.
OVERRIDE_PREFIXES = ("ANTHROPIC_", "CLAUDE_", "AWS_", "BEDROCK_", "GOOGLE_",
                     "GCLOUD_", "GCP_", "CLOUDSDK_", "CLOUD_ML_", "VERTEX_", "AZURE_", "FOUNDRY_")
CONFIG_DIRECTORY = "CLAUDE_CONFIG_DIR"


class ClaudeProbeError(Exception):
    pass


def curate(output, *, returncode):
    """Pure strict JSON curation; unknown/private fields never enter the result."""
    if type(returncode) is not int or returncode not in (0, 1):
        raise ClaudeProbeError("Unsupported Claude auth status exit; inspect native installation/sign-in before refreshing")
    if not isinstance(output, str):
        raise ClaudeProbeError("Claude auth status must be UTF-8 JSON text; inspect the CLI and update the adapter")
    try:
        payload = decode_json(output)
    except (ValueError, UnicodeError, RecursionError):
        raise ClaudeProbeError("Malformed Claude auth status JSON; inspect the CLI and update the adapter before refreshing") from None
    if not isinstance(payload, dict):
        raise ClaudeProbeError("Claude auth status must be a JSON object; inspect the CLI and update the adapter")
    logged_in, method, provider = (payload.get(key) for key in ("loggedIn", "authMethod", "apiProvider"))
    if (type(logged_in) is not bool or not isinstance(method, str) or method not in AUTH_METHODS
            or not isinstance(provider, str) or provider not in API_PROVIDERS):
        raise ClaudeProbeError("Unsupported Claude auth facts; inspect native status and update the adapter's field contract")
    if logged_in != (returncode == 0) or logged_in != (method != "none"):
        raise ClaudeProbeError("Contradictory Claude sign-in facts/exit; inspect native status before refreshing")
    return dict(loggedIn=logged_in, authMethod=method, apiProvider=provider)


def selected_path(value, *, directory=False):
    """Check selected paths using metadata only, never opening configuration."""
    try:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError
        path = path.resolve(strict=True)
        accessible = (path.is_dir() and os.access(path, os.R_OK | os.X_OK) if directory
                      else path.is_file() and os.access(path, os.X_OK))
        if not accessible:
            raise ValueError
        return path
    except (OSError, ValueError, TypeError, RuntimeError):
        if directory:
            raise ClaudeProbeError("Supply an accessible absolute trusted --cwd/config directory; restore access without changing the intended native account") from None
        raise ClaudeProbeError("Supply an accessible absolute executable --binary for the installed native Claude CLI") from None


def probe(binary, cwd, *, run=subprocess.run, clock=time.time, timer=time.monotonic):
    if any(name != CONFIG_DIRECTORY and name.startswith(OVERRIDE_PREFIXES) for name in os.environ):
        raise ClaudeProbeError("Unset Claude API/provider/auth environment overrides before observing native sign-in; do not substitute credentials or accounts")
    binary, cwd = selected_path(binary), selected_path(cwd, directory=True)
    environment = native_environment()
    if CONFIG_DIRECTORY in os.environ:
        # The shared environment intentionally omits Claude-specific selectors.
        # Preserve this explicit selection, after metadata-only validation.
        selected_path(os.environ[CONFIG_DIRECTORY], directory=True)
        environment[CONFIG_DIRECTORY] = os.environ[CONFIG_DIRECTORY]
    started_at, started = clock(), timer()
    outputs = []
    for command in (("--version",), ("--safe-mode", "--restricted", "auth", "status", "--json")):
        try:
            result = run([str(binary), *command], cwd=str(cwd), env=environment,
                         stdin=subprocess.DEVNULL, capture_output=True, text=True,
                         encoding="utf-8", errors="strict", timeout=20, check=False)
        except subprocess.TimeoutExpired:
            raise ClaudeProbeError("Claude metadata command timed out; inspect the installation/connection before an explicit refresh") from None
        except (OSError, UnicodeError, ValueError):
            raise ClaudeProbeError("Cannot read Claude metadata; inspect executable/cwd access and UTF-8 CLI compatibility before refreshing") from None
        if command == ("--version",):
            if (type(result.returncode) is not int or result.returncode != 0
                    or not isinstance(result.stdout, str)
                    or re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}(?: \(Claude Code\))?", result.stdout.strip()) is None):
                raise ClaudeProbeError("Unsupported Claude CLI version response; restore the selected native installation or update the adapter")
            version = result.stdout.strip()
            # 2.1.317 fixes auth status exiting before refreshed tokens are saved:
            # https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md#21317
            if tuple(int(part) for part in version.split(" ", 1)[0].split(".")) < (2, 1, 317):
                raise ClaudeProbeError("Update the selected native Claude CLI to 2.1.317 or newer before observing auth status")
            outputs.append(version)
        else:
            outputs.append(curate(result.stdout, returncode=result.returncode))
    observed_at, duration = clock(), timer() - started
    if (any(type(value) not in (int, float) or not 0 <= value <= sys.float_info.max
            or not math.isfinite(value) for value in (started_at, observed_at, duration))
            or observed_at < started_at):
        raise ClaudeProbeError("Invalid observation clock/duration; restore the host clock before refreshing")
    return dict(schema_version=1, kind="claude-native-auth", harness="claude", binary=str(binary), cwd=str(cwd),
                cli_version=outputs[0], started_at=started_at, observed_at=observed_at, probe_seconds=duration,
                auth=outputs[1], readiness="unverified", quota_observation=None, subscription_eligibility=None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        output = json.dumps(probe(args.binary, args.cwd), indent=2, allow_nan=False)
    except ClaudeProbeError as exc:
        print("native-claude: " + str(exc), file=sys.stderr)
        return 1
    print(output)
    return 0


def environment():
    """Retain native account selection; refuse routing/auth overrides even if empty."""
    if any(name != CONFIG_DIRECTORY and name.startswith(OVERRIDE_PREFIXES) for name in os.environ):
        raise ClaudeProbeError("Unset Claude API/provider/auth overrides; restore the intended native subscription")
    result = native_environment()
    if CONFIG_DIRECTORY in os.environ:
        selected_path(os.environ[CONFIG_DIRECTORY], directory=True)
        result[CONFIG_DIRECTORY] = os.environ[CONFIG_DIRECTORY]
    return result


def validate_auth(observation, binary, cwd):
    if (not isinstance(observation, dict) or type(observation.get("schema_version")) is not int
            or observation["schema_version"] != 1 or observation.get("kind") != "claude-native-auth"
            or observation.get("harness") != "claude" or observation.get("binary") != str(binary)
            or observation.get("cwd") != str(cwd)
            or observation.get("auth") != dict(loggedIn=True, authMethod="claude.ai", apiProvider="firstParty")
            or not supported_version(observation.get("cli_version"), suffix=" (Claude Code)")
            or any(type(observation.get(k)) not in (int, float) or not 0 <= observation[k] <= sys.float_info.max
                   for k in ("started_at", "observed_at", "probe_seconds"))
            or observation["observed_at"] < observation["started_at"]):
        raise ClaudeProbeError("Restore native claude.ai/firstParty sign-in on Claude Code 2.1.319 or newer before execution")


# The oldest Claude Code whose event shapes were observed. Newer releases are accepted;
# the strict decoder still refuses any shape it does not recognize.
MINIMUM_VERSION = (2, 1, 319)
# Tool lists exactly as Claude Code reports them in its init event.
READ_TOOLS = ["Read", "StructuredOutput"]
WRITE_TOOLS = ["Bash", "Edit", "Glob", "Grep", "Read", "StructuredOutput", "Write"]


def supported_version(value, *, suffix=""):
    if not isinstance(value, str) or not value.endswith(suffix):
        return False
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", value[:len(value) - len(suffix)])
    return match is not None and tuple(int(part) for part in match.groups()) >= MINIMUM_VERSION
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def write_command(model, effort, session, turns):
    """An implementation specialist with edit tools and a shell, in bypassPermissions mode."""
    from codex_worker import outcome_schema
    # Operator policy: agents run unconfined and never prompt. A writer that commits on
    # its own still fails the factory commit's HEAD check.
    return ["--safe-mode", "--print", "--tools", "Read", "Edit", "Write", "Glob", "Grep", "Bash",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disallowedTools", "mcp__*", "--disable-slash-commands", "--no-chrome",
            "--permission-mode", "bypassPermissions", "--permission-prompts", "none",
            "--model", model, "--effort", effort, "--session-id", session,
            "--max-turns", str(turns), "--no-session-persistence",
            "--system-prompt", "You are the assigned implementation specialist. Follow the supplied assignment. "
            "Edit and test only inside this checkout. Do not delegate, stage, commit or change Git state; the "
            "factory commits your finished work after a completed report.",
            "--json-schema", json.dumps(outcome_schema()), "--output-format", "stream-json",
            "--verbose", "--include-hook-events"]


def read_command(model, effort, session, turns, coordination=None):
    """A read-only specialist; a bounded coordination answer extends its report schema."""
    from codex_worker import outcome_schema
    return ["--safe-mode", "--print", "--tools", "Read",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disallowedTools", "mcp__*", "--disable-slash-commands", "--no-chrome",
            "--permission-mode", "bypassPermissions", "--permission-prompts", "none",
            "--model", model, "--effort", effort, "--session-id", session,
            "--max-turns", str(turns), "--no-session-persistence",
            "--system-prompt", "You are an independent read-only specialist. Follow the supplied assignment. "
            "Use only Read and StructuredOutput. Do not delegate, execute commands or change files.",
            "--json-schema", json.dumps(outcome_schema(coordination)), "--output-format", "stream-json",
            "--verbose", "--include-hook-events"]


def parse_execution(events, *, session_id, cwd, model, max_turns, writer=False, coordination=None):
    """Decode the observed native lifecycle without supplying process/provenance authority."""
    def refuse() -> NoReturn:
        raise ClaudeProbeError("Unsupported or conflicting Claude lifecycle; inspect the original stream and pane without retrying")
    # Agents run in bypassPermissions mode, so tools may reach outside the checkout like
    # any shell command can. Only shapes are checked; the factory commit takes the worktree alone.
    def text(value):
        return isinstance(value, str) and bool(value)
    def bash_call(identity):
        return text(identity) and tools.get(identity) == "Bash"
    def known_task(identity):
        return text(identity) and identity in tasks
    expected_tools = WRITE_TOOLS if writer else READ_TOOLS
    callable_tools = set(expected_tools)
    report, final, pending, tools, rates = None, None, {}, {}, []
    initialized, denied, tasks = False, {}, set()
    for event in events:
        if (not isinstance(event, dict) or final is not None or event.get("session_id") != session_id):
            refuse()
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "ui_invalidate":
            # Observed 2.1.319 rendering notification in bypassPermissions runs, even before init.
            # It carries no report, usage or permission authority.
            if not isinstance(event.get("event"), str):
                refuse()
            continue
        if not initialized:
            if (kind != "system" or event.get("subtype") != "init" or event.get("cwd") != cwd
                    or event.get("model") != model or event.get("tools") != expected_tools
                    or event.get("mcp_servers") != []
                    # bypassPermissions now; the confined modes remain readable in retained streams.
                    or event.get("permissionMode") not in ({"acceptEdits", "bypassPermissions"} if writer
                                                           else {"dontAsk", "bypassPermissions"})
                    or event.get("slash_commands") != [] or event.get("apiKeySource") != "none"
                    or not supported_version(event.get("claude_code_version"))
                    or event.get("additional_directories") != []):
                refuse()
            initialized = True
        elif kind == "assistant":
            message = event.get("message")
            if (not isinstance(message, dict) or message.get("model") != model
                    or event.get("parent_tool_use_id") is not None or not isinstance(message.get("content"), list)):
                refuse()
            for block in message["content"]:
                if not isinstance(block, dict) or block.get("type") not in ("text", "thinking", "tool_use"):
                    refuse()
                if block["type"] == "tool_use":
                    name, identity = block.get("name"), block.get("id")
                    if name not in callable_tools or not isinstance(identity, str) or not identity or identity in tools:
                        refuse()
                    tools[identity] = name
                    pending[identity] = block
                    if name == "Read" and (not isinstance(block.get("input"), dict)
                            or not isinstance(block["input"].get("file_path"), str)
                            or not Path(block["input"]["file_path"]).is_absolute()):
                        refuse()
                    if name in ("Edit", "Write") and (not isinstance(block.get("input"), dict)
                            or not isinstance(block["input"].get("file_path"), str)
                            or not Path(block["input"]["file_path"]).is_absolute()):
                        refuse()
                    if name == "Bash" and (not isinstance(block.get("input"), dict)
                            or not isinstance(block["input"].get("command"), str)):
                        refuse()
                    if name in ("Glob", "Grep") and not isinstance(block.get("input"), dict):
                        refuse()
                    if name == "StructuredOutput":
                        if report is not None:
                            refuse()
                        report = block.get("input")
        elif kind == "user":
            message = event.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("content"), list) or event.get("parent_tool_use_id") is not None:
                refuse()
            for block in message["content"]:
                if not isinstance(block, dict) or block.get("type") != "tool_result" or not isinstance(block.get("tool_use_id"), str) or block["tool_use_id"] not in pending:
                    refuse()
                call = pending.pop(block["tool_use_id"])
                name = call["name"]
                if (type(block.get("is_error", False)) is not bool
                        or (block["tool_use_id"] in denied and block.get("is_error") is not True)):
                    refuse()
                if name == "Read" and block.get("is_error", False) is False:
                    result = event.get("tool_use_result")
                    if not isinstance(result, dict) or not isinstance(result.get("file"), dict):
                        refuse()
                    path = result["file"].get("filePath")
                    if not isinstance(path, str) or path != call["input"]["file_path"]:
                        refuse()
                if name in ("Edit", "Write") and block.get("is_error", False) is False:
                    result = event.get("tool_use_result")
                    if not isinstance(result, dict) or result.get("filePath") != call["input"]["file_path"]:
                        refuse()
                if name in ("Glob", "Grep") and block.get("is_error", False) is False and (
                        ("path" in call["input"] and not text(call["input"]["path"]))
                        or (name == "Glob" and not text(call["input"].get("pattern")))
                        or (name == "Grep" and "glob" in call["input"] and not text(call["input"]["glob"]))):
                    refuse()
                if name == "StructuredOutput" and block.get("is_error", False) is not False:
                    refuse()
        elif kind == "rate_limit_event":
            if not isinstance(event.get("rate_limit_info"), dict):
                refuse()
            rates.append(event["rate_limit_info"])
        elif kind == "system" and event.get("subtype") == "thinking_tokens":
            # Inspection-only estimates; final actual usage remains authoritative.
            if any(type(event.get(key)) is not int or event[key] < 0
                   for key in ("estimated_tokens", "estimated_tokens_delta")):
                refuse()
        elif kind == "system" and event.get("subtype") == "api_retry":
            # The CLI retries transient API failures on its own; the final result stays authoritative.
            if any(type(event.get(key)) is not int or event[key] < 0
                   for key in ("attempt", "max_retries", "retry_delay_ms")):
                refuse()
        elif kind == "tool_progress":
            # Heartbeats while a long Bash call runs.
            if (event.get("tool_name") != "Bash" or not bash_call(event.get("parent_tool_use_id"))
                    or not text(event.get("tool_use_id")) or type(event.get("elapsed_time_seconds")) is not int):
                refuse()
        elif kind == "system" and event.get("subtype") == "task_started":
            # Claude Code tracks long or backgrounded Bash calls as tasks. Their output still reaches
            # the agent as tool results, so task lifecycle events carry no report or usage authority.
            if (not bash_call(event.get("tool_use_id")) or event.get("task_type") != "local_bash"
                    or not text(event.get("task_id")) or event["task_id"] in tasks):
                refuse()
            tasks.add(event["task_id"])
        elif kind == "system" and event.get("subtype") == "task_updated":
            if not known_task(event.get("task_id")) or not isinstance(event.get("patch"), dict):
                refuse()
        elif kind == "system" and event.get("subtype") == "task_notification":
            if not known_task(event.get("task_id")) or not text(event.get("status")):
                refuse()
        elif kind == "system" and event.get("subtype") == "background_tasks_changed":
            if (not isinstance(event.get("tasks"), list)
                    or any(not isinstance(task, dict) or not known_task(task.get("task_id")) for task in event["tasks"])):
                refuse()
        elif (kind == "system" and event.get("subtype") == "permission_denied"
                and event.get("tool_name") == "SandboxNetworkAccess"):
            # A writer's sandbox refused a network connection. It is not a tool call and
            # never appears in the final permission_denials; the command itself saw the error.
            if not writer or not isinstance(event.get("tool_use_id"), str) or not event["tool_use_id"]:
                refuse()
        elif kind == "system" and event.get("subtype") == "permission_denied":
            identity, name = event.get("tool_use_id"), event.get("tool_name")
            if (name not in callable_tools - {"StructuredOutput"} or not isinstance(identity, str)
                    or tools.get(identity) != name or identity not in pending or identity in denied):
                refuse()
            denied[identity] = dict(tool_name=name, tool_use_id=identity, tool_input=pending[identity]["input"])
        elif kind == "result":
            if (pending or report is None or event.get("subtype") != "success" or event.get("is_error") is not False
                    or event.get("terminal_reason") != "completed" or event.get("api_error_status") is not None
                    or type(event.get("num_turns")) is not int or not 1 <= event["num_turns"] <= max_turns
                    or type(event.get("safety_stops")) is not int or event["safety_stops"] != 0
                    or not isinstance(event.get("subagent_stats"), dict)
                    or type(event["subagent_stats"].get("spawned")) is not int or event["subagent_stats"]["spawned"] != 0
                    or event.get("permission_denials") != list(denied.values())
                    or event.get("structured_output") != report):
                refuse()
            try:
                if decode_json(event.get("result")) != report:
                    refuse()
            except (ValueError, TypeError):
                refuse()
            final = event
        else:
            refuse()
    if (final is None or not isinstance(report, dict)
            or set(report) != ({"outcome", "artifacts", "answer"} if coordination else {"outcome", "artifacts"})
            or report.get("outcome") not in ("approved", "completed", "findings", "blocked")
            or not isinstance(report.get("artifacts"), list) or not report["artifacts"]
            or any(not isinstance(ref, str) or not ref.strip() for ref in report["artifacts"])):
        refuse()
    if coordination is not None:
        import coordination_evidence
        import factory
        try:
            coordination_evidence.validate_answer(report["answer"], coordination, report["outcome"])
        except factory.FactoryError:
            refuse()
    usage, model_usage = final.get("usage"), final.get("modelUsage")
    if (not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ("input_tokens", "output_tokens"))
            or not isinstance(model_usage, dict) or set(model_usage) != {model}
            or not isinstance(model_usage[model], dict) or model_usage[model].get("provider") != "firstParty"
            or model_usage[model].get("canonicalModel") != model
            or any(type(model_usage[model].get(k)) is not int or model_usage[model][k] < 0
                   for k in ("inputTokens", "outputTokens"))
            or model_usage[model]["inputTokens"] != usage["input_tokens"]
            or model_usage[model]["outputTokens"] != usage["output_tokens"]):
        refuse()
    return dict(report=report, usage=usage, model_usage=model_usage, turns=final["num_turns"], rate_limits=rates)


if __name__ == "__main__":
    raise SystemExit(main())
