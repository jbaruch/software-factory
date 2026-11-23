"""PTY state machine fixtures use no provider processes."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import errno
import fcntl
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, call, patch

import native_claude
import native_grok
import native_usage
import ready_runner
from test_native_grok import GROK_USAGE, GROK_USAGE_NO_CREDITS


def staged_output(fds, _write, _error, timeout):
    return (fds if timeout > 0 else [], [], [])


class NativeUsageTransport(unittest.TestCase):
    def test_grok_weekly_panel_survives_only_complete_presentation_tails(self):
        begin, end = b"\x1b[?2026h", b"\x1b[?2026l"
        prompt = begin + (
            "\x1b[2;3H+ New Agent\x1b[36;3H│ ❯   │"
            "\x1b[39;3HEnter:create  │  Tab:list\x1b[36;7H"
        ).encode() + end
        menu = begin + (
            "\x1b[33;3H❯ show View usage\x1b[34;3H──────────\x1b[36;7H/usage show"
            "\x1b[39;9Hsend  │  Ctrl+s:send+open  │  Shift+Tab:mode  │  ?:shortcuts\x1b[36;18H"
        ).encode() + end
        # Mini 1.0.92 showed a setup frame, then added quota fields on rows 9, 11 and 12.
        setup = begin + b"\x1b[9;3HUsage\x1b[39;3HPress Esc to close" + end
        heading, bar, reset = GROK_USAGE_NO_CREDITS.split("││")
        first = begin + b"\x1b[9;3H" + heading.encode() + end
        bar_row = b"\x1b[11;3H" + ("│" + bar).encode()
        second = begin + bar_row + end
        last = begin + b"\x1b[12;3H" + ("│" + reset).encode()
        close = b"\x1b[0m\x1b[39;120H" + end
        panel = first + second + last + close
        quota_frame = first[:-len(end)] + bar_row + last[len(begin):] + close
        cursor_show = b"\x1b[?25h"
        control_only = begin + cursor_show + end  # Exact control-only frame in the SRE replay.
        unpositioned = begin + "Weekly limit (SuperGrok)│████│100%│Resets: November 15, 10:40".encode() + end
        clear = begin + b"\x1b[2J\x1b[22;3H" + "│Resets: November 15, 10:40".encode() + end
        erased = (begin + b"\x1b[20;3HWeekly limit (SuperGrok)" + end
                  + begin + b"\x1b[21;3H" + "│░ 0%".encode() + end + clear)
        redraw = begin + b"\x1b[12;3HLoading" + end
        other_write = begin + b"\x1b[17;3HLater text" + end
        reset_row = "\x1b[12;3H│Resets: November 15, 10:40".encode()
        heading_row = first[len(begin):-len(end)]
        unused_bar = "\x1b[11;3H│░ 0%".encode()
        unused_quota = begin + heading_row + unused_bar + reset_row + close
        clear_screen = b"\x1b[2J"
        rebuilt = begin + clear_screen + heading_row + unused_bar + reset_row + close
        reset_repaint = begin + reset_row + end
        reset_and_loading = begin + reset_row + b"\x1b[17;3HLoading" + end
        rebuilt_cases = (
            ("rebuilt panel", rebuilt, control_only,
             dict(pool="SuperGrok", remaining_percent=100, resets_at="November 15, 10:40")),
            ("rebuilt panel then reset repaint", rebuilt, reset_repaint, None),
            ("rebuilt panel then reset repaint and Loading", rebuilt, reset_and_loading, None),
            ("erased heading", begin + heading_row + clear_screen + unused_bar + reset_row + close, control_only, None),
            ("erased bar", begin + unused_bar + clear_screen + heading_row + reset_row + close, control_only, None),
            ("reset-only rebuild", begin + clear_screen + reset_row + close, control_only, None),
            ("partial reset after clear", rebuilt.replace(b"10:40", b"10:4"), control_only, None),
        )
        legacy = GROK_USAGE.replace("││ Credits:", "│\r\n│ Credits:").encode() + b"\x1b[0m"
        no_capacity = dict(pool="SuperGrok", remaining_percent=0, resets_at="November 15, 10:40")
        cases = (
            ("split setup and quota", [setup, quota_frame], no_capacity),
            ("coalesced setup and quota", [setup + quota_frame], no_capacity),
            ("two updates", [first, begin + bar_row + last[len(begin):] + close], no_capacity),
            ("split close and cursor-show", [panel[:-1], panel[-1:], cursor_show], no_capacity),
            ("coalesced close and cursor-show", [panel + cursor_show], no_capacity),
            ("split control-only frame", [panel, control_only], no_capacity),
            ("coalesced control-only frame", [panel + control_only], no_capacity),
            ("bounded cursor-only frame", [panel, begin + b"\x1b[40;120H\x1b[H" + end], no_capacity),
            ("out-of-bounds cursor-only frame", [panel, begin + b"\x1b[41;1H" + end], None),
            ("unknown control", [panel + begin + b"\x1b[1A" + end], None),
            ("split unpositioned SRE panel", [unpositioned, control_only], None),
            ("coalesced unpositioned SRE panel", [unpositioned + control_only], None),
            ("repainted heading", [first + first + second + last + close], no_capacity),
            ("legacy", [legacy], dict(pool="SuperGrok", remaining_percent=3, resets_at="November 15, 08:40")),
            ("legacy cursor move", [legacy + b"\x1b[2;3H"], None),
            ("legacy incomplete credits", [legacy.replace(b"22.63", b"22.6")], None),
            ("erased zero-percent bar", [erased], None),
            ("stale reset", [last + close + first + second], None),
            ("reset split across writes", [panel.replace(b"10:40", b"10:\x1b[0m40")], no_capacity),
            ("split clear after panel", [panel, clear], None),
            ("coalesced clear after panel", [panel + clear], None),
            ("split redraw", [panel, redraw], None),
            ("coalesced redraw", [panel + redraw], None),
            ("split reset repaint", [unused_quota, reset_repaint], None),
            ("coalesced reset repaint", [unused_quota + reset_repaint], None),
            ("split reset repaint and Loading", [unused_quota, reset_and_loading], None),
            ("coalesced reset repaint and Loading", [unused_quota + reset_and_loading], None),
            ("later visible text", [panel, other_write], None),
            ("unframed visible text", [panel, b"extra"], None),
            ("split partial control", [panel, b"\x1b[?"], None),
            ("coalesced partial control", [panel + b"\x1b[?"], None),
            ("unpaired open", [panel, begin + cursor_show], None),
            ("unpaired close", [panel, end], None),
            ("partial reset", [panel.replace(b"10:40", b"10:4")], None),
            ("trailing text on reset row", [panel.replace(b"10:40", b"10:40 extra")], None),
            ("duplicate visible heading", [first + first.replace(b"[9;", b"[10;") + second + last + close], None),
            ("impossible percentage", [panel.replace(b"100%", b"101%")], None),
            *((f"{layout} {label}", chunks, expected) for label, frame, tail, expected in rebuilt_cases
              for layout, chunks in (("split", [frame, tail]), ("coalesced", [frame + tail]))),
        )
        plan = dict(schema_version=1, limits=dict(workers=1, tasks=1, reserve_workers=0, minimum_remaining_percent=10),
                    jobs=[])
        runner = ready_runner.Runner(Path.cwd(), plan, codex=sys.executable, rex=sys.executable,
                                     server="unix:///test.sock", grok=sys.executable)
        probe_usage = native_grok.probe_usage
        for label, chunks, expected in cases:
            with self.subTest(label=label):
                events = Mock()
                events.read.side_effect = [prompt, menu, *chunks, b""]
                events.write.side_effect = lambda fd, data: len(data)

                def interactive(_command, _cwd, _env, **spec):
                    return native_usage.exchange(17, timeout=1, **spec)

                def ready(fds, _write, _error, timeout):
                    panel_reads = events.read.call_count - 2
                    queued = events.write.call_count == 2 and panel_reads < len(chunks)
                    return (fds if timeout > 0 or queued else [], [], [])

                with (patch.dict(os.environ, {}, clear=True),
                      patch.object(native_usage.time, "monotonic", return_value=0),
                      patch.object(native_usage.select, "select", side_effect=ready),
                      patch.object(native_usage.os, "read", events.read),
                      patch.object(native_usage.os, "write", events.write),
                      patch.object(runner, "refresh_capacity"),
                      patch.object(native_grok, "probe_usage",
                                   side_effect=lambda binary, cwd: probe_usage(binary, cwd, interactive=interactive))):
                    report = runner.harnesses()["grok"]
                self.assertEqual(report["windows"], [expected] if expected is not None else [])
                if expected is None:
                    self.assertIs(report["available"], False)
                    self.assertIn("closed before the complete /usage panel", report["reason"])
                read = call.read(17, 8192)
                self.assertEqual(events.mock_calls[:4], [read, call.write(17, b"/usage show"), read, call.write(17, b"\r")])
                self.assertEqual(events.mock_calls[4:], [read] * (len(chunks) + (expected is None)))

    def test_grok_checks_dense_frame_history_within_the_output_bound(self):
        begin, end = b"\x1b[?2026h", b"\x1b[?2026l"
        incomplete = begin + b"\x1b[12;3HResets: November 15, 10:40" + end
        quota = begin + ("\x1b[9;3HWeekly limit (SuperGrok)\x1b[11;3H░ 0%"
                         "\x1b[12;3HResets: November 15, 10:40").encode() + end
        history = incomplete * ((native_usage.OUTPUT_LIMIT - len(quota)) // len(incomplete))
        panel = native_grok.usage_complete(bytearray(history + quota))
        self.assertIsNotNone(panel)
        self.assertEqual(native_grok.parse_usage(panel), [
            dict(pool="SuperGrok", remaining_percent=100, resets_at="November 15, 10:40"),
        ])

    def test_grok_callable_completion_keeps_the_exchange_deadline_and_byte_bound(self):
        for label in ("deadline", "byte bound"):
            with self.subTest(label=label):
                now = 0
                pending = b"\x1b[?2026h" + b"x" * (native_usage.OUTPUT_LIMIT - len(b"\x1b[?2026h"))
                chunks = (pending[index:index + 8192] for index in range(0, len(pending), 8192))
                reads = Mock(side_effect=[b"prompt", b"menu", *chunks, b"x"])

                def complete(raw):
                    nonlocal now
                    result = native_grok.usage_complete(raw)
                    if label == "deadline" and len(raw) == native_usage.OUTPUT_LIMIT:
                        now = 1
                    return result

                with (patch.object(native_usage.time, "monotonic", side_effect=lambda: now),
                      patch.object(native_usage.select, "select", side_effect=staged_output),
                      patch.object(native_usage.os, "read", reads),
                      patch.object(native_usage.os, "write", side_effect=lambda fd, data: len(data))):
                    error = "Timed out" if label == "deadline" else "exceeded its output bound"
                    with self.assertRaisesRegex(native_usage.PanelError, error):
                        native_usage.exchange(17, b"/usage show", "prompt", "menu", complete, "blocked", 1)
                self.assertEqual(reads.call_count, 2 + native_usage.OUTPUT_LIMIT // 8192 + (label == "byte bound"))

    def test_waits_for_complete_prompt_builtin_menu_and_panel(self):
        events = Mock()
        events.read.side_effect = [
            b"startup banner",
            b"\x1b[38;5;9mready pro",
            b"mpt\x1b[0m",
            b"slash command /usage",
            b"\x1b[38;5;9m /usage built-in\x1b[0m",
            b"\x1b[4mWeekly limit 94%",
            b" Resets Nov",
            b" 15, 08:40 Panel finished\x1b[0m",
        ]
        events.write.side_effect = lambda fd, data: len(data)
        with (patch.object(native_usage.select, "select", side_effect=staged_output),
              patch.object(native_usage.os, "read", events.read),
              patch.object(native_usage.os, "write", events.write)):
            result = native_usage.exchange(17, b"/usage", "ready prompt", "/usage built-in", "Panel finished", "trust", 1)
        self.assertEqual(result, "Weekly limit 94% Resets Nov 15, 08:40 Panel finished")
        read = call.read(17, 8192)
        self.assertEqual(events.mock_calls, [read, read, read, call.write(17, b"/usage"), read, read,
                                             call.write(17, b"\r"), read, read, read])

    def test_never_sends_slash_on_trust_prompt_or_enter_without_builtin_command(self):
        for chunks, expected in (([b"trust this directory?", b""], []),
                                 ([b"ready prompt", b"unknown command", b""], [call(17, b"/usage")])):
            with (self.subTest(chunks=chunks),
                  patch.object(native_usage.select, "select", side_effect=staged_output),
                  patch.object(native_usage.os, "read", side_effect=chunks),
                  patch.object(native_usage.os, "write", return_value=6) as write):
                with self.assertRaisesRegex(native_usage.PanelError, "closed before|showed a modal"):
                    native_usage.exchange(17, b"/usage", "ready prompt", "/usage built-in", "Panel finished", "trust", 1)
                self.assertEqual(write.call_args_list, expected)

    def test_native_commands_require_the_idle_screen_and_exact_menu(self):
        claude = b"[Screen Reader Mode: on via flag]\r\nClaude Code v2.1.329\r\n$\r\n"
        claude_menu = "/usage    Show session cost, plan usage, and activity\r\n$\xa0 /usage\r\n".encode()
        claude_import = b"Allow external CLAUDE.md file imports?\r\nExternal imports:\r\nEnter to confirm\r\n"
        grok = "│ ❯   │\r\n+ New Agent\r\nEnter:create  │  Tab:list".encode()
        grok_menu = "❯ show View usage\r\n│ ❯ /usage show   │".encode()
        cases = (
            (native_claude, b"/usage", [claude, claude_menu], [b"/usage", b"\r"]),
            (native_claude, b"/usage", [claude.replace(b"$", claude_import + b"$"), b""], []),
            (native_claude, b"/usage", [b"Current week (all models)\r\n" + claude, b""], []),
            (native_claude, b"/usage", [claude.replace(b"$", b"$ /other"), b""], []),
            (native_claude, b"/usage", [claude.replace(b"$", claude_menu + b"$"), b""], []),
            (native_claude, b"/usage", [claude.replace(b"$", b"$ /other\r\n$"), b""], []),
            (native_claude, b"/usage", [claude, b"External imports:\r\n" + claude_menu, b""], [b"/usage"]),
            (native_claude, b"/usage", [claude, claude_menu.replace(b" /usage\r", b" /other\r"), b""], [b"/usage"]),
            (native_grok, b"/usage show", [grok, grok_menu, b""], [b"/usage show"]),
            (native_grok, b"/usage show", ["│ ❯   │".encode(), b""], []),
            (native_grok, b"/usage show", [grok, grok_menu + b"Trust this directory?", b""], [b"/usage show"]),
            (native_grok, b"/usage show", [grok, grok_menu.replace(b"/usage show", b"/usage"), b""], [b"/usage show"]),
        )
        for provider, request, chunks, keys in cases:
            with (self.subTest(provider=provider.__name__, chunks=chunks),
                  patch.object(native_usage.select, "select", side_effect=staged_output),
                  patch.object(native_usage.os, "read", side_effect=[*chunks, b"Panel finished"]),
                  patch.object(native_usage.os, "write", side_effect=lambda fd, data: len(data)) as write):
                args = (17, request, provider.USAGE_PROMPT, provider.USAGE_MENU, "Panel finished", provider.USAGE_BLOCKED, 1)
                if keys[-1:] == [b"\r"]:
                    self.assertEqual(native_usage.exchange(*args), "Panel finished")
                else:
                    with self.assertRaises(native_usage.PanelError):
                        native_usage.exchange(*args)
                self.assertEqual(write.call_args_list, [call(17, key) for key in keys])

    def test_redraw_invalidates_input_until_fresh_native_evidence(self):
        claude = b"[Screen Reader Mode: on via flag]\r\nClaude Code v2.1.327\r\n$\r\n"
        claude_menu = "/usage    Show session cost, plan usage, and activity\r\n$\xa0 /usage\r\n".encode()
        grok = "│ ❯   │\r\n+ New Agent\r\nEnter:create  │  Tab:list".encode()
        clear = b"\x1b[2J\x1b[H"
        modal = b"Select a workspace"
        begin, end = b"\x1b[?2026h", b"\x1b[?2026l"
        caret = b"\x1b[36;7H"
        grok_screen = begin + (
            "\x1b[2;3H+ New Agent\x1b[36;3H│ ❯   │"
        ).encode() + caret + end + begin + (
            "\x1b[39;3HEnter:create  │  Tab:list  │  Shift+Tab:mode  │  ?:shortcuts"
        ).encode() + caret + end
        # Use the cursor positions observed in Grok 1.0.77's post-command update.
        grok_menu_redraw = begin + (
            "\x1b[32;3H          \x1b[33;3H❯ show View usage\x1b[34;3H──────────\x1b[36;7H/usage show"
            "\x1b[39;9Hsend  │ \x1b[39;17H Ctrl+s:send+open  │  Shift+Tab:mode  │  ?:shortcuts"
        ).encode()
        grok_screen_menu = grok_menu_redraw + caret + end
        # The c681767 live probe ended at zero-based cell 17, just after /usage show.
        grok_end_cell_menu = grok_menu_redraw + b"\x1b[36;18H" + end
        moved_focus = begin + b"\x1b[12;1HChoose an account\x1b[12;2H" + end
        moved_column = begin + b"\x1b[12;1HChoose an account\x1b[36;1H" + end
        account_redraw = begin + b"\x1b[12;1HChoose an account\x1b[36;18H" + end
        workspace_redraw = begin + b"\x1b[12;1HSelect a workspace" + end
        unrelated_redraw = begin + b"\x1b[12;1HDashboard refreshed\x1b[36;18H" + end
        incomplete_command = begin + b"\x1b[36;17H " + caret + end
        menu_suffix = begin + b"\x1b[33;20H!" + end
        command_suffix = begin + b"\x1b[36;18H EXTRA" + end
        menu_without_selection = grok_end_cell_menu.replace("\x1b[33;3H❯ show View usage".encode(), b"")
        menu_without_command = grok_end_cell_menu.replace(caret + b"/usage show", b"")
        partial_erases = [begin + b"\x1b[32;1H" + prompt + b"\x1b[32;40H\x1b[K" for prompt in
                          (b"Select a workspace", b"Choose an account")]
        styled_menu = grok_end_cell_menu.replace(b"/usage show", b"\x1b[1m/usage show\x1b[0m")
        cleared_update = begin + b"\x1b[2J"
        menu_writes = grok_end_cell_menu[len(begin):]
        positioned_rejections = [
            (cleared_update, menu_writes),
            (menu_suffix, grok_end_cell_menu), (command_suffix, grok_end_cell_menu),
            (workspace_redraw, grok_end_cell_menu), (account_redraw, grok_end_cell_menu),
            (grok_end_cell_menu, unrelated_redraw), (grok_end_cell_menu, menu_without_selection),
            (grok_end_cell_menu, menu_without_command),
            (grok_end_cell_menu, b"\x1b["),
            (partial_erases[0] + end + b"\x1b[H", grok_end_cell_menu),
        ]
        positioned_rejections.extend((erase, grok_end_cell_menu[len(begin):]) for erase in partial_erases)
        cases = (
            (native_claude, b"/usage", [[claude + clear]], []),
            (native_claude, b"/usage", [[claude], [claude_menu + clear]], [b"/usage"]),
            (native_claude, b"/usage", [[claude], [claude_menu + b"\x1b[?1049h"]], [b"/usage"]),
            (native_claude, b"/usage", [[claude], [begin + claude_menu.replace(b"$", end + begin + b"$") + end]],
             [b"/usage", b"\r"]),
            (native_grok, b"/usage show", [[grok], [grok_screen_menu + clear + modal]], [b"/usage show"]),
            (native_grok, b"/usage show", [[grok], [grok_screen_menu + b"\r" + modal]], [b"/usage show"]),
            (native_grok, b"/usage show", [[grok], [grok_screen_menu + modal]], [b"/usage show"]),
            (native_grok, b"/usage show", [[grok], [grok_screen_menu, b"\x1b[", b"2J\x1b[", b"H" + modal]],
             [b"/usage show"]),
            (native_claude, b"/usage", [[clear + claude], [claude_menu, clear + claude_menu]], [b"/usage", b"\r"]),
            (native_grok, b"/usage show", [[clear + grok], [grok_screen_menu, b"\x1b[2",
                                                          b"J\x1b[H" + grok_screen_menu[:1], grok_screen_menu[1:]]],
             [b"/usage show", b"\r"]),
            (native_grok, b"/usage show", [[grok_screen], [grok_screen_menu, moved_focus]], [b"/usage show"]),
            (native_grok, b"/usage show", [[grok_screen], [grok_screen_menu, moved_column]], [b"/usage show"]),
            (native_grok, b"/usage show", [[grok_screen], [grok_screen_menu, account_redraw]], [b"/usage show"]),
            (native_grok, b"/usage show", [[grok_screen], [grok_screen_menu, unrelated_redraw]], [b"/usage show"]),
            (native_grok, b"/usage show", [[grok_screen], [grok_screen_menu, incomplete_command]], [b"/usage show"]),
            (native_grok, b"/usage show", [[grok_screen], [grok_screen_menu]], [b"/usage show", b"\r"]),
            (native_grok, b"/usage show", [[grok_screen], [grok_end_cell_menu[:-len(end)]]], [b"/usage show"]),
            (native_grok, b"/usage show", [[grok_screen], [grok_end_cell_menu[:-1], grok_end_cell_menu[-1:]]],
             [b"/usage show", b"\r"]),
            (native_grok, b"/usage show", [[grok_screen], [grok_screen_menu, unrelated_redraw, grok_end_cell_menu]],
             [b"/usage show", b"\r"]),
            (native_grok, b"/usage show", [[grok_screen], [grok_screen_menu, clear, grok_screen_menu]],
             [b"/usage show", b"\r"]),
            (native_grok, b"/usage show", [[grok_screen], [workspace_redraw, clear, grok_end_cell_menu]],
             [b"/usage show", b"\r"]),
            (native_grok, b"/usage show", [[grok_screen], [workspace_redraw + clear + grok_end_cell_menu]],
             [b"/usage show", b"\r"]),
            (native_grok, b"/usage show", [[grok_screen], [partial_erases[0] + end + grok_end_cell_menu,
                                                          clear[:3], clear[3:] + grok_end_cell_menu]],
             [b"/usage show", b"\r"]),
            (native_grok, b"/usage show", [[grok_screen], [unrelated_redraw + styled_menu]],
             [b"/usage show", b"\r"]),
            (native_grok, b"/usage show", [[grok_screen], [cleared_update + menu_writes, grok_end_cell_menu]],
             [b"/usage show", b"\r"]),
            # The same bytes must produce the same writes across read boundaries.
            *((native_grok, b"/usage show", [[grok_screen], chunks], [b"/usage show"])
              for earlier, latest in positioned_rejections for chunks in ([earlier + latest], [earlier, latest])),
        )
        for provider, request, stages, keys in cases:
            with self.subTest(provider=provider.__name__, stages=stages):
                queue = iter(stages)
                pending = list(next(queue))

                def readable(fds, _write, _error, timeout):
                    return (fds if pending or timeout > 0 else [], [], [])

                def write_key(_fd, key):
                    nonlocal pending
                    pending = list(next(queue, [b"Panel finished"]))
                    return len(key)

                with (patch.object(native_usage.select, "select", side_effect=readable),
                      patch.object(native_usage.os, "read", side_effect=lambda *_: pending.pop(0) if pending else b""),
                      patch.object(native_usage.os, "write", side_effect=write_key) as write):
                    args = (17, request, provider.USAGE_PROMPT, provider.USAGE_MENU, "Panel finished", provider.USAGE_BLOCKED, 1)
                    if keys[-1:] == [b"\r"]:
                        self.assertEqual(native_usage.exchange(*args), "Panel finished")
                    else:
                        with self.assertRaisesRegex(native_usage.PanelError, "closed before|showed a modal"):
                            native_usage.exchange(*args)
                    self.assertEqual(write.call_args_list, [call(17, key) for key in keys])

    def test_spawn_and_cleanup_cancellation_close_pty_and_reap_child(self):
        popen = subprocess.Popen
        openpty = native_usage.pty.openpty
        killpg = os.killpg
        cases = (
            ("spawn", signal.SIGTERM, ready_runner.terminate, SystemExit),
            ("spawn", signal.SIGINT, signal.default_int_handler, KeyboardInterrupt),
            ("spawn", signal.SIGTERM, signal.SIG_DFL, SystemExit),
            ("cleanup", signal.SIGTERM, ready_runner.terminate, SystemExit),
            ("cleanup", signal.SIGINT, signal.default_int_handler, KeyboardInterrupt),
            ("exchange and cleanup", signal.SIGTERM, ready_runner.terminate, KeyboardInterrupt),
        )
        for phase, sig, handler, error in cases:
            with self.subTest(phase=phase, signal=sig):
                children = []
                descriptors = []
                previous = signal.signal(sig, handler)
                original_handlers = tuple(signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT))
                original_interrupt = KeyboardInterrupt("exchange interrupted")

                def open_terminal():
                    pair = openpty()
                    descriptors.extend(pair)
                    return pair

                def spawn(*args, **kwargs):
                    child = popen(*args, **kwargs)
                    if kwargs.get("start_new_session"):
                        children.append(child)
                        master = descriptors[0]
                        self.assertTrue(select.select([master], [], [], 5)[0])
                        self.assertIn(b"child ready", os.read(master, 4096))
                        if phase == "spawn":
                            signal.raise_signal(sig)
                    return child

                def group_signal(pgid, group_sig):
                    killpg(pgid, group_sig)
                    if phase != "spawn" and group_sig == signal.SIGTERM:
                        signal.raise_signal(sig)

                try:
                    with (patch.object(native_usage.subprocess, "Popen", side_effect=spawn),
                          patch.object(native_usage.pty, "openpty", side_effect=open_terminal),
                          patch.object(native_usage.os, "killpg", side_effect=group_signal) as group,
                          patch.object(native_usage.os, "write") as write,
                          patch.object(native_usage, "exchange", return_value="panel",
                                       side_effect=original_interrupt if phase == "exchange and cleanup" else None) as exchange,
                          self.assertRaises(error) as raised):
                        probe = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                                 "print('child ready', flush=True); time.sleep(10)")
                        native_usage.read_panel([sys.executable, "-c", probe], "/", {},
                                                request=b"/usage", prompt="ready", menu="menu", complete="panel", blocked="trust")
                    if isinstance(raised.exception, SystemExit):
                        self.assertEqual(raised.exception.code, 143)
                    if phase == "exchange and cleanup":
                        self.assertIs(raised.exception, original_interrupt)
                    self.assertEqual(tuple(signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)), original_handlers)
                    self.assertEqual(exchange.call_count, int(phase != "spawn"))
                    write.assert_not_called()
                    self.assertEqual(len(descriptors), 2)
                    for fd in descriptors:
                        with self.assertRaises(OSError) as closed:
                            os.fstat(fd)
                        self.assertEqual(closed.exception.errno, errno.EBADF)
                    self.assertEqual(len(children), 1)
                    self.assertEqual(group.call_args_list, [call(children[0].pid, signal.SIGTERM),
                                                            call(children[0].pid, signal.SIGKILL)])
                    self.assertEqual(children[0].returncode, -signal.SIGKILL)
                    with self.assertRaises(ChildProcessError):
                        os.waitpid(children[0].pid, os.WNOHANG)
                    with self.assertRaises(ProcessLookupError):
                        killpg(children[0].pid, 0)
                finally:
                    signal.signal(sig, previous)
                    for child in children:
                        if child.returncode is None:
                            child.kill()
                            child.wait(timeout=1)

    def test_failed_spawn_closes_each_descriptor_once(self):
        before = [signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)]
        with (patch.object(native_usage.pty, "openpty", return_value=(17, 18)),
              patch.object(native_usage.fcntl, "ioctl"),
              patch.object(native_usage.subprocess, "Popen", side_effect=OSError("fixture private details")),
              patch.object(native_usage.os, "close") as close):
            with self.assertRaisesRegex(native_usage.PanelError, "Terminal startup or I/O failed") as error:
                native_usage.read_panel(["fixture"], "/", {}, request=b"/usage", prompt="ready", menu="menu",
                                        complete="panel", blocked="trust")
        self.assertNotIn("fixture private details", str(error.exception))
        self.assertEqual(close.call_args_list, [call(18), call(17)])
        self.assertEqual([signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)], before)

    def test_signal_denial_requires_zombie_group_inventory_on_darwin(self):
        ps = ["/bin/ps", "-ax", "-o", "pid=,pgid=,state="]
        cases = (
            ("zombies only", "darwin", errno.EPERM, b"41 41 Z\n42 41 Z+\n43 43 S\n", b"", None),
            ("unrelated native state", "darwin", errno.EPERM, b"41 41 Z\n43 43 ?Es\n", b"", None),
            ("unreadable owned state", "darwin", errno.EPERM, b"41 41 Z\n43 41 ?Es\n", b"", "group could not be confirmed"),
            ("live member", "darwin", errno.EPERM, b"41 41 Z\n42 41 S\n", b"", "group could not be confirmed"),
            ("malformed row", "darwin", errno.EPERM, b"41 41 Z\n42 41\n", b"", "group could not be confirmed"),
            ("malformed PGID", "darwin", errno.EPERM, b"41 41 Z\n43 43?Es\n", b"", "group could not be confirmed"),
            ("missing leader", "darwin", errno.EPERM, b"43 43 S\n", b"", "group could not be confirmed"),
            ("partial listing", "darwin", errno.EPERM, b"41 41 Z\n", b"incomplete", "group could not be confirmed"),
            ("timeout", "darwin", errno.EPERM, None, b"", "group could not be confirmed"),
            ("other errno", "darwin", errno.EACCES, b"", b"", "Terminal startup or I/O failed"),
            ("other platform", "linux", errno.EPERM, b"", b"", "Terminal startup or I/O failed"),
        )
        for name, platform, code, stdout, stderr, message in cases:
            with self.subTest(name=name):
                events = Mock()
                events.killpg.side_effect = [None, PermissionError(code, "denied")]
                if stdout is None:
                    events.run.side_effect = subprocess.TimeoutExpired(ps, 1)
                else:
                    events.run.return_value = subprocess.CompletedProcess(ps, 0, stdout, stderr)
                child = Mock(pid=41, wait=events.wait)
                with (patch.object(native_usage.sys, "platform", platform),
                      patch.object(native_usage.pty, "openpty", return_value=(17, 18)),
                      patch.object(native_usage.fcntl, "ioctl"),
                      patch.object(native_usage.subprocess, "Popen", return_value=child),
                      patch.object(native_usage.subprocess, "run", events.run),
                      patch.object(native_usage.os, "close"),
                      patch.object(native_usage.os, "killpg", events.killpg),
                      patch.object(native_usage.time, "sleep"),
                      patch.object(native_usage, "exchange", return_value="Weekly limit 94%")):
                    if message is None:
                        result = native_usage.read_panel(["fixture"], "/", {}, request=b"/usage", prompt="ready",
                                                         menu="menu", complete="panel", blocked="trust")
                        self.assertEqual(result, "Weekly limit 94%")
                    else:
                        with self.assertRaisesRegex(native_usage.PanelError, message):
                            native_usage.read_panel(["fixture"], "/", {}, request=b"/usage", prompt="ready", menu="menu",
                                                    complete="panel", blocked="trust")
                expected = [call.killpg(41, signal.SIGTERM), call.killpg(41, signal.SIGKILL)]
                if platform == "darwin" and code == errno.EPERM:
                    expected.append(call.run(ps, env={"LC_ALL": "C"}, stdin=subprocess.DEVNULL,
                                             capture_output=True, timeout=1, check=True))
                self.assertEqual(events.mock_calls, [*expected, call.wait(timeout=1)])
                child.poll.assert_not_called()

    def test_timeout_and_interrupt_clean_up_a_descendant_after_the_leader_exits(self):
        probe = """
