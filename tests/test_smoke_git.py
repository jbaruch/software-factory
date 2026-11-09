"""Offline smoke orchestration with fake sandbox/signing transports and owned Git fixtures."""
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from scripts import smoke_git as smoke


HOOK_FACTS = {name: True for name in (
    "external_push_blocked", "credential_read_blocked", "private_key_read_blocked",
    "network_blocked", "publication_token_absent", "signer_environment_absent",
    "arbitrary_agent_signing_blocked", "sibling_index_write_blocked", "inherited_git_auth_absent",
    "git_auth_settings_absent", "original_config_read_blocked")}


class SmokeGit(unittest.TestCase):
    @contextmanager
    def transports(self, *, hook_facts=None):
        real_run, real_which = subprocess.run, shutil.which
        keygen = str(Path(sys.executable).resolve())
        calls = []
        def which(name, *args, **kwargs):
            return keygen if name == "ssh-keygen" else real_which(name, *args, **kwargs)
        def run(command, **options):
            if command[0] == keygen:
                if "-q" in command:
                    key = Path(command[command.index("-f") + 1])
                    key.write_text("synthetic private-key fixture\n")
                    key.with_suffix(".pub").write_text("ssh-ed25519 synthetic-public-key\n")
                return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
            result = real_run(command, **options)
            if "cat-file" in command and "commit" in command:
                result.stdout += "\ngpgsig -----BEGIN SSH SIGNATURE-----\n"
            return result
        def commit(args, receipt):
            calls.append(Path.cwd())
            if len(calls) == 2:
                raise smoke.factory.FactoryError("Unsupported external hook paths")
            if len(calls) == 3:
                receipt["commit"]["state"] = "committing"
                raise smoke.factory.FactoryError("Secret scan refused the staged tree")
            git = smoke.factory.git_binary()
            environment = {**smoke.factory.git_environment(), "GIT_CONFIG_GLOBAL": os.devnull,
                           "GIT_CONFIG_SYSTEM": os.devnull}
            real_run([git, "add", "--all"], check=True, capture_output=True, env=environment)
            real_run([git, "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false",
                      "commit", "-qm", args.commit_message], check=True, capture_output=True, env=environment)
            return subprocess.CompletedProcess([], 0, stdout=json.dumps(HOOK_FACTS if hook_facts is None else hook_facts), stderr="")
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}))
            stack.enter_context(patch.object(smoke.shutil, "which", side_effect=which))
            stack.enter_context(patch.object(smoke.subprocess, "run", side_effect=run))
            stack.enter_context(patch.object(smoke.factory_commit, "isolated_git", side_effect=lambda *a, **k: nullcontext()))
            stack.enter_context(patch.object(smoke.factory_commit, "private_signer", side_effect=lambda *a: nullcontext("fixture-agent")))
            stack.enter_context(patch.object(smoke.factory_commit, "commit_changes", side_effect=commit))
            yield calls

    def test_fixture_orchestration_reports_checks_and_removes_owned_repositories(self):
        before = Path.cwd()
        with self.transports() as calls:
            result = smoke.conformance(Path(sys.executable))
        self.assertEqual(result["schema_version"], 1)
        self.assertTrue(all(result["checks"].values()))
        self.assertTrue(result["checks"]["credential_commit_refused_without_hook"])
        self.assertTrue(result["checks"]["external_precommit_refused_before_staging"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(Path.cwd(), before)
        self.assertTrue(all(not directory.exists() for directory in calls))

    def test_cli_json_preserves_a_failed_check(self):
        facts = {**HOOK_FACTS, "network_blocked": False}
        output, error = StringIO(), StringIO()
        with self.transports(hook_facts=facts), redirect_stdout(output), redirect_stderr(error):
            code = smoke.main(["--codex", sys.executable])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output.getvalue())["checks"]["network_blocked"])
        self.assertEqual(error.getvalue(), "")

    def test_incomplete_hook_evidence_fails_visibly_without_json_or_leftovers(self):
        before = Path.cwd()
        output, error = StringIO(), StringIO()
        with self.transports(hook_facts={}) as calls, redirect_stdout(output), redirect_stderr(error):
            code = smoke.main(["--codex", sys.executable])
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("hook evidence is incomplete", error.getvalue())
        self.assertEqual(Path.cwd(), before)
        self.assertTrue(all(not directory.exists() for directory in calls))

    def test_invalid_binary_refuses_before_fixture_creation(self):
        output, error = StringIO(), StringIO()
        with patch.object(smoke, "conformance") as fixture, redirect_stdout(output), redirect_stderr(error):
            code = smoke.main(["--codex", "relative-not-a-cli"])
        self.assertEqual(code, 1)
        fixture.assert_not_called()
        self.assertEqual(output.getvalue(), "")
        self.assertIn("absolute native Codex executable", error.getvalue())


if __name__ == "__main__":
    unittest.main()
