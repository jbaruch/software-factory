"""Run one trusted Go command under the inherited cache admission locks."""
import os
import select
import signal
import subprocess
import sys
import time


def main():
    caller, seconds = map(int, sys.argv[1:3])
    command = sys.argv[3:]
    # Mini Watcher creates a session headed by Factory and tears down that
    # exact group. Direct commands have a private group targeted by the keeper.
    hosted_group = os.getpgrp() == caller
    group = caller if hosted_group else 0
    # Keep the leader waitable through the final signal, which pins its group
    # ID. EOF also covers descendants that inherit the pipe from Go.
    reader, writer = os.pipe()

    def child_changed(signum, _frame):
        signal.signal(signum, signal.SIG_DFL)
        os.close(writer)

    signal.signal(signal.SIGCHLD, child_changed)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGCHLD})
    # Only the keeper inherits Factory's locks. Go and its tools cannot pin them.
    child = subprocess.Popen(command, pass_fds=(writer,), process_group=group)
    deadline = time.monotonic() + seconds
    expired = not select.select([reader], [], [], seconds)[0]
    if hosted_group and not expired:
        # A stop also delivers SIGCHLD. Verify the driver exit under the same
        # deadline before returning success in the Watcher's group.
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            expired = True
    if not hosted_group or expired or os.getppid() != caller:
        try:
            os.killpg(group or child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # No process remains to receive the signal.
        except PermissionError:
            # Darwin reports EPERM for an unreaped zombie with no live
            # signal recipients in this same-user private Go group.
            if sys.platform != "darwin" or hosted_group or not select.select([reader], [], [], 0)[0]:
                raise
    code = child.wait(timeout=seconds)
    if not select.select([reader], [], [], seconds)[0]:
        raise TimeoutError("Could not confirm Go writer exit after teardown; inspect the cache before retrying")
    if not hosted_group:
        # The driver can exit before a compiler that closed all inherited FDs.
        # After reaping the driver, only probe the group, never signal a reused ID.
        stop_by = time.monotonic() + seconds
        while True:
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                break
            except PermissionError:
                if sys.platform != "darwin":
                    raise
                break  # No signalable member of the same-user group on Darwin.
            if time.monotonic() >= stop_by:
                raise TimeoutError("Go group exit unconfirmed; inspect the cache before retrying")
            time.sleep(0.01)
    if expired:
        print(f"Go {command[1]} exceeded its {seconds}-second deadline", file=sys.stderr)
        return 124
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    sys.exit(main())
