"""Run one trusted Go command under the inherited cache admission locks."""
import os
import select
import signal
import subprocess
import sys
import time


def main():
    seconds = int(sys.argv[1])
    command = sys.argv[2:]
    # Wake on driver status change without reaping it: its PID pins our signal target.
    reader, writer = os.pipe()

    def child_changed(signum, _frame):
        signal.signal(signum, signal.SIG_DFL)
        os.close(writer)

    signal.signal(signal.SIGCHLD, child_changed)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGCHLD})
    # Only the keeper inherits Factory's locks. Go and its tools cannot pin them.
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, process_group=0)
    expired = not select.select([sys.stdin, reader], [], [], seconds)[0]
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
