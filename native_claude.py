"""Observe sanitized native Claude authentication metadata without a model turn."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time

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
            outputs.append(result.stdout.strip())
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


if __name__ == "__main__":
    raise SystemExit(main())
