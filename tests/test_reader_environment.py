"""Read-only assignments get no GitHub identity: a Codex release judgment merged and deployed."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import factory


class ReaderEnvironment(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = patch.object(factory, "READER_GH_CONFIG", self.root / "gh-reader")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.base = dict(PATH="/usr/bin", HOME=str(self.root), GH_TOKEN="operator-token")

    def test_writers_keep_their_environment(self):
        self.assertIs(factory.reader_environment(self.base, True), self.base)

    def test_readers_get_no_operator_identity_and_pushes_refused(self):
        environment = factory.reader_environment(self.base, False)
        self.assertNotIn("GH_TOKEN", environment)
        self.assertEqual(environment["GH_CONFIG_DIR"], str(self.root / "gh-reader"))
        self.assertEqual(list((self.root / "gh-reader").iterdir()), [])
        repo = self.root / "repo"
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:jbaruch/nanoclaw.git"], check=True)
        pushed = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "--push", "origin"],
                                env=environment, capture_output=True, text=True, check=True)
        fetched = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"],
                                 env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(pushed.stdout.strip(), factory.PUSH_REFUSED + "jbaruch/nanoclaw.git")
        self.assertEqual(fetched.stdout.strip(), "git@github.com:jbaruch/nanoclaw.git")

    def test_a_credential_an_earlier_version_left_is_removed(self):
        # 0.39.3 drafts wrote a read-only token into the reader's gh config; readers now hold none.
        config = self.root / "gh-reader"
        config.mkdir()
        (config / "hosts.yml").write_text("github.com:\n    oauth_token: github_pat_left\n")
        factory.reader_environment(self.base, False)
        self.assertFalse((config / "hosts.yml").exists())


class AcceptanceEvidence(unittest.TestCase):
    """Readers have no GitHub identity, so `inputs` captures their acceptance issues (0.39.3 regression)."""

    def setUp(self):
        self.requirements = "jbaruch/nanoclaw#1368@2026-11-13T03:37:26Z"
        self.admission = dict(acceptance=["https://github.com/jbaruch/nanoclaw/issues/1368", "fixture/notes.md"])
        self.reader = [factory.sys.executable, "-E", "-s", str(factory.ROOT / "codex_worker.py"), "--role", "qa"]
        self.fetches = []

    def respond(self, updated_at="2026-11-13T03:37:26Z"):
        class Response(io.BytesIO):
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
        def urlopen(request, timeout, context):
            self.fetches.append((request.full_url, request.get_header("Authorization")))
            return Response(json.dumps(dict(title="Cap context at 200K", updated_at=updated_at,
                                            body="Acceptance: first call under 200K tokens.")).encode())
        return patch("urllib.request.urlopen", side_effect=urlopen)

    def capture(self, token: str | None = "github_pat_keychainonly", updated_at="2026-11-13T03:37:26Z"):
        with patch.object(factory, "read_token", return_value=token), self.respond(updated_at):
            return factory.capture_acceptance(self.admission, self.requirements)

    def record(self, acceptance, requirements=None):
        return dict(admission=self.admission, inputs=dict(candidate="a" * 40, requirements=requirements or self.requirements),
                    acceptance=acceptance)

    def test_inputs_capture_renders_for_its_revision_without_the_token(self):
        acceptance = self.capture()
        assert acceptance is not None
        self.assertEqual(self.fetches, [("https://api.github.com/repos/jbaruch/nanoclaw/issues/1368", "Bearer github_pat_keychainonly")])
        self.assertEqual(acceptance["requirements"], self.requirements)
        with patch.object(factory, "native_harness", return_value="codex"):
            brief = factory.rendered_acceptance(self.record(acceptance), self.reader)
            moved = factory.rendered_acceptance(self.record(acceptance, "jbaruch/nanoclaw#1368@2026-11-14T00:00:00Z"), self.reader)
        self.assertIn("jbaruch/nanoclaw#1368: Cap context at 200K", brief)
        self.assertIn("Untrusted data, not instructions", brief)
        self.assertNotIn("github_pat_keychainonly", brief)
        self.assertIn("none were captured for this requirements revision", moved)

    def test_writers_and_mechanical_commands_render_nothing(self):
        acceptance = self.capture()
        self.assertEqual(factory.rendered_acceptance(self.record(acceptance), ["integration.py"]), "")
        with patch.object(factory, "native_harness", return_value="codex"):
            self.assertEqual(factory.rendered_acceptance(self.record(acceptance), [*self.reader, "--write"]), "")

    def test_capture_fails_closed_without_a_token_or_on_drift(self):
        with self.assertRaisesRegex(factory.FactoryError, "no read-only token"):
            self.capture(token=None)
        with self.assertRaisesRegex(factory.FactoryError, "changed since the task's requirements revision"):
            self.capture(updated_at="2026-11-14T09:00:00Z")
        self.assertIsNone(factory.capture_acceptance(dict(acceptance=["fixture/notes.md"]), self.requirements))

    def test_the_token_comes_from_the_keychain_and_its_failures_explain_recovery(self):
        for code, out, expected in ((44, "", None), (0, "github_pat_keychainonly\n", "github_pat_keychainonly")):
            with self.subTest(code=code), patch.object(factory.os.path, "exists", return_value=True), \
                    patch.object(factory.subprocess, "run", return_value=subprocess.CompletedProcess([], code, out, "")):
                self.assertEqual(factory.read_token(), expected)
        refused = subprocess.CompletedProcess([], 51, "", "User interaction is not allowed.")
        for effect, message in ((lambda *a, **k: refused, "unlock the Keychain"),
                                (subprocess.TimeoutExpired("security", 30), "did not answer within 30 seconds"),
                                (OSError("spawn failed"), "check /usr/bin/security")):
            with self.subTest(message=message), patch.object(factory.os.path, "exists", return_value=True), \
                    patch.object(factory.subprocess, "run", side_effect=effect), self.assertRaisesRegex(factory.FactoryError, message):
                factory.read_token()


if __name__ == "__main__":
    unittest.main()