import fcntl
import os
import signal
import time

leader = os.getpid()
if os.fork():
    os._exit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
with open("descendant.lock", "w") as held:
    fcntl.flock(held, fcntl.LOCK_EX)
    deadline = time.monotonic() + 10
    while os.getppid() == leader and time.monotonic() < deadline:
        time.sleep(0.01)
    if os.getppid() != leader:
        print("descendant ready", flush=True)
        time.sleep(10)
"""
        exchange = native_usage.exchange
        for interrupt in (False, True):
            with self.subTest(interrupt=interrupt), tempfile.TemporaryDirectory() as cwd:
                def after_leader_exit(master, *args, **kwargs):
                    self.assertTrue(select.select([master], [], [], 5)[0], "fixture descendant never became ready")
                    self.assertIn(b"descendant ready", native_usage.os.read(master, 4096))
                    if interrupt:
                        raise KeyboardInterrupt
                    return exchange(master, *args, **kwargs)

                error = KeyboardInterrupt if interrupt else native_usage.PanelError
                with (patch.object(native_usage, "exchange", side_effect=after_leader_exit),
                      self.assertRaises(error) as raised):
                    native_usage.read_panel([sys.executable, "-c", probe], cwd, {},
                                            request=b"/usage", prompt="input prompt", menu="menu", complete="panel",
                                            blocked="trust", timeout=0.05)
                if not interrupt:
                    self.assertIn("Timed out waiting for the input prompt", str(raised.exception))
                # The OS releases this lock when the descendant exits, even if an
                # external reaper has yet to collect its status.
                with (Path(cwd) / "descendant.lock").open("r+") as held:
                    deadline = time.monotonic() + 1
                    while True:
                        try:
                            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except BlockingIOError:
                            if time.monotonic() >= deadline:
                                self.fail("PTY descendant still owns its lock after cleanup")
                            time.sleep(0.01)
