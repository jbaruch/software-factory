"""Run the shipped launcher and ready runner without host profiles, Rex or providers."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import factory
from scripts import smoke_rex


CONTROLLER = Path(__file__).resolve().parents[1] / "skills/factory/runtime/ready_controller.py"


class ReadyControllerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "project with spaces"
        self.project.mkdir()
        self.plan = "plan $(touch injected) 'quoted'.json"
        (self.project / self.plan).write_text(json.dumps(dict(schema_version=1, limits=dict(
            workers=1, tasks=1, reserve_workers=0, minimum_remaining_percent=15), jobs=[])))
        self.state = self.root / "runner state"
        self.python = self.root / "pinned python"
        self.python.symlink_to(sys.executable)

        # Exercise real Bash argument handling and BASH_ENV without the host login profiles.
        bash = self.root / "bash"
        bash.write_text('#!/bin/sh\nexec /bin/bash --noprofile "$@"\n')
        bash.chmod(0o700)

    def run_controller(self, log, *, project=None, env=None, extra=()):
        return subprocess.run([sys.executable, "-B", str(CONTROLLER), str(log),
            str(project if project is not None else self.project),
            str(self.python), "--",
            "--plan", self.plan, "--state-dir", str(self.state), "--once",
            "--codex", str(self.root / "absent-codex"), "--rex", str(self.root / "absent-rex"),
            "--server", "unix://" + str(self.root / "absent-rpc"), *extra],
            env={"PATH": str(self.root), **(env or {})}, text=True, capture_output=True, timeout=5)

    def check_exit(self, result, log, expected):
        self.assertEqual(result.returncode, expected, result.stderr)
        self.assertEqual(json.loads(result.stdout), dict(log=str(log), controller_exit=expected))
        output, trailer = log.read_text().rsplit("\ncontroller_exit=", 1)
        self.assertEqual(trailer, f"{expected}\n")
        return output

    def test_shell_startup_runs_after_log_creation_and_retains_both_streams(self):
        log = self.root / "startup.log"
        startup = self.root / "startup fixture.sh"
        notice = '{"result":"startup diagnostics ready"}'
        startup.write_text('test -f "$EXPECTED_LOG" || exit 91\n'
                           f"printf '%s\\n' '{notice}'\n"
                           "printf 'EARLY_STDOUT\\n'\nprintf 'EARLY_STDERR\\n' >&2\nexit 37\n")
        result = self.run_controller(log, env=dict(BASH_ENV=str(startup), EXPECTED_LOG=str(log)))
        self.assertEqual(self.check_exit(result, log, 37), f"{notice}\nEARLY_STDOUT\nEARLY_STDERR\n")
        smoke_rex.check_controller_run(result, log, 37)
        self.assertEqual(stat.S_IMODE(log.stat().st_mode), 0o600)
        self.assertFalse(self.state.exists())

    def test_early_zero_fails_even_with_a_descendant_holding_the_pipe(self):
        log = self.root / "startup-zero.log"
        startup = self.root / "startup-zero.sh"
        pid = self.root / "startup-child.pid"
        startup.write_text('/bin/sleep 30 >/dev/null 2>&1 &\nprintf "%s\\n" "$!" > "$CHILD_PID"\n'
                           "printf 'EARLY_STDOUT\\n'\nprintf 'EARLY_STDERR\\n' >&2\nexit 0\n")
        try:
            result = self.run_controller(log, env=dict(BASH_ENV=str(startup), CHILD_PID=str(pid)))
            output = self.check_exit(result, log, 1)
            self.assertTrue(output.startswith("EARLY_STDOUT\nEARLY_STDERR\n"))
            self.assertIn("runner did not acknowledge startup", output)
            self.assertFalse(self.state.exists())
        finally:
            if pid.exists():
                os.kill(int(pid.read_text()), signal.SIGTERM)

    def test_busy_attempt_then_retry_runs_the_same_plan_with_a_fresh_log(self):
        busy = self.root / "busy.log"
        with factory.execution_lock(self.state):
            attempt = self.run_controller(busy)
        self.assertIn("owns execution", self.check_exit(attempt, busy, 75))
        retry = self.root / "retry.log"
        output = json.loads(self.check_exit(self.run_controller(retry), retry, 0))
        self.assertEqual(output["result"], "complete")
        self.assertEqual(output["jobs"], [])
        self.assertFalse((self.project / "injected").exists())
        self.assertTrue(busy.read_text().endswith("\ncontroller_exit=75\n"))

    def test_child_directory_and_runner_parser_failures_keep_exact_codes(self):
        cases = [
            (1, {"project": self.root / "absent-project"}, "absent-project"),
            (2, {"extra": ("--unknown-runner-option",)}, "unrecognized arguments"),
        ]
        for expected, options, diagnostic in cases:
            with self.subTest(expected=expected):
                log = self.root / f"failure-{expected}.log"
                result = self.run_controller(log, **options)
                self.assertIn(diagnostic, self.check_exit(result, log, expected))

    def test_missing_shell_retains_diagnostic_and_status(self):
        log = self.root / "missing-shell.log"
        result = self.run_controller(log, env=dict(PATH=str(self.root / "absent-bin")))
        self.assertIn("cannot start bash", self.check_exit(result, log, 127))

    def test_terminated_shell_maps_to_signal_exit_code(self):
        terminated = self.root / "terminated.log"
        startup = self.root / "terminated.sh"
        startup.write_text('kill -TERM "$$"\n')
        result = self.run_controller(terminated, env=dict(BASH_ENV=str(startup)))
        self.check_exit(result, terminated, 128 + signal.SIGTERM)

    def test_smoke_accepts_real_runner_result_after_startup_notices(self):
        startup = self.root / "startup-notice.sh"
        startup.write_text("printf 'STARTUP_STDOUT'\nprintf 'STARTUP_STDERR' >&2\n")
        for label in ("success", "retry"):
            with self.subTest(label=label):
                log = self.root / (label + ".log")
                result = self.run_controller(log, env=dict(BASH_ENV=str(startup)))
                output = self.check_exit(result, log, 0)
                self.assertTrue(output.startswith("STARTUP_STDOUTSTARTUP_STDERR"))
                smoke_rex.check_controller_run(result, log, 0)

    def test_smoke_accepts_startup_descendant_notice_after_launcher_returns(self):
        log = self.root / "descendant.log"
        startup = self.root / "descendant.sh"
        release = self.root / "release-descendant"
        startup.write_text("""(
    for ((attempt=0; attempt<1000; attempt++)); do
        if [[ -f "$RELEASE_DESCENDANT" ]]; then
            printf 'LATE_STARTUP_NOTICE\\n' >&2
            exit
        fi
        /bin/sleep 0.01
    done
) &
""")
        try:
            result = self.run_controller(log, env=dict(BASH_ENV=str(startup), RELEASE_DESCENDANT=str(release)))
            self.check_exit(result, log, 0)
            original = log.read_bytes()
        finally:
            release.touch()
        deadline = time.monotonic() + 5
        while log.read_bytes() == original and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(log.read_bytes(), original + b"LATE_STARTUP_NOTICE\n")
        smoke_rex.check_controller_run(result, log, 0)

    def test_smoke_reports_process_summary_status_and_runner_errors_separately(self):
        log = self.root / "invalid.log"
        summary = json.dumps(dict(log=str(log), controller_exit=37))
        run = subprocess.CompletedProcess([], 37, stdout=summary)
        with self.assertRaisesRegex(RuntimeError, "Controller process exited 1, expected 37"):
            smoke_rex.check_controller_run(subprocess.CompletedProcess([], 1, stdout="invalid"), log, 37)
        with self.assertRaisesRegex(RuntimeError, "Invalid launcher JSON"):
            smoke_rex.check_controller_run(subprocess.CompletedProcess([], 37, stdout="invalid"), log, 37)
        with self.assertRaisesRegex(RuntimeError, "Launcher JSON did not report exit 37"):
            smoke_rex.check_controller_run(subprocess.CompletedProcess([], 37, stdout="{}"), log, 37)
        log.write_text("EARLY_STDOUT\nEARLY_STDERR\ncontroller_exit=0\n")
        with self.assertRaisesRegex(RuntimeError, "Expected one controller_exit=37 record"):
            smoke_rex.check_controller_run(run, log, 37)
        log.write_text("EARLY_STDOUT\ncontroller_exit=37\n")
        with self.assertRaisesRegex(RuntimeError, "Shell startup diagnostics from both streams"):
            smoke_rex.check_controller_run(run, log, 37)
        success_log = self.root / "success.log"
        success = self.run_controller(success_log)
        runner_output = self.check_exit(success, success_log, 0)
        log.write_text(runner_output + "\ncontroller_exit=37\n")
        with self.assertRaisesRegex(RuntimeError, "Unexpected runner result"):
            smoke_rex.check_controller_run(run, log, 37)
        success_log.write_text(runner_output + "A notice after the runner result\n\ncontroller_exit=0\n")
        smoke_rex.check_controller_run(success, success_log, 0)
        success_log.write_text(runner_output + runner_output + "\ncontroller_exit=0\n")
        with self.assertRaisesRegex(RuntimeError, "Expected one structured runner result"):
            smoke_rex.check_controller_run(success, success_log, 0)

    def test_smoke_state_directory_is_private_before_clone(self):
        state = self.root / "new parent" / "smoke state"
        argv = ["smoke_rex", "--rex", "absent-rex", "--server", "absent-server", "--state-dir", str(state)]
        umask = os.umask(0o022)
        try:
            with patch.object(sys, "argv", argv), patch.object(smoke_rex.subprocess, "run",
                    side_effect=subprocess.CalledProcessError(1, "git clone")) as clone:
                with self.assertRaises(subprocess.CalledProcessError):
                    smoke_rex.main()
                self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
                clone.assert_called_once()
                state.chmod(0o755)
                clone.reset_mock()
                with patch.object(sys, "stderr"), self.assertRaises(SystemExit) as refused:
                    smoke_rex.main()
                self.assertEqual(refused.exception.code, 2)
                clone.assert_not_called()
        finally:
            os.umask(umask)

    def test_existing_log_refuses_before_startup_and_keeps_evidence(self):
        log = self.root / "existing.log"
        log.write_text("previous evidence")
        result = self.run_controller(log)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Choose a fresh log", result.stderr)
        self.assertEqual(log.read_text(), "previous evidence")
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
