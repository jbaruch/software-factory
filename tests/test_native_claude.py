"""Fixed sanitized JSON and injected processes only; no Claude, providers or Rex."""
import runtime_path  # noqa: F401  Must precede runtime imports.
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import native_claude as native

SIGNED_IN = dict(loggedIn=True, authMethod="claude.ai", apiProvider="firstParty")
SIGNED_OUT = dict(loggedIn=False, authMethod="none", apiProvider="firstParty")


def transport(payload=SIGNED_IN, code=0):
    return Mock(side_effect=[subprocess.CompletedProcess([], 0, "2.1.319 (Claude Code)\n", ""),
                             subprocess.CompletedProcess([], code, json.dumps(payload), "fixture-private-stderr")])


class NativeClaude(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"HOME": "/fixture/native-home", "PATH": "/usr/bin:/bin"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        sockets = patch("socket.socket", side_effect=AssertionError("No sockets in native metadata tests"))
        sockets.start()
        self.addCleanup(sockets.stop)

    def test_all_documented_methods_preserve_sign_in_facts_without_eligibility(self):
        for method in native.AUTH_METHODS:
            facts = dict(SIGNED_IN, loggedIn=method != "none", authMethod=method)
            with self.subTest(method=method):
                self.assertEqual(native.curate(json.dumps(facts), returncode=int(method == "none")), facts)
                result = native.probe(sys.executable, Path.cwd(), run=transport(facts, int(method == "none")))
                self.assertEqual(result["auth"], facts)
                self.assertEqual(result["readiness"], "unverified")
                self.assertIsNone(result["quota_observation"])
                self.assertIsNone(result["subscription_eligibility"])

    def test_non_first_party_provider_is_preserved_without_inventing_eligibility(self):
        for provider in native.API_PROVIDERS:
            facts = dict(SIGNED_IN, authMethod="third_party", apiProvider=provider)
            self.assertEqual(native.curate(json.dumps(facts), returncode=0), facts)

    def test_private_extra_data_is_omitted_without_reading_auth_files(self):
        facts = dict(SIGNED_IN, email="fixture@example.invalid", accountId="fixture-account",
                     token="fixture-private-token", credentials={"secret": "fixture-secret"},
                     configDirectory="/fixture/private-config", projectsDirectory="/fixture/private-projects",
                     loginExpiresAt=123, analyticsDisabled=True, subscriptionType="fixture-tier")
        run = transport(facts)
        with patch("builtins.open", side_effect=AssertionError("No auth/config reads")):
            result = native.probe(sys.executable, Path.cwd(), run=run)
        self.assertEqual(result["auth"], SIGNED_IN)
        encoded = json.dumps(result)
        for private in ("fixture@", "fixture-account", "fixture-private", "fixture-secret", "fixture-tier",
                        "loginExpiresAt", "analyticsDisabled", "credentials", "native_output", "stderr"):
            self.assertNotIn(private, encoded)
        self.assertEqual(set(result), {"schema_version", "kind", "harness", "binary", "cwd", "cli_version",
                                      "started_at", "observed_at", "probe_seconds", "auth", "readiness",
                                      "quota_observation", "subscription_eligibility"})

    def test_probe_exact_commands_closed_stdin_environment_and_timing(self):
        run = transport(SIGNED_OUT, 1)
        clocks, ticks = iter([1000, 1002]), iter([10, 12])
        with patch.dict(os.environ, {"GH_TOKEN": "fixture-private", "NODE_OPTIONS": "fixture-injection"}):
            result = native.probe(sys.executable, Path.cwd(), run=run,
                                  clock=lambda: next(clocks), timer=lambda: next(ticks))
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [[str(Path(sys.executable).resolve()), "--version"],
                          [str(Path(sys.executable).resolve()), "--safe-mode", "--restricted", "auth", "status", "--json"]])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs, dict(cwd=str(Path.cwd().resolve()),
                             env={"HOME": "/fixture/native-home", "PATH": "/usr/bin:/bin",
                                  "PYTHONDONTWRITEBYTECODE": "1", "GIT_NO_REPLACE_OBJECTS": "1"},
                             stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                             errors="strict", timeout=20, check=False))
        self.assertEqual(result["auth"], SIGNED_OUT)
        self.assertEqual((result["started_at"], result["observed_at"], result["probe_seconds"]), (1000, 1002, 2))
        self.assertEqual((result["schema_version"], result["kind"], result["harness"]), (1, "claude-native-auth", "claude"))

    def test_wrong_known_types_missing_unknown_and_contradictory_facts_refuse(self):
        payloads = [[], None, True, {}, dict(SIGNED_IN, loggedIn=1), dict(SIGNED_IN, loggedIn="true"),
                    dict(SIGNED_IN, loggedIn=None), dict(SIGNED_IN, authMethod=[]), dict(SIGNED_IN, authMethod=None),
                    dict(SIGNED_IN, authMethod="unknown"), dict(SIGNED_IN, authMethod="none"),
                    dict(SIGNED_OUT, authMethod="api_key"), dict(SIGNED_IN, apiProvider=1),
                    dict(SIGNED_IN, apiProvider=None), dict(SIGNED_IN, apiProvider="unknown"),
                    dict(SIGNED_IN, apiProvider="fixture@example.invalid"), dict(SIGNED_IN, apiProvider="/private/path")]
        for key in SIGNED_IN:
            payloads.append({name: value for name, value in SIGNED_IN.items() if name != key})
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(native.ClaudeProbeError):
                native.curate(json.dumps(payload), returncode=0)
        for payload, code in ((SIGNED_IN, 1), (SIGNED_OUT, 0), (SIGNED_IN, 2), (SIGNED_OUT, -9),
                              (SIGNED_IN, True), (SIGNED_IN, 0.0)):
            with self.subTest(code=code), self.assertRaises(native.ClaudeProbeError):
                native.curate(json.dumps(payload), returncode=code)

    def test_strict_json_even_for_omitted_private_fields(self):
        encoded = json.dumps(SIGNED_IN)
        invalid = ["", "fixture-private", encoded + encoded, "warning\n" + encoded, encoded + " trailing",
                   encoded[:-1] + ', "loggedIn": true}', encoded[:-1] + ', "private": {"x": 1, "x": 2}}',
                   b"\xff", 123, "[" * 2000 + "]" * 2000]
        for number in ("NaN", "Infinity", "-Infinity", "1e999", "-1e999", "9" * 400):
            invalid.append(encoded[:-1] + ', "omitted": ' + number + "}")
        for output in invalid:
            with self.subTest(output=str(output)[:60]), self.assertRaises(native.ClaudeProbeError):
                native.curate(output, returncode=0)

    def test_environment_overrides_including_empty_refuse_before_child(self):
        names = [prefix + "FIXTURE" for prefix in native.OVERRIDE_PREFIXES]
        names += ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
                  "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                  "AWS_PROFILE", "GOOGLE_APPLICATION_CREDENTIALS", "AZURE_FOUNDRY_API_KEY"]
        for name in names:
            for value in ("", "fixture-private"):
                run = Mock()
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                    with self.assertRaisesRegex(native.ClaudeProbeError, "Unset") as error:
                        native.probe(sys.executable, Path.cwd(), run=run)
                run.assert_not_called()
                self.assertNotIn("fixture-private", str(error.exception))

    def test_assignment_environment_disables_background_tasks(self):
        # A writer's background shell outlived its result; Claude woke again and the run ended with two results.
        self.assertEqual(native.environment()["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "1")
        with patch.dict(os.environ, {"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0"}):
            with self.assertRaisesRegex(native.ClaudeProbeError, "Unset"):
                native.environment()

    def test_explicit_config_directory_preserved_without_reading_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            run = transport()
            with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": directory}), patch("builtins.open", side_effect=AssertionError):
                native.probe(sys.executable, Path.cwd(), run=run)
            for call in run.call_args_list:
                self.assertEqual(call.kwargs["env"]["CLAUDE_CONFIG_DIR"], directory)
        for directory in ("", "relative-config", "/nonexistent/fixture-config", sys.executable):
            run = Mock()
            with self.subTest(directory=directory), patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": directory}):
                with self.assertRaisesRegex(native.ClaudeProbeError, "intended native account"):
                    native.probe(sys.executable, Path.cwd(), run=run)
            run.assert_not_called()

    def test_invalid_paths_and_permissions_refuse_before_child(self):
        for binary, cwd in (("relative", Path.cwd()), (sys.executable, "relative"),
                            ("/nonexistent/fixture", Path.cwd()), (Path.cwd(), Path.cwd()),
                            (sys.executable, sys.executable), (None, Path.cwd()),
                            (sys.executable, "/bad\0path")):
            run = Mock()
            with self.subTest(binary=binary, cwd=cwd), self.assertRaises(native.ClaudeProbeError):
                native.probe(binary, cwd, run=run)
            run.assert_not_called()
        for permitted in (False, True):
            run = Mock()
            with patch("native_claude.os.access", side_effect=[permitted, False]), self.assertRaises(native.ClaudeProbeError):
                native.probe(sys.executable, Path.cwd(), run=run)
            run.assert_not_called()

    def test_process_errors_and_invalid_utf8_are_sanitized_without_retry(self):
        for error in (OSError("fixture-private-path/token"), subprocess.TimeoutExpired("fixture-private", 20,
                       output="fixture-secret", stderr="fixture-secret"),
                      UnicodeDecodeError("utf-8", b"\xff", 0, 1, "fixture-private")):
            for stage in (0, 1):
                run = Mock(side_effect=[error] if stage == 0 else [subprocess.CompletedProcess([], 0, "2.1.319", ""), error])
                with self.subTest(error=type(error), stage=stage), self.assertRaises(native.ClaudeProbeError) as raised:
                    native.probe(sys.executable, Path.cwd(), run=run)
                self.assertNotIn("fixture-", str(raised.exception))
                self.assertEqual(run.call_count, stage + 1)

    def test_bad_version_stops_before_auth_and_bad_auth_never_retries(self):
        for code, version in ((1, "2.1.319"), (0, ""), (0, "fixture-secret"), (0, "2.1.319\nfixture-private"),
                              (False, "2.1.319"), (0, b"2.1.319"), (0, "2.1.317-beta"),
                              (0, "2.1"), (0, "2.1.317.0"), (0, "v2.1.317")):
            run = Mock(return_value=subprocess.CompletedProcess([], code, version, "fixture-secret"))
            with self.subTest(code=code, version=version), self.assertRaises(native.ClaudeProbeError):
                native.probe(sys.executable, Path.cwd(), run=run)
            self.assertEqual(run.call_count, 1)
        for code, output in ((2, json.dumps(SIGNED_IN)), (0, "fixture-secret"), (1, json.dumps(SIGNED_IN))):
            run = Mock(side_effect=[subprocess.CompletedProcess([], 0, "2.1.319", ""),
                                    subprocess.CompletedProcess([], code, output, "fixture-secret")])
            with self.assertRaises(native.ClaudeProbeError):
                native.probe(sys.executable, Path.cwd(), run=run)
            self.assertEqual(run.call_count, 2)

    def test_older_versions_refuse_with_only_the_version_call(self):
        for version in ("2.1.316", "2.1.316 (Claude Code)\n", "2.1.99", "2.0.999", "1.99.9999"):
            run = Mock(return_value=subprocess.CompletedProcess([], 0, version, "fixture-private"))
            with self.subTest(version=version), self.assertRaisesRegex(
                    native.ClaudeProbeError, "Update.*2.1.317 or newer"):
                native.probe(sys.executable, Path.cwd(), run=run)
            self.assertEqual([call.args[0] for call in run.call_args_list],
                             [[str(Path(sys.executable).resolve()), "--version"]])

    def test_exact_floor_installed_and_newer_versions_preserve_sanitized_facts(self):
        for version in ("2.1.317", "2.1.317 (Claude Code)\n", "2.1.319 (Claude Code)\n",
                        "2.1.1000", "2.2.0", "3.0.0"):
            for facts, code in ((SIGNED_IN, 0), (SIGNED_OUT, 1)):
                payload = dict(facts, token="fixture-private-token", email="fixture@example.invalid")
                run = Mock(side_effect=[subprocess.CompletedProcess([], 0, version, "fixture-private"),
                                        subprocess.CompletedProcess([], code, json.dumps(payload), "fixture-private")])
                with self.subTest(version=version, code=code):
                    result = native.probe(sys.executable, Path.cwd(), run=run)
                    self.assertEqual([call.args[0] for call in run.call_args_list],
                                     [[str(Path(sys.executable).resolve()), "--version"],
                                      [str(Path(sys.executable).resolve()), "--safe-mode", "--restricted",
                                       "auth", "status", "--json"]])
                    self.assertEqual(result["cli_version"], version.strip())
                    self.assertEqual(result["auth"], facts)
                    self.assertEqual(result["schema_version"], 1)
                    self.assertEqual(result["readiness"], "unverified")
                    self.assertIsNone(result["quota_observation"])
                    self.assertIsNone(result["subscription_eligibility"])
                    self.assertNotIn("fixture", json.dumps(result))

    def test_bad_clock_refuses(self):
        for times, ticks in (([10, 9], [1, 2]), ([10, float("inf")], [1, 2]), ([True, 10], [1, 2]),
                              ([10, 11], [2, 1]), ([10, 11], [1, float("nan")])):
            clocks, timers = iter(times), iter(ticks)
            with self.subTest(times=times, ticks=ticks), self.assertRaises(native.ClaudeProbeError):
                native.probe(sys.executable, Path.cwd(), run=transport(),
                             clock=lambda: next(clocks), timer=lambda: next(timers))

    def test_cli_signed_out_success_and_no_partial_json_on_failure(self):
        original_probe = native.probe
        for run, expected in ((transport(SIGNED_OUT, 1), 0), (transport(SIGNED_IN, 1), 1),
                              (Mock(side_effect=OSError("fixture-private")), 1)):
            stdout, stderr = StringIO(), StringIO()
            def injected(binary, cwd):
                return original_probe(binary, cwd, run=run)
            with patch.object(native, "probe", side_effect=injected), redirect_stdout(stdout), redirect_stderr(stderr):
                result = native.main(["--binary", sys.executable, "--cwd", str(Path.cwd())])
            self.assertEqual(result, expected)
            if expected == 0:
                self.assertEqual(json.loads(stdout.getvalue())["auth"], SIGNED_OUT)
                self.assertEqual(stderr.getvalue(), "")
            else:
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("native-claude:", stderr.getvalue())
                self.assertNotIn("fixture-private", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
