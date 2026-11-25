"""Run one trusted Go command under the inherited cache admission locks."""
import ctypes
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time


def main():
    seconds = int(sys.argv[1])
    command = sys.argv[2:]
    # Factory supplies the verified scratch parent as cwd. Own each allocation
    # here so losing Factory cannot interrupt cleanup after the Go group drains.
    with tempfile.TemporaryDirectory(prefix="factory-go-trim-", dir=Path.cwd()) as temporary:
        os.environ.update(HOME=temporary, TMPDIR=temporary, GOTMPDIR=temporary, GOPATH=temporary)
        # Go reads telemetry mode from its user config. Disable the detached
        # telemetry child before even "go env" can start it in this fresh HOME.
        config = "Library/Application Support" if sys.platform == "darwin" else ".config"
        telemetry = Path(temporary) / config / "go/telemetry"
        telemetry.mkdir(parents=True)
        (telemetry / "mode").write_text("off\n")
        package = Path(temporary) / "src/factory.local/trim"
        package.mkdir(parents=True)
        # The stable import path reuses the probe's cache entries across hourly runs.
        (package / "trim.go").write_text("package trim\nconst Value = 1\n")
        return run(command, seconds, package)


def run(command, seconds, package):
    if sys.platform == "linux":
        # PR_SET_CHILD_SUBREAPER makes this private keeper adopt orphaned Go tools.
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            print(f"Cannot enable Go keeper child subreaper: {os.strerror(ctypes.get_errno())}", file=sys.stderr)
            return 1
    # Wake on driver status change without reaping it: its PID pins our signal target.
    reader, writer = os.pipe()
    child = None

    def child_changed(signum, _frame):
        if sys.platform == "linux" and (child is None or not os.waitid(
                os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)):
            return  # An adopted compiler may exit before the driver.
        signal.signal(signum, signal.SIG_DFL)
        os.write(writer, b".")

    signal.signal(signal.SIGCHLD, child_changed)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGCHLD})
    # Only the keeper inherits Factory's locks. Go and its tools cannot pin them.
    child = subprocess.Popen(command, cwd=package, stdin=subprocess.DEVNULL, process_group=0)
    if sys.platform == "linux":
        child_changed(signal.SIGCHLD, None)  # Cover driver exit during Popen.
    expired = not select.select([sys.stdin, reader], [], [], seconds)[0]
    signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # No process remains to receive the signal.
    except PermissionError:
        if sys.platform != "darwin":
            raise
        # Darwin reports EPERM when only a zombie remains in our same-user group.
    code = child.wait()
    # A compiler can close all inherited FDs and outlive its driver. Probe until
    # the group has drained before releasing the locks. Never signal a reaped PID.
    while True:
        if sys.platform == "linux":
            try:
                while os.waitpid(-child.pid, os.WNOHANG)[0]:
                    pass
            except ChildProcessError:
                pass  # No adopted child remains in this group.
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            break
        except PermissionError:
            if sys.platform != "darwin":
                raise
            break
        time.sleep(0.01)
    if expired:
        print(f"Go {command[1]} exceeded its {seconds}-second deadline", file=sys.stderr)
        return 124
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main())
