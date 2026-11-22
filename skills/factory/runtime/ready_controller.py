#!/usr/bin/env python3
"""Run one ready-runner attempt with a private log from login-shell startup."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", help="Fresh log path in an existing private directory")
    parser.add_argument("project", help="Working directory for the runner")
    parser.add_argument("python", help="Pinned Python 3.11+ executable for the runner")
    parser.add_argument("runner_args", nargs=argparse.REMAINDER, help="Ready runner options after --")
    args = parser.parse_args()
    for name in ("log", "project", "python"):
        if not Path(getattr(args, name)).is_absolute():
            parser.error(f"{name} must be an absolute path")

    runner = Path(__file__).resolve().with_name("ready_runner.py")
    os.umask(0o077)
    try:
        with open(args.log, "xb") as log:
            reader, writer = os.pipe()
            try:
                command = ["bash", "-lc", 'set -euo pipefail; cd -- "$1"; shift; exec "$@"',
                           "ready-controller", args.project, args.python, "-B",
                           "-X", f"factory-controller-fd={writer}", str(runner), *args.runner_args]
                try:
                    status = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                            pass_fds=(writer,)).returncode
                except OSError as exc:
                    status = 127 if isinstance(exc, FileNotFoundError) else 126
                    log.write(f"ready-controller: cannot start bash: {exc}\n".encode())
                if status == 0:
                    # Startup may have left a descendant holding the write end.
                    os.set_blocking(reader, False)
                    try:
                        started = os.read(reader, 1) == b"1"
                    except BlockingIOError:
                        started = False
                    if not started:
                        status = 1
                        log.write(b"\nready-controller: runner did not acknowledge startup; "
                                  b"check login-shell and interpreter startup before retrying\n")
            finally:
                os.close(writer)
                os.close(reader)
            if status < 0:
                status = 128 - status
            log.write(f"\ncontroller_exit={status}\n".encode())
    except OSError as exc:
        print(f"ready-controller: cannot write {args.log}: {exc}. Choose a fresh log in a writable directory.",
              file=sys.stderr)
        return 1
    print(json.dumps(dict(log=args.log, controller_exit=status)))
    if status:
        print(f"ready-controller: exited {status}; inspect {args.log}", file=sys.stderr)
    return status


if __name__ == "__main__":
    sys.exit(main())
