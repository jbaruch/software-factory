#!/usr/bin/env python3
"""Remind an opted-in pane to triage retained Rex exits."""
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time

STOP_WARNING = "foreman-rex-stop: check the Stop input and Rex connection"
REASON = (
    "A finished pane remains in this Rex session. Check helpers you own using "
    "the Rex helpers procedure in the factory command reference."
)
REX_ID = r":[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
UNIX_ENDPOINT = r"unix:///(?:[A-Za-z0-9._~/-]|%[0-9A-Fa-f]{2})+"
RESPONSE_LIMIT = 64 * 1024
STOP_LIMIT = 1024 * 1024


def read_object(data):
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def rex_read(binary, server, deadline, *args):
    end = min(deadline, time.monotonic() + 0.5)
    if end <= time.monotonic():
        raise TimeoutError
    with subprocess.Popen(
        [binary, "--autostart=false", "-S", server, *args],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as proc:
        assert proc.stdout is not None  # stdout=PIPE was requested
        fd = proc.stdout.fileno()
        data = bytearray()
        try:
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                    raise TimeoutError
                chunk = os.read(fd, RESPONSE_LIMIT + 1 - len(data))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > RESPONSE_LIMIT:
                    raise ValueError("Rex response exceeds byte limit")
            if proc.wait(timeout=end - time.monotonic()) != 0:
                raise subprocess.CalledProcessError(proc.returncode, proc.args)
        finally:
            if proc.poll() is None:
                proc.kill()
    return read_object(data)


def main():
    block = os.environ.get("REX_BLOCK", "")
    if os.environ.get("SOFTWARE_FACTORY_FOREMAN_BLOCK") != block or not re.fullmatch("block" + REX_ID, block):
        return
    session = os.environ.get("REX_SESSION", "")
    server = os.environ.get("REX_SERVER", "")
    binary = Path(os.environ.get("REX_BIN_DIR", "")) / "rex"
    if not (re.fullmatch("session" + REX_ID, session) and re.fullmatch(UNIX_ENDPOINT, server)
            and binary.is_absolute()):
        return

    try:
        payload = sys.stdin.buffer.read(STOP_LIMIT + 1)
        if len(payload) > STOP_LIMIT:
            raise ValueError("Stop input exceeds byte limit")
        active = read_object(payload)["stop_hook_active"]
        if type(active) is not bool:
            raise ValueError("Expected a boolean Stop follow-up marker")
        if active:
            return

        deadline = time.monotonic() + 2
        view = rex_read(binary, server, deadline, "session", "inspect", session, "--json")
        if view["session_id"] != session:
            return
        blocks = {item["block_id"] for window in view.get("windows", [])
                  for layer in window["layers"] for item in layer["blocks"]}
        if block not in blocks:
            return
        for sibling in sorted(blocks - {block}):
            if not re.fullmatch("block" + REX_ID, sibling):
                raise ValueError("Expected a full Rex block ID")
            process = rex_read(binary, server, deadline, "block", "call", "-s", session, "-b", sibling, "process")
            last_exit = process.get("last_exit")
            if last_exit is None:
                continue
            if not isinstance(last_exit, dict) or type(last_exit.get("exit_code")) is not int:
                raise ValueError("Expected an integer Rex exit code")
            if process.get("child") is None and process.get("foreground") is None:
                print(json.dumps({"decision": "block", "reason": REASON}))
                return
    except (OSError, ValueError, TypeError, KeyError, RecursionError, subprocess.SubprocessError):
        print(STOP_WARNING, file=sys.stderr)


if __name__ == "__main__":
    main()
