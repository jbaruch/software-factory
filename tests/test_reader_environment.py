"""Read-only assignments read GitHub but never change it: a Codex release judgment merged and deployed."""
import runtime_path  # noqa: F401  Must precede runtime imports.
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
        self.token = self.root / "github-read-token"
        for name, value in (("GITHUB_READ_TOKEN", self.token), ("READER_GH_CONFIG", self.root / "gh-reader")):
            patcher = patch.object(factory, name, value)
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

    def test_readers_reach_the_read_only_token_through_gh_never_the_environment(self):
        self.token.write_text("github_pat_readonly\n")
        self.token.chmod(0o600)
        environment = factory.reader_environment(self.base, False)
        self.assertNotIn("github_pat_readonly", "\n".join(environment.values()))  # An `env` dump can't log it.
        hosts = self.root / "gh-reader" / "hosts.yml"
        self.assertIn("oauth_token: github_pat_readonly", hosts.read_text())
        self.assertEqual(hosts.stat().st_mode & 0o777, 0o600)
        self.token.unlink()
        factory.reader_environment(self.base, False)
        self.assertFalse(hosts.exists())

    def test_an_exposed_or_malformed_token_refuses(self):
        self.token.write_text("github_pat_readonly\n")
        self.token.chmod(0o644)
        with self.assertRaisesRegex(factory.FactoryError, "chmod 600"):
            factory.reader_environment(self.base, False)
        self.token.write_text("two tokens\n")
        self.token.chmod(0o600)
        with self.assertRaisesRegex(factory.FactoryError, "one read-only GitHub token"):
            factory.reader_environment(self.base, False)


if __name__ == "__main__":
    unittest.main()
