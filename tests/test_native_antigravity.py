"""Recorded native usage rows and fake transports; no provider or Rex calls."""
from contextlib import redirect_stderr
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import native_antigravity as native

USAGE = ("Gemini Models\tWeekly Limit Remaining\t99%\t2026-11-14T09:20:42Z\n"
         "Claude and GPT models\tWeekly Limit Remaining\t0%\t2026-11-08T19:45:58Z\n")


class NativeAntigravity(unittest.TestCase):
    def setUp(self):
        environment = {key: value for key, value in os.environ.items() if key not in native.API_ENVIRONMENT}
        clean = patch.dict(os.environ, environment, clear=True)
        clean.start()
        self.addCleanup(clean.stop)

    def test_native_groups_percentages_and_resets_are_preserved(self):
        groups = native.parse_usage(USAGE)
        self.assertEqual(groups, [dict(group="Gemini Models", window="weekly", percent_remaining=99.0,
                                      resets_at="2026-11-14T09:20:42Z"),
                                  dict(group="Claude and GPT models", window="weekly", percent_remaining=0.0,
                                      resets_at="2026-11-08T19:45:58Z")])
        self.assertEqual(native.parse_usage(USAGE.replace("99%", "0.5%"))[0]["percent_remaining"], 0.5)

    def test_missing_changed_duplicate_or_invalid_usage_refuses(self):
        for output in ("", "Authentication required", USAGE + USAGE, USAGE.replace("99%", "101%"),
                       USAGE.replace("99%", "nan%"), USAGE.replace("99%", "-1%"),
                       USAGE.replace("Weekly Limit Remaining", "Five Hour Limit Remaining"),
                       USAGE.replace("2026-11-14T09:20:42Z", "2026-11-14T09:20:42"),
                       USAGE.replace("2026-11-14T09:20:42Z", "not-a-date")):
            with self.subTest(output=output), self.assertRaises(native.UsageError):
                native.parse_usage(output)

    def test_probe_is_metadata_only_filtered_and_preserves_unknown_readiness(self):
        run = Mock(side_effect=[subprocess.CompletedProcess([], 0, "1.5.3\n", ""),
                                subprocess.CompletedProcess([], 0, USAGE, "")])
        ticks = iter([10, 12])
        with patch.dict(os.environ, {"GH_TOKEN": "fixture-private-token"}):
            result = native.probe(sys.executable, Path.cwd(), run=run, clock=lambda: 1000, timer=lambda: next(ticks))
        self.assertEqual([call.args[0][1:] for call in run.call_args_list], [["--version"], ["--print", "/usage"]])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
            self.assertNotIn("GH_TOKEN", call.kwargs["env"])
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["cli_version"], "1.5.3")
        self.assertEqual(result["probe_seconds"], 2)
        self.assertEqual(result["observed_at"], 1000)
        self.assertEqual(result["native_output"], USAGE)
        self.assertEqual(result["readiness"], "unverified")

    def test_api_or_adc_overrides_refuse_before_start(self):
        for key in native.API_ENVIRONMENT:
            run = Mock()
            with self.subTest(key=key), patch.dict(os.environ, {key: ""}), self.assertRaisesRegex(native.UsageError, "Unset"):
                native.probe(sys.executable, Path.cwd(), run=run)
            run.assert_not_called()

    def test_path_failures_refuse_without_transport(self):
        run = Mock()
        for binary, cwd in (("relative-agy", Path.cwd()), (sys.executable, Path.cwd() / "missing-native-fixture"),
                            (str(Path.cwd()), Path.cwd())):
            with self.subTest(binary=binary, cwd=cwd), self.assertRaises(native.UsageError):
                native.probe(binary, cwd, run=run)
        run.assert_not_called()

    def test_process_failures_hide_native_diagnostics_and_do_not_retry(self):
        for error in (OSError("private fixture"), subprocess.TimeoutExpired("private fixture", 20),
                      UnicodeDecodeError("utf-8", b"\xff", 0, 1, "private fixture")):
            run = Mock(side_effect=error)
            with self.subTest(error=type(error)), self.assertRaises(native.UsageError) as caught:
                native.probe(sys.executable, Path.cwd(), run=run)
            self.assertNotIn("private fixture", str(caught.exception))
            self.assertEqual(run.call_count, 1)
        for result in (subprocess.CompletedProcess([], 1, "private fixture", ""),
                       subprocess.CompletedProcess([], 0, "1.5.3", "private fixture"),
                       subprocess.CompletedProcess([], 0, "", "")):
            with self.subTest(result=result), self.assertRaises(native.UsageError):
                native.probe(sys.executable, Path.cwd(), run=Mock(return_value=result))

    def test_cli_reports_actionable_error_without_traceback(self):
        error = StringIO()
        with patch.object(native, "probe", side_effect=native.UsageError("Inspect native sign-in")), redirect_stderr(error):
            code = native.main(["--binary", sys.executable, "--cwd", str(Path.cwd())])
        self.assertEqual(code, 1)
        self.assertEqual(error.getvalue(), "native-antigravity: Inspect native sign-in\n")


if __name__ == "__main__":
    unittest.main()
