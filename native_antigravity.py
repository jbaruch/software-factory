"""Observe native Antigravity weekly quota groups without a model turn."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from codex_worker import native_environment

API_ENVIRONMENT = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "AGY_ADC_AUTH")


class UsageError(Exception):
    pass


def parse_usage(output):
    """Read the observed four-column `agy --print /usage` format."""
    groups, seen = [], set()
    for line in output.splitlines():
        fields = line.split("\t")
        if (len(fields) != 4 or not fields[0].strip() or fields[0] in seen
                or fields[1] != "Weekly Limit Remaining"
                or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?%", fields[2])):
            raise UsageError("Unsupported Antigravity usage format; inspect the native /usage report and update its reader before scheduling")
        group, _, percentage, reset = fields
        remaining = float(percentage[:-1])
        try:
            parsed = datetime.fromisoformat(reset.replace("Z", "+00:00"))
        except ValueError:
            raise UsageError("Invalid Antigravity quota reset; inspect the original usage report before scheduling") from None
        if not 0 <= remaining <= 100 or parsed.utcoffset() is None:
            raise UsageError("Invalid Antigravity quota value/reset; inspect the original usage report before scheduling")
        seen.add(group)
        groups.append(dict(group=group, window="weekly", percent_remaining=remaining, resets_at=reset))
    if not groups:
        raise UsageError("Antigravity returned no quota groups; inspect native sign-in and /usage before scheduling")
    return groups


def probe(binary, cwd, *, run=subprocess.run, clock=time.time, timer=time.monotonic):
    if any(name in os.environ for name in API_ENVIRONMENT):
        raise UsageError("Unset Gemini API/ADC environment overrides before observing the native subscription route")
    if not Path(binary).is_absolute():
        raise UsageError("Supply an absolute native Antigravity executable before observing quota")
    try:
        binary, cwd = Path(binary).resolve(strict=True), Path(cwd).resolve(strict=True)
    except OSError:
        raise UsageError("Restore the selected native Antigravity executable and working directory before refreshing quota") from None
    if not binary.is_file() or not os.access(binary, os.X_OK) or not cwd.is_dir():
        raise UsageError("Supply an executable native Antigravity file and existing working directory")
    started, outputs = timer(), {}
    for command in (("--version",), ("--print", "/usage")):
        try:
            result = run([str(binary), *command], cwd=str(cwd), env=native_environment(),
                         stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=20, check=False)
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            raise UsageError("Cannot observe native Antigravity quota; inspect its installation, sign-in and connection before refreshing") from None
        if result.returncode or result.stderr:
            raise UsageError("Native Antigravity quota observation failed; inspect native sign-in/configuration and /usage before refreshing")
        outputs[command[0]] = result.stdout
    version = outputs["--version"].strip()
    if not version:
        raise UsageError("Antigravity returned no CLI version; restore its installation before refreshing quota")
    return dict(schema_version=1, kind="antigravity-native-usage", binary=str(binary), cwd=str(cwd),
                cli_version=version, observed_at=clock(), probe_seconds=timer() - started,
                groups=parse_usage(outputs["--print"]), native_output=outputs["--print"],
                readiness="unverified",
                note="Quota groups only; account eligibility, model-to-group binding, reservations and worker readiness require separate evidence.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(probe(args.binary, args.cwd), indent=2, allow_nan=False))
    except UsageError as exc:
        print("native-antigravity: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
