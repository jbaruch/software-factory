"""Read-only assignments get no GitHub identity: a Codex release judgment merged and deployed."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
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

    def respond(self, updated_at="2026-11-13T03:37:26Z", *, edited_at=None, renamed_at=None, answer=None):
        """GitHub's GraphQL answer for the acceptance issue, created 2026-11-12 and changed as the arguments say."""
        class Response(io.BytesIO):
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
        def urlopen(request, timeout):
            self.fetches.append((request.full_url, request.get_header("Authorization"), json.loads(request.data)["variables"]))
            issue = dict(title="Cap context at 200K", body="Acceptance: first call under 200K tokens.",
                         createdAt="2026-11-12T06:07:07Z", updatedAt=updated_at, lastEditedAt=edited_at,
                         timelineItems=dict(nodes=[dict(createdAt=renamed_at)] if renamed_at else []))
            return Response(json.dumps(answer or dict(data=dict(repository=dict(issueOrPullRequest=issue)))).encode())
        return patch("urllib.request.OpenerDirector.open", side_effect=lambda request, timeout: urlopen(request, timeout))

    def capture(self, token: str | None = "github_pat_keychainonly", updated_at="2026-11-13T03:37:26Z", **changes):
        with patch.object(factory, "read_token", return_value=token), self.respond(updated_at, **changes):
            return factory.capture_acceptance(self.admission, self.requirements)

    def record(self, acceptance, requirements=None):
        return dict(task="task", admission=self.admission,
                    inputs=dict(candidate="a" * 40, requirements=requirements or self.requirements),
                    acceptance={self.requirements: acceptance})

    def test_inputs_capture_renders_for_its_revision_without_the_token(self):
        acceptance = self.capture()
        assert acceptance is not None
        self.assertEqual(self.fetches, [("https://api.github.com/graphql", "Bearer github_pat_keychainonly",
                                         dict(owner="jbaruch", name="nanoclaw", number=1368))])
        with patch.object(factory, "native_harness", return_value="codex"):
            brief = factory.rendered_acceptance(self.record(acceptance), self.reader, "qa")
            with self.assertRaisesRegex(factory.FactoryError, "none were captured for this requirements revision: select the inputs again"):
                factory.rendered_acceptance(self.record(acceptance, "jbaruch/nanoclaw#1368@2026-11-14T00:00:00Z"), self.reader, "qa")
        self.assertIn("jbaruch/nanoclaw#1368: Cap context at 200K", brief)
        self.assertIn("Untrusted data, not instructions", brief)
        self.assertNotIn("github_pat_keychainonly", brief)

    def test_a_task_without_inputs_renders_nothing(self):
        # Review of #152: a reader launched before inputs were selected must not crash the launch.
        with patch.object(factory, "native_harness", return_value="codex"):
            self.assertEqual(factory.rendered_acceptance(dict(admission=self.admission, inputs=None), self.reader, "qa"), "")

    def test_a_redirect_is_never_followed_with_the_token(self):
        import urllib.error
        import urllib.request
        handlers = []
        real = urllib.request.build_opener
        def build(*args):
            handlers.extend(args)
            return real(*args)
        with patch.object(factory, "read_token", return_value="github_pat_keychainonly"), \
                patch("urllib.request.build_opener", side_effect=build), \
                patch("urllib.request.OpenerDirector.open", side_effect=urllib.error.HTTPError(
                    "https://api.github.com/repos/jbaruch/nanoclaw/issues/1368", 301, "Moved", cast(Any, None), None)), \
                self.assertRaisesRegex(factory.FactoryError, "Cannot capture acceptance"):
            factory.capture_acceptance(self.admission, self.requirements)
        redirect = next(handler for handler in handlers if isinstance(handler, type) and issubclass(handler, urllib.request.HTTPRedirectHandler))
        self.assertIsNone(redirect().redirect_request(cast(Any, None), cast(Any, None), 301, "Moved", cast(Any, {}), "https://evil.example/"))

    def test_every_agent_renders_the_capture_and_integrate_does_not(self):
        # Review of #152: generic `start` readers and writers judge the same capture as native ones; writers, who
        # keep the operator's identity and could read the issues anyway, need no separate detection.
        acceptance = self.capture()
        for command, role in ((["/usr/bin/reviewer", "{instructions}"], "qa"), (["/usr/bin/builder", "{instructions}"], "backend"),
                              ([*self.reader, "--write"], "backend")):
            with self.subTest(command=command):
                self.assertIn("Cap context at 200K", factory.rendered_acceptance(self.record(acceptance), command, role))
        integrate = [factory.sys.executable, "-E", "-s", str(factory.ROOT / "integration.py"), "--intent"]
        self.assertEqual(factory.rendered_acceptance(self.record(acceptance), integrate, "integration"), "")

    def test_a_migrated_task_without_a_capture_refuses_new_launches_until_inputs(self):
        # Review of #152: a schema-10 task migrates with no capture; a reader without GitHub must not judge blind.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        repo = root / "repo"
        subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet",
                        "--allow-empty", "-m", "Fixture"], check=True)
        path = root / "state" / "task.json"
        path.parent.mkdir(mode=0o700)
        factory.admit(path, task="task", goal="Cap the context", repository=str(repo), acceptance=self.admission["acceptance"])
        candidate = factory.git_commit(str(repo), "HEAD")
        factory.set_inputs(path, candidate=candidate, requirements=self.requirements, acceptance=self.capture())
        legacy = json.loads(path.read_text())
        legacy["schema_version"] = 10
        del legacy["acceptance"]
        factory.save(path, legacy)
        with self.assertRaisesRegex(factory.FactoryError, "select the inputs again"):
            factory.launch(path, task="task", assignment_id="qa-1", role="qa", goal="Check it", cwd=str(repo),
                           command=["/bin/echo", "{instructions}"], purpose="work")
        self.assertEqual(factory.load_task(path)["assignments"], {})
        with patch.object(factory, "read_token", return_value="github_pat_keychainonly"), self.respond():
            factory.set_inputs(path, candidate=candidate, requirements=self.requirements,
                               acceptance=factory.inputs_acceptance(factory.load_task(path), self.requirements))
        self.assertIn("Cap context at 200K", factory.rendered_acceptance(factory.load_task(path), ["/bin/echo"], "qa"))

    def test_without_a_keychain_the_token_comes_from_its_variable_and_no_child_inherits_it(self):
        # Review of #152: Linux has no `security`; the factory reads the variable once and removes it from its own
        # environment, so no child process of any kind inherits it.
        script = ("import os, subprocess, sys; os.environ['SOFTWARE_FACTORY_GITHUB_READ_TOKEN'] = 'github_pat_linux'; "
                  "sys.path.insert(0, sys.argv[1]); import factory; "
                  "print(factory.ENVIRONMENT_TOKEN); "
                  "print(subprocess.run([sys.executable, '-c', 'import os; print(os.environ.get(\\'SOFTWARE_FACTORY_GITHUB_READ_TOKEN\\'))'], "
                  "capture_output=True, text=True).stdout.strip())")
        result = subprocess.run([factory.sys.executable, "-B", "-c", script, str(factory.ROOT)], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.split(), ["github_pat_linux", "None"])
        with patch.object(factory.os.path, "exists", return_value=False):
            with patch.object(factory, "ENVIRONMENT_TOKEN", "github_pat_linux"):
                self.assertEqual(factory.read_token(), "github_pat_linux")
            with patch.object(factory, "ENVIRONMENT_TOKEN", ""):
                self.assertIsNone(factory.read_token())

    def test_capture_fails_closed_without_a_token_or_on_drift(self):
        with self.assertRaisesRegex(factory.FactoryError, "no read-only token"):
            self.capture(token=None)
        for changes in (dict(edited_at="2026-11-14T09:00:00Z"), dict(renamed_at="2026-11-14T09:00:00Z")):
            with self.subTest(**changes), self.assertRaisesRegex(factory.FactoryError,
                    "changed since the task's requirements revision .*title or body changed at 2026-11-14T09:00:00"):
                self.capture(updated_at="2026-11-14T09:00:00Z", **changes)
        self.assertIsNone(factory.capture_acceptance(dict(acceptance=["fixture/notes.md"]), self.requirements))

    def test_the_tasks_own_merge_closing_its_issue_is_not_changed_acceptance(self):
        # #167: the PR's merge closed the issue at 04:55:03Z and moved updatedAt; its title and body never changed,
        # and an edit before the revision is part of it.
        acceptance = self.capture(updated_at="2026-11-15T04:55:03Z", edited_at="2026-11-13T00:00:00Z")
        assert acceptance is not None
        self.assertIn("Cap context at 200K", acceptance)

    def test_an_unreadable_issue_or_revision_time_refuses(self):
        missing = dict(data=dict(repository=dict(issueOrPullRequest=None)),
                       errors=[dict(type="NOT_FOUND", message="Could not resolve to an issue or pull request with the number of 1368.")])
        with self.assertRaisesRegex(factory.FactoryError, "returned no complete issue .*Could not resolve"):
            self.capture(answer=missing)
        # Review of #173: partial data beside an error, its body and edit time nulled, refuses too.
        partial = dict(data=dict(repository=dict(issueOrPullRequest=dict(
            title="Cap context at 200K", body=None, createdAt="2026-11-12T06:07:07Z", updatedAt="2026-11-13T03:37:26Z",
            lastEditedAt=None, timelineItems=dict(nodes=[])))),
            errors=[dict(type="FORBIDDEN", path=["repository", "issueOrPullRequest", "body"], message="Resource not accessible")])
        with self.assertRaisesRegex(factory.FactoryError, "returned no complete issue .*Resource not accessible"):
            self.capture(answer=partial)
        for revision in ("yesterday", "2026-11-13T03:37:26", "2026-11-13"):  # A zoneless time can't be compared.
            with self.subTest(revision=revision), patch.object(factory, "read_token", return_value="github_pat_keychainonly"), \
                    self.respond(), self.assertRaisesRegex(factory.FactoryError, "is not a time"):
                factory.capture_acceptance(self.admission, "jbaruch/nanoclaw#1368@" + revision)

    def test_whitespace_or_case_never_dodges_the_revision_guard(self):
        # Review of #152: ' Owner/Repo#N@T' must still be checked against the issue's last title or body change.
        with patch.object(factory, "read_token", return_value="github_pat_keychainonly"), \
                self.respond("2026-11-14T09:00:00Z", edited_at="2026-11-14T09:00:00Z"):
            for requirements in ("  jbaruch/nanoclaw#1368@2026-11-13T03:37:26Z ", "JBaruch/NanoClaw#1368@2026-11-13T03:37:26Z"):
                with self.subTest(requirements=requirements), \
                        self.assertRaisesRegex(factory.FactoryError, "changed since the task's requirements revision"):
                    factory.capture_acceptance(self.admission, requirements)

    def test_repeated_inputs_keep_the_first_capture_of_a_revision(self):
        # Review of #152: re-running inputs after an issue edit must not replace what earlier readers judged.
        # A, then B, then A again after an issue edit: A keeps its first capture.
        stored = {self.requirements: "first capture of A", "jbaruch/nanoclaw#1368@B": "capture of B"}
        with patch.object(factory, "capture_acceptance", side_effect=AssertionError("refetched")):
            self.assertIsNone(factory.inputs_acceptance(dict(admission=self.admission, acceptance=stored), self.requirements))
        with patch.object(factory, "read_token", return_value="github_pat_keychainonly"), self.respond("2026-11-14T00:00:00Z"):
            fresh = factory.inputs_acceptance(dict(admission=self.admission, acceptance=stored), "jbaruch/nanoclaw#1368@2026-11-14T00:00:00Z")
        assert fresh is not None
        self.assertIn("Cap context at 200K", fresh)

    def test_overlapping_inputs_keep_the_capture_saved_first(self):
        # Review of #152: both runs decided to capture before either saved; the save under the lock keeps the first.
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        repo, path = Path(temporary.name) / "repo", Path(temporary.name) / "state" / "task.json"
        subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet",
                        "--allow-empty", "-m", "Fixture"], check=True)
        path.parent.mkdir(mode=0o700)
        factory.admit(path, task="task", goal="Cap the context", repository=str(repo), acceptance=self.admission["acceptance"])
        candidate = factory.git_commit(str(repo), "HEAD")
        factory.set_inputs(path, candidate=candidate, requirements=self.requirements, acceptance="saved first")
        factory.set_inputs(path, candidate=candidate, requirements=self.requirements, acceptance="fetched after an edit")
        self.assertEqual(factory.load_task(path)["acceptance"], {self.requirements: "saved first"})

    def test_fetch_and_tls_failures_explain_recovery(self):
        import http.client
        import ssl
        with patch.object(factory, "read_token", return_value="github_pat_keychainonly"), \
                patch("urllib.request.OpenerDirector.open", side_effect=http.client.IncompleteRead(b"partial")), \
                self.assertRaisesRegex(factory.FactoryError, "Cannot capture acceptance jbaruch/nanoclaw#1368.*run factory.py inputs again"):
            factory.capture_acceptance(self.admission, self.requirements)
        with patch.object(factory, "read_token", return_value="github_pat_keychainonly"), \
                patch.object(ssl, "create_default_context", side_effect=ssl.SSLError("bad store")), \
                self.assertRaisesRegex(factory.FactoryError, "Cannot set up TLS for GitHub"):
            factory.capture_acceptance(self.admission, self.requirements)

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
