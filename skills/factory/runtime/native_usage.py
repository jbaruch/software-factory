"""Read a native usage panel without submitting an unrecognized slash command."""
from collections.abc import Callable
import errno
import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from unicodedata import category, east_asian_width

CONTROL = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()][A-Z0-9]|[=>])")
INPUT_CONTROL = re.compile(
    rb"\x1b7\x1b\[r\x1b8|\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|"
    rb"[PX^_][^\x1b]*\x1b\\|(?![\[\]PX^_])[ -/]*[0-~])|\x1b|\r\n|[\x00-\x08\x0b-\x1f\x7f]|\Z"
)
PRESENTATION = re.compile(
    rb"\x1b7\x1b\[r\x1b8|\x1b\[(?:[0-9;:]*m|\?(?:25|100[02346]|1015|2004|2031)[hl]|c|>0q|\?u)"
    rb"|\x1b\](?:0|11|12|7501);[^\x07\x1b]*(?:\x07|\x1b\\)|\x07"
)
POSITION = re.compile(rb"\x1b\[([1-9][0-9]?);([1-9][0-9]{0,2})H")
ROWS, COLUMNS = 40, 120
OUTPUT_LIMIT = 256 * 1024


class PanelError(Exception):
    pass


def read_panel(command, cwd, env, *, request, prompt, menu, complete, blocked, raw_blocked=b"", timeout: float = 15):
    """Own one bounded PTY session, and keep its output private to the parser."""
    try:
        master, slave = pty.openpty()
        try:
            child = None
            cancelled = None
            handlers = {}
            interrupted = False
            cleanup_complete = False

            def defer(signum, _frame):
                nonlocal cancelled
                if cancelled is None:
                    cancelled = signum

            def arm_interrupt_deferral():
                if threading.current_thread() is threading.main_thread():
                    for sig in (signal.SIGTERM, signal.SIGINT):
                        if signal.getsignal(sig) != signal.SIG_IGN:
                            handlers[sig] = signal.signal(sig, defer)

            def restore_interrupt_handlers():
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
                handlers.clear()

            def deliver_interrupt():
                nonlocal cancelled
                signum, cancelled = cancelled, None
                if signum is not None:
                    if signal.getsignal(signum) == signal.SIG_DFL:
                        raise SystemExit(128 + signum)  # Exit with the default signal status after cleanup.
                    signal.raise_signal(signum)

            try:
                try:
                    arm_interrupt_deferral()
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLUMNS, 0, 0))
                    child = subprocess.Popen(command, cwd=cwd, env={**env, "TERM": "xterm-256color"},
                                             stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
                finally:
                    try:
                        os.close(slave)
                    finally:
                        restore_interrupt_handlers()
                        deliver_interrupt()
                return exchange(master, request, prompt, menu, complete, blocked, timeout, raw_blocked=raw_blocked)
            except (KeyboardInterrupt, SystemExit):
                interrupted = True
                raise
            finally:
                if child is not None:
                    arm_interrupt_deferral()
                    # The child starts as group leader. Keep it unreaped until the
                    # last signal so its PID cannot be reused as another group's ID.
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                        try:
                            time.sleep(1)
                        finally:
                            os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass  # The process group has already exited.
                    except PermissionError as exc:
                        if sys.platform != "darwin" or exc.errno != errno.EPERM:
                            raise
                        # XNU excludes zombies from group signal delivery and can
                        # return EPERM even when no live group member remains.
                        denied = PanelError("Native terminal group could not be confirmed stopped after signal denial; "
                                            "inspect the native CLI processes before refreshing")
                        try:
                            inventory = subprocess.run(
                                ["/bin/ps", "-ax", "-o", "pid=,pgid=,state="],
                                env={"LC_ALL": "C"}, stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=1, check=True,
                            )
                        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                            raise denied from None
                        # The unreaped leader must appear in a usable inventory.
                        group = str(child.pid).encode("ascii")
                        leader_seen = False
                        for line in inventory.stdout.splitlines():
                            identity = re.match(rb"[ \t]*(\d+)[ \t]+(\d+)(?=[ \t]|$)", line)
                            if identity is None:
                                raise denied from None
                            pid, pgid = identity.groups()
                            if pgid != group:
                                continue
                            if re.fullmatch(rb"[ \t]+Z[+<>AELNSsVWX]*[ \t]*", line[identity.end():]) is None:
                                raise denied from None
                            leader_seen = leader_seen or pid == group
                        if inventory.stderr or not leader_seen:
                            raise denied from None
                    finally:
                        try:
                            child.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            raise PanelError("Native terminal did not exit during cleanup; inspect the native CLI process before refreshing") from None
                cleanup_complete = True
        finally:
            try:
                os.close(master)
            finally:
                restore_interrupt_handlers()
            # Preserve an interrupt already unwinding, and let cleanup failures surface.
            if cleanup_complete and not interrupted:
                deliver_interrupt()
    except OSError:
        raise PanelError("Terminal startup or I/O failed; inspect the native CLI in a trusted interactive directory") from None


def screen_evidence(rows, position, *, positioned_menu):
    trim = " " if positioned_menu else None
    lines = ["".join(rows.get(row, ())).rstrip(trim) for row in range(1, ROWS + 1)]
    text = "\n".join(lines).rstrip("\n" if positioned_menu else None)
    if position is None:
        return text, None
    caret_row, caret_col = position
    caret_line = sum(len(line) + 1 for line in lines[:caret_row - 1])
    return text, (caret_line, caret_col, rows.get(caret_row, []))


def input_evidence(pending, *, positioned_menu):
    """For Enter, compose the viewport and also track the newest update's writes."""
    _, visible, fresh = next(render_evidence(pending, positioned_menu=positioned_menu),
                             (0, ("", None), ("", None)))
    return visible, fresh


def render_evidence(pending, *, positioned_menu, frame_ends: set[int] | None = None):
    """Render once and yield evidence at selected closes, or at the end for input gating."""
    visible = ""
    rows = {}
    fresh_rows = None
    synchronized = False
    certain = True
    position = None
    start = 0
    for control in INPUT_CONTROL.finditer(pending):
        fragment = pending[start:control.start()].decode("utf-8", errors="replace")
        if positioned_menu and fragment and (not synchronized or position is None):
            certain = False
        elif position is None:
            visible += fragment
        elif fragment and certain:
            row, col = position
            line = rows.setdefault(row, [" "] * COLUMNS)
            fresh_line = fresh_rows.setdefault(row, [" "] * COLUMNS) if fresh_rows is not None else None
            for char in fragment:
                if col >= COLUMNS or char == "\n":
                    if not positioned_menu:
                        return
                    certain = False
                    break
                wide = east_asian_width(char) in ("W", "F")
                if positioned_menu:
                    end = col + (2 if wide else 1)
                    # Unmodelled width, wrapping and partial overwrites of wide cells
                    # can affect rows beyond the characters in this fragment.
                    if (char == "\ufffd" or category(char)[0] in "CM" or end >= COLUMNS
                            or line[col] == "" or line[end] == ""):
                        certain = False
                        break
                    if fresh_line is not None:
                        fresh_line[col] = char
                        if wide:
                            fresh_line[col + 1] = ""
                line[col] = char
                col += 1
                if wide and col < COLUMNS:
                    line[col] = ""
                    col += 1
            position = row, col
        code = control.group()
        start = control.end()
        if not code:
            break
        if code == b"\x1b" or (code == b"\r" and control.end() == len(pending)):
            return
        if PRESENTATION.fullmatch(code):
            continue
        if code == b"\x1b[?2026h":
            if positioned_menu:
                certain = certain and not synchronized
                # A synchronized update repaints selected cells of the same viewport.
                fresh_rows = {}
                position = None
            synchronized = True
        elif code == b"\x1b[?2026l" and synchronized:
            synchronized = False
            if (frame_ends is not None and start in frame_ends and certain
                    and fresh_rows is not None and position is not None):
                yield start, screen_evidence(rows, position, positioned_menu=True), screen_evidence(
                    fresh_rows, position, positioned_menu=True)
        elif synchronized and (move := POSITION.fullmatch(code)):
            # Grok emits positioned changes across synchronized updates.
            row, col = map(int, move.groups())
            if not (row <= ROWS and col <= COLUMNS):
                if not positioned_menu:
                    return
                certain = False
            else:
                position = row, col - 1
        elif positioned_menu:
            if code == b"\x1b[2J":
                rows.clear()
                # Quota can use writes after the clear. Enter requires a new complete update.
                fresh_rows = {} if frame_ends is not None and synchronized else None
                position = None  # Require a new explicit cursor position after erasing.
                certain = True
            elif code == b"\x1b[H":
                position = (1, 0) if synchronized else None
            else:
                # An unknown effect can leave cells behind. A redraw alone cannot
                # restore confidence in the viewport.
                certain = False
        elif code == b"\r\n" and position is None:
            visible += "\r\n"
        elif position is None and (column := re.fullmatch(rb"\x1b\[(\d{1,3})G", code)) and (
            int(column[1]) == len(visible.rsplit("\n", 1)[-1]) + 1
        ):
            continue  # Line-mode prompt's cursor is already at that column.
        else:
            visible = ""
            rows.clear()
            position = None
    fresh = ("", None)
    if frame_ends is not None or synchronized or not certain:
        return
    if fresh_rows is not None and position is not None:
        fresh = screen_evidence(fresh_rows, position, positioned_menu=True)
    if positioned_menu or position is not None:
        yield start, screen_evidence(rows, position, positioned_menu=positioned_menu), fresh
    else:
        yield start, (visible, None), fresh


def positioned_match(pattern, request, text, caret):
    match = pattern.search(text)
    if not match or caret is None or not re.fullmatch(r"[ \t\r\n│]*", text[match.end():]):
        return None
    caret_line, caret_col, cells = caret
    input_line = match["input_line"]
    command = input_line.lstrip(" ")
    if (match.start("input_line") != caret_line or command.encode("utf-8") != request
            or cells != list(input_line.ljust(COLUMNS))
            or caret_col not in (len(input_line) - len(command), len(input_line))):
        return None
    # Text offsets can differ when other rows contain retained cells.
    return tuple((text.count("\n", 0, match.start(group)), match[group])
                 for group in ("selection_line", "input_line"))


def exchange(master, request, prompt: str, menu: str, complete: str | Callable[[bytearray], str | None],
             blocked, timeout, *, raw_blocked=b""):
    deadline = time.monotonic() + timeout
    pending = bytearray()
    for label, marker, key in (("input prompt", prompt, request),
                               ("built-in /usage menu entry", menu, b"\r"),
                               ("complete /usage panel", complete, None)):
        matcher = marker if callable(marker) else re.compile(marker)
        positioned_menu = key == b"\r" and not callable(matcher) and "input_line" in matcher.groupindex
        while (left := deadline - time.monotonic()) > 0:
            panel = None
            raw_modal = key is not None and bool(raw_blocked) and raw_blocked in CONTROL.sub(b"", pending)
            if raw_modal:
                ready, modal = False, True
            elif callable(matcher):
                panel = matcher(pending)
                left = deadline - time.monotonic()
                if left <= 0:
                    continue
                ready, modal = panel is not None, False
            elif key is None:
                text = CONTROL.sub(b"", pending).decode("utf-8", errors="replace")
                if matcher.search(text):
                    return text
                ready = modal = False
            else:
                (text, caret), fresh = input_evidence(pending, positioned_menu=positioned_menu)
                modal = re.search(blocked, text)
                if positioned_menu:
                    composed = positioned_match(matcher, request, text, caret)
                    ready = composed is not None and composed == positioned_match(matcher, request, *fresh)
                else:
                    match = matcher.search(text)
                    if match and caret is not None:
                        if not match.groupdict().get("input_line") or match.start("input_line") != caret[0]:
                            match = None
                    ready = match and re.fullmatch(r"[ \t\r\n│]*", text[match.end():])
            if ready or modal:
                # Finish reading queued redraws before accepting evidence.
                readable = []
                if not raw_modal and (positioned_menu or not modal):
                    readable, _, _ = select.select([master], [], [], 0)
                if not readable and modal:
                    raise PanelError("Native terminal showed a modal or stale usage output; inspect the native CLI before refreshing")
                if not readable and time.monotonic() < deadline:
                    if panel is not None:
                        return panel
                    if key is not None and os.write(master, key) != len(key):
                        raise PanelError("Terminal accepted incomplete input; inspect the native CLI before refreshing")
                    pending.clear()
                    break
            else:
                readable, _, _ = select.select([master], [], [], left)
            if not readable:
                continue
            try:
                chunk = os.read(master, 8192)
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                chunk = b""  # A closed PTY may signal EOF with EIO.
            if not chunk:
                raise PanelError(f"Native terminal closed before the {label}; inspect native sign-in and /usage")
            pending.extend(chunk)
            if len(pending) > OUTPUT_LIMIT:
                raise PanelError(f"Native terminal exceeded its output bound before the {label}; inspect /usage")
        else:
            raise PanelError(f"Timed out waiting for the {label}; inspect native sign-in, directory trust and /usage")
    raise PanelError("Native /usage panel was incomplete; inspect the CLI before refreshing")
