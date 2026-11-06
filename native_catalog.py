"""Observe native Grok/Antigravity model catalogs without starting a model turn."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


class CatalogError(Exception):
    pass


def parse_catalog(harness, output):
    if harness not in ("grok", "antigravity"):
        raise CatalogError("Choose the grok or antigravity catalog adapter")
    models, seen = [], set()
    started = harness == "antigravity"
    for line in output.splitlines():
        line = line.strip()
        if not line or line == "Fetching available models...":
            continue
        if harness == "grok":
            if line == "Available models:":
                started = True
                continue
            if not started:
                continue  # Native account/default notices remain in the original output.
            match = re.fullmatch(r"[*-]\s+(\S+)(?:\s+\(default\))?", line)
            if not match:
                raise CatalogError("Unrecognized Grok catalog; inspect the native CLI and update the adapter")
            identity, name = match[1], match[1]
        else:
            identity, separator, name = line.partition("\t")
            name = name.strip()
            if not separator or not name or any(char.isspace() for char in identity):
                raise CatalogError("Unrecognized Antigravity catalog; inspect the native CLI and update the adapter")
        if not identity or identity in seen:
            raise CatalogError("Ambiguous native model IDs; inspect the original catalog before selecting candidates")
        seen.add(identity)
        models.append(dict(id=identity, name=name))
    if not started:
        raise CatalogError("Native catalog is missing; inspect native sign-in/connection before refreshing")
    return models


def probe(harness, binary, *, run=subprocess.run, clock=time.time, timer=time.monotonic) -> dict:
    if harness not in ("grok", "antigravity") or not Path(binary).is_absolute():
        raise CatalogError("Choose grok or antigravity and supply its absolute native executable path")
    try:
        binary = Path(binary).resolve(strict=True)
    except OSError:
        raise CatalogError("Cannot access native CLI; supply its existing executable path") from None
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise CatalogError("Native CLI is not executable; restore access to the selected executable")
    started = timer()
    outputs = {}
    for command in ("--version", "models"):
        try:
            result = run([str(binary), command], stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, encoding="utf-8", timeout=20, check=False)
        except (OSError, UnicodeDecodeError, subprocess.TimeoutExpired):
            raise CatalogError(f"Cannot observe native {harness}; inspect its executable, sign-in and connection before refreshing") from None
        if result.returncode:
            raise CatalogError(f"Native {harness} {command} failed; inspect native sign-in/configuration before refreshing")
        outputs[command] = result.stdout
    version = outputs["--version"].strip()
    if not version:
        raise CatalogError("Native CLI returned no version; inspect the selected executable before refreshing")
    return dict(schema_version=1, kind="native-model-catalog", harness=harness, binary=str(binary),
                cli_version=version, observed_at=clock(), probe_seconds=timer() - started,
                models=parse_catalog(harness, outputs["models"]), native_output=outputs,
                readiness="unverified", quota_observation=None,
                note="Catalog discovery only; worker access, tools, thinking effort and allowance sharing remain unverified.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("harness", choices=("grok", "antigravity"))
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(probe(args.harness, args.binary), indent=2, allow_nan=False))
    except CatalogError as exc:
        print("native-catalog: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
