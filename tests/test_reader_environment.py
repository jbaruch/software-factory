"""Read-only assignments get no GitHub identity: a Codex release judgment merged and deployed."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import errno
import io
import json
import os
import plistlib
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import call, patch

import codex_worker
import factory
import go_cache


class ReaderEnvironment(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = patch.object(factory, "READER_GH_CONFIG", self.root / "gh-reader")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.base = dict(PATH="/usr/bin", HOME=str(self.root), GH_TOKEN="operator-token")

    def test_a_linked_worktree_gets_its_own_go_cache_that_goes_with_it(self):
        # Each worktree wrote its own copy of every Go cache entry into the shared cache, kept five days (#183).
        repo, linked = self.root / "repo", self.root / "linked"
        def git(*args, cwd=repo):
            subprocess.run([factory.git_binary(), "-C", str(cwd), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                            "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                           check=True, capture_output=True, env=factory.git_environment())
        repo.mkdir()
        git("init", "--quiet")
        (repo / "go.mod").write_text("module fixture\n")
        git("add", "go.mod")
        git("commit", "--quiet", "-m", "Fixture")
        git("worktree", "add", "--quiet", "--detach", str(linked))
        self.assertNotIn("GOCACHE", factory.worker_environment(self.base, str(repo)))
        self.assertEqual(factory.worker_environment(self.base, str(repo))["GIT_CONFIG_VALUE_0"], "git@github.com:")
        git("remote", "add", "origin", "git@github.com:jbaruch/fixture.git")
        self.assertEqual(factory.worker_environment(self.base, str(repo))["GIT_CONFIG_VALUE_0"], "git@github.com:jbaruch/fixture")
        cache = Path(factory.worker_environment(self.base, str(linked))["GOCACHE"])
        self.assertEqual(cache, factory.git_dir(str(linked)) / "go-build")
        self.assertEqual(cache.parent.parent, factory.git_common_dir(str(repo)) / "worktrees")
        worker = factory.worker_environment(self.base, str(linked))
        self.assertEqual((worker["GOCACHE"], worker["GH_CONFIG_DIR"]), (str(cache), str(self.root / "gh-reader")))
        if sys.platform == "darwin":
            # Hourly Time Machine backups would otherwise carry the cache to the NAS.
            excluded = subprocess.run(["/usr/bin/tmutil", "isexcluded", str(cache)], capture_output=True, text=True, check=True)
            self.assertTrue(excluded.stdout.startswith("[Excluded]"), excluded.stdout)
        (cache / "00").mkdir(parents=True, exist_ok=True)
        (cache / "00" / "entry-d").write_bytes(b"compiled")
        git("worktree", "remove", "--force", str(linked))
        self.assertFalse(cache.exists())
        # Review of #184: a newline in a checkout's path is still one Git directory.
        odd = self.root / "linked\nname"
        git("worktree", "add", "--quiet", "--detach", str(odd))
        self.assertEqual(Path(factory.worker_environment(self.base, str(odd))["GOCACHE"]),
                         factory.git_dir(str(odd)) / "go-build")

    def test_time_machine_exclusion_falls_back_to_tmutil_and_explains_a_double_failure(self):
        # Review of #184: a volume without extended attributes must not stop every worker without an alternative.
        calls = []
        def run(command, **options):
            calls.append(command[0])
            return subprocess.CompletedProcess(command, 0 if command[0] == "/usr/bin/tmutil" else 1, "", "Operation not supported")
        with patch.object(factory.subprocess, "run", side_effect=run):
            factory.exclude_from_time_machine(self.root)
        self.assertEqual(calls, ["/usr/bin/xattr", "/usr/bin/tmutil"])
        calls.clear()
        with patch.object(factory.subprocess, "run", side_effect=lambda command, **options: (
                calls.append(command[0]), subprocess.CompletedProcess(command, 0, "", ""))[1]):
            factory.exclude_from_time_machine(self.root)
        self.assertEqual(calls, ["/usr/bin/xattr"])
        with patch.object(factory.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 1, "", "Operation not supported"), OSError("tmutil missing")]), \
                self.assertRaisesRegex(factory.FactoryError, r"xattr: Operation not supported; tmutil: tmutil missing\)\. "
                                       "Move the repository"):
            factory.exclude_from_time_machine(self.root)

    def test_workers_get_no_operator_identity_and_pushes_refused(self):
        # Readers lost it after a release judgment merged and deployed; writers need none, since the factory commits
        # their work and the foreman pushes it.
        environment = factory.no_github_identity(self.base, "jbaruch/nanoclaw")
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
        self.assertEqual(pushed.stdout.strip(), factory.PUSH_REFUSED + ".git")
        self.assertEqual(fetched.stdout.strip(), "git@github.com:jbaruch/nanoclaw.git")
        # A test fixture's remote on another GitHub repository resolves as it does in CI (#188).
        subprocess.run(["git", "-C", str(repo), "remote", "add", "fixture", "https://github.com/acme/widgets.git"], check=True)
        fixture = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "--push", "fixture"],
                                 env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(fixture.stdout.strip(), "https://github.com/acme/widgets.git")
        # Without a recognised origin, every GitHub push is refused, as before.
        self.assertEqual(factory.no_github_identity(self.base)["GIT_CONFIG_VALUE_0"], "git@github.com:")

    def test_a_credential_an_earlier_version_left_is_removed(self):
        # 0.39.3 drafts wrote a read-only token into the reader's gh config; workers now hold none.
        config = self.root / "gh-reader"
        config.mkdir()
        (config / "hosts.yml").write_text("github.com:\n    oauth_token: github_pat_left\n")
        factory.no_github_identity(self.base)
        self.assertFalse((config / "hosts.yml").exists())


class AcceptanceEvidence(unittest.TestCase):
    """Readers have no GitHub identity, so `inputs` captures their acceptance issues (0.39.3 regression)."""

    marker = "fixture-marker"
    account_home = "/Users/Fixture User"
    login_keychain = account_home + "/Library/Keychains/login.keychain-db"
    other_keychain = "/Users/Another User/Library/Keychains/login.keychain-db"
    lookup = ["/usr/bin/security", "find-generic-password", "-s", "software-factory-github-read"]
    list_keychains = ["/usr/bin/security", "list-keychains", "-d", "user"]
    security_options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            encoding="utf-8", timeout=30, check=False)
    metadata_options = security_options | {"stdout": subprocess.DEVNULL}

    def setUp(self):
        self.requirements = "jbaruch/nanoclaw#1368@2026-11-13T03:37:26Z"
        self.admission = dict(acceptance=["https://github.com/jbaruch/nanoclaw/issues/1368", "fixture/notes.md"])
        self.reader = [factory.sys.executable, "-E", "-s", str(factory.ROOT / "codex_worker.py"), "--role", "qa"]
        self.fetches = []

    def security_result(self, status, stdout=""):
        return subprocess.CompletedProcess([], status, stdout, self.marker)

    def login_lookup_results(self):
        listing = f'    "{self.other_keychain}"\n    "{self.login_keychain}"\n'
        return [self.security_result(44), self.security_result(0, listing)]

    def respond(self, updated_at="2026-11-13T03:37:26Z", *, edited_at=None, renamed_at=None, answer=None, comment=None):
        """GitHub's GraphQL answer for the acceptance issue, created 2026-11-12 and changed as the arguments say."""
        class Response(io.BytesIO):
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False
        def urlopen(request, timeout):
            if request.data is None:
                self.assertIsNotNone(comment, "only the explicitly selected comment may be fetched")
                self.fetches.append((request.full_url, request.get_header("Authorization"), None))
                return Response(json.dumps(comment).encode())
            self.fetches.append((request.full_url, request.get_header("Authorization"), json.loads(request.data)["variables"]))
            issue = dict(title="Cap context at 200K", body="Acceptance: first call under 200K tokens.",
                         createdAt="2026-11-12T06:07:07Z", updatedAt=updated_at, lastEditedAt=edited_at,
                         timelineItems=dict(nodes=[dict(createdAt=renamed_at)] if renamed_at else []))
            return Response(json.dumps(answer or dict(data=dict(repository=dict(issueOrPullRequest=issue)))).encode())
        return patch("urllib.request.OpenerDirector.open", side_effect=lambda request, timeout: urlopen(request, timeout))

    def capture(self, token: str | None = "github_pat_keychainonly", updated_at="2026-11-13T03:37:26Z", **changes):
        with patch.object(factory, "read_token", return_value=token), self.respond(updated_at, **changes):
            return factory.capture_acceptance(self.admission, self.requirements)

    def record(self, acceptance, requirements=None) -> dict[str, Any]:
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

    def test_selected_owner_decision_is_frozen_in_its_requirements_revision(self):
        permalink = "https://github.com/jbaruch/nanoclaw/issues/1368#issuecomment-7"
        api = "https://api.github.com/repos/jbaruch/nanoclaw/issues"
        comment = dict(id=7, html_url=permalink, issue_url=f"{api}/1368", author_association="OWNER",
                       user=dict(id=12345, login="jbaruch"), updated_at="2026-11-14T00:00:00Z",
                       body="Use 250K tokens for the initial call.")
        prior = self.capture()
        record = self.record(prior)
        original_approval = dict(inputs=dict(record["inputs"]), candidate=record["inputs"]["candidate"], outcome="approved")
        self.assertTrue(factory.result_current(record, original_approval))
        revision = "jbaruch/nanoclaw#1368@2026-11-14T00:00:00Z"
        with patch.object(factory, "read_token", return_value="github_pat_keychainonly"), self.respond(comment=comment):
            with self.assertRaisesRegex(factory.FactoryError, "select a new revision"):
                factory.inputs_acceptance(record, self.requirements, owner_decision=permalink)
            with self.assertRaisesRegex(factory.FactoryError, "changed at"):
                factory.capture_acceptance(self.admission, self.requirements, owner_decision=permalink)
            fresh = factory.inputs_acceptance(record, revision, owner_decision=permalink)
        self.assertEqual(self.fetches[-1], (f"{api}/comments/7", "Bearer github_pat_keychainonly", None))
        assert fresh is not None
        record["acceptance"][revision] = fresh
        record["inputs"]["requirements"] = revision
        self.assertFalse(factory.result_current(record, original_approval))
        with patch.object(factory, "read_token", side_effect=AssertionError("frozen revisions are offline")):
            self.assertIsNone(factory.inputs_acceptance(record, revision, owner_decision=permalink))
            with self.assertRaisesRegex(factory.FactoryError, "select a new revision"):
                factory.inputs_acceptance(record, revision, owner_decision=permalink.removesuffix("7") + "8")
            brief = factory.rendered_acceptance(record, self.reader, "qa")
        for excerpt in (permalink, "jbaruch (GitHub account 12345)", comment["updated_at"], comment["body"], "200K tokens"):
            self.assertIn(excerpt, brief)
        self.assertEqual(record["acceptance"][self.requirements], prior)

    def test_unrelated_or_nonowner_comments_cannot_be_selected(self):
        permalink = "https://github.com/jbaruch/nanoclaw/issues/1368#issuecomment-7"
        api = "https://api.github.com/repos/jbaruch/nanoclaw/issues"
        comment = dict(id=7, html_url=permalink, issue_url=f"{api}/1368", author_association="OWNER",
                       user=dict(id=12345, login="jbaruch"), updated_at="2026-11-13T00:00:00Z", body="Decision.")
        with patch.object(factory, "read_token", side_effect=AssertionError("check admission before network")):
            with self.assertRaisesRegex(factory.FactoryError, "one of this task's admitted issues"):
                factory.capture_acceptance(self.admission, self.requirements, owner_decision=permalink.replace("1368", "1369"))
        for changes in ({"author_association": "MEMBER"}, {"issue_url": f"{api}/1369"}, {"id": 8}):
            with (self.subTest(changes=changes), patch.object(factory, "read_token", return_value="github_pat_keychainonly"),
                  self.respond(comment=comment | changes), self.assertRaisesRegex(factory.FactoryError, "check its permalink, authorship and access")):
                factory.capture_acceptance(self.admission, self.requirements, owner_decision=permalink)

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

    def test_every_agent_renders_the_capture(self):
        # Review of #152: generic `start` readers and writers judge the same capture as native ones; writers, who
        # keep the operator's identity and could read the issues anyway, need no separate detection.
        acceptance = self.capture()
        for command, role in ((["/usr/bin/reviewer", "{instructions}"], "qa"), (["/usr/bin/builder", "{instructions}"], "backend"),
                              ([*self.reader, "--write"], "backend")):
            with self.subTest(command=command):
                self.assertIn("Cap context at 200K", factory.rendered_acceptance(self.record(acceptance), command, role))

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
        with patch.object(factory.sys, "platform", "linux"):
            with patch.object(factory, "ENVIRONMENT_TOKEN", "github_pat_linux"):
                self.assertEqual(factory.read_token(), "github_pat_linux")
            with patch.object(factory, "ENVIRONMENT_TOKEN", ""):
                self.assertIsNone(factory.read_token())

    def test_capture_fails_closed_without_a_token_or_on_drift(self):
        with self.assertRaisesRegex(factory.FactoryError, "(?i)no read-only token"):
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
                       errors=[dict(type="NOT_FOUND", message=self.marker)])
        with self.assertRaisesRegex(factory.FactoryError, "returned no complete issue .*inspect the acceptance reference") as error:
            self.capture(token=self.marker, answer=missing)
        self.assertNotIn(self.marker, str(error.exception))
        # Review of #173: partial data beside an error, its body and edit time nulled, refuses too.
        partial = dict(data=dict(repository=dict(issueOrPullRequest=dict(
            title="Cap context at 200K", body=None, createdAt="2026-11-12T06:07:07Z", updatedAt="2026-11-13T03:37:26Z",
            lastEditedAt=None, timelineItems=dict(nodes=[])))),
            errors=[dict(type="FORBIDDEN", path=["repository", "issueOrPullRequest", "body"], message="Resource not accessible")])
        with self.assertRaisesRegex(factory.FactoryError, "returned no complete issue .*read-only token's access") as error:
            self.capture(answer=partial)
        self.assertNotIn("Resource not accessible", str(error.exception))
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
        import urllib.error
        for failure in (http.client.IncompleteRead(self.marker.encode()), urllib.error.URLError(self.marker)):
            with self.subTest(failure=type(failure).__name__), patch.object(factory, "read_token", return_value=self.marker), \
                    patch("urllib.request.OpenerDirector.open", side_effect=failure), \
                    self.assertRaisesRegex(factory.FactoryError, "Cannot capture acceptance.*run factory.py inputs again") as error:
                factory.capture_acceptance(self.admission, self.requirements)
            self.assertNotIn(self.marker, str(error.exception))
        with patch.object(factory, "read_token", return_value=self.marker), \
                patch.object(ssl, "create_default_context", side_effect=ssl.SSLError(self.marker)), \
                self.assertRaisesRegex(factory.FactoryError, "Cannot set up TLS for GitHub") as error:
            factory.capture_acceptance(self.admission, self.requirements)
        self.assertNotIn(self.marker, str(error.exception))

    def test_working_implicit_keychain_read_captures_without_a_login_lookup(self):
        with self.respond(), patch.object(factory.sys, "platform", "darwin"), \
                patch.object(factory.subprocess, "run", side_effect=[
                    self.security_result(0), self.security_result(0, self.marker + "\n")]) as run:
            capture = factory.capture_acceptance(self.admission, self.requirements)
        assert capture is not None
        self.assertIn("Cap context at 200K", capture)
        self.assertNotIn(self.marker, capture)
        self.assertEqual(self.fetches[0][:2], ("https://api.github.com/graphql", "Bearer " + self.marker))
        self.assertEqual(run.call_args_list, [call(self.lookup, **self.metadata_options),
                                              call([*self.lookup, "-w"], **self.security_options)])

    def test_login_keychain_denial_and_interactive_recovery_on_inputs(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        repo = root / "repo"
        subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "--quiet",
                        "--allow-empty", "-m", "Fixture"], check=True)
        state = root / "state"
        state.mkdir(mode=0o700)
        path = state / "task.json"
        factory.admit(path, task="task", goal="Cap the context", repository=str(repo), acceptance=self.admission["acceptance"])
        candidate = factory.git_commit(str(repo), "HEAD")
        args = ["--state-dir", str(state), "inputs", "--task", "task", "--candidate", candidate,
                "--requirements", self.requirements]
        before = path.read_bytes()
        commands = [call(self.lookup, **self.metadata_options),
                    call(self.list_keychains, **self.security_options),
                    call([*self.lookup, self.login_keychain], **self.metadata_options),
                    call([*self.lookup, "-w", self.login_keychain], **self.security_options)]
        steps = [*self.login_lookup_results(), self.security_result(0, self.marker)]
        denied = self.security_result(36, self.marker)
        results = iter([*steps[:1], denied, *steps[:2], denied, *steps, denied,
                        *steps, self.security_result(0, self.marker + "\n")])
        real_run = subprocess.run
        security_calls = []

        def run(command, *args, **options):
            if command[0] == "/usr/bin/security":
                security_calls.append(call(command, *args, **options))
                return next(results)
            return real_run(command, *args, **options)

        errors, output = io.StringIO(), io.StringIO()
        with self.respond(), patch.object(factory.sys, "platform", "darwin"), \
                patch.object(factory.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=self.account_home)), \
                patch.object(factory.subprocess, "run", side_effect=run), redirect_stderr(errors), redirect_stdout(output):
            recovery = ("requires macOS user interaction. In native macOS Terminal as the same user, rerun the same "
                        "factory.py inputs command with the same state directory, task, candidate and requirements revision, "
                        "and respond to the macOS authorization prompt")
            denials = ["Listing the user Keychains", "Looking up software-factory-github-read metadata in the login Keychain",
                       "Found software-factory-github-read in the login Keychain outside the current Keychain search. "
                       "Reading software-factory-github-read"]
            for command_count, operation in enumerate(denials, start=2):
                with self.subTest(operation=operation):
                    self.assertEqual(factory.main(args), 1)
                    self.assertEqual(security_calls, commands[:command_count])
                    self.assertEqual(self.fetches, [])
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(output.getvalue(), "")
                    self.assertEqual(errors.getvalue(), f"factory: {operation} {recovery}\n")
                security_calls.clear()
                errors.seek(0)
                errors.truncate()
            self.assertEqual(factory.main(args), 0)
            saved = factory.load_task(path)["acceptance"]
            self.assertIn("Cap context at 200K", saved[self.requirements])
            self.assertEqual(factory.main(args), 0)
            self.assertEqual(factory.load_task(path)["acceptance"], saved)
        self.assertEqual(security_calls, commands)
        self.assertEqual(self.fetches, [("https://api.github.com/graphql", "Bearer " + self.marker,
                                         dict(owner="jbaruch", name="nanoclaw", number=1368))])
        self.assertNotIn(self.marker, errors.getvalue() + output.getvalue() + path.read_text())
        self.assertNotIn(self.marker, factory.rendered_acceptance(factory.load_task(path), self.reader, "qa"))
        with patch.dict(os.environ, {factory.TOKEN_VARIABLE: self.marker}), \
                patch.object(factory, "READER_GH_CONFIG", root / "gh-reader"):
            environment = factory.worker_environment(codex_worker.native_environment(), str(repo))
        self.assertNotIn(self.marker, environment.values())

    def test_keychain_failures_do_not_fetch_or_expose_subprocess_output(self):
        missing = self.security_result(44, self.marker)
        login = self.login_lookup_results()
        cases = [
            ([*login, missing], "No read-only token entry .* was found.*In Keychain Access, select login"),
            ([self.security_result(36, self.marker)], "requires macOS user interaction.*native macOS Terminal"),
            ([self.security_result(0), self.security_result(36, self.marker)],
             "requires macOS user interaction.*native macOS Terminal"),
            ([missing, self.security_result(51)], "Cannot locate the user login Keychain .*status 51"),
            ([*login, self.security_result(51, self.marker)], "Cannot check .* in the login Keychain .*status 51"),
            ([*login, self.security_result(0), missing], "Found .* outside the current Keychain search.*Cannot read .*status 44"),
            ([self.security_result(51, self.marker)], "Cannot read .*status 51"),
            ([self.security_result(0), self.security_result(0, self.marker + " invalid\n")], "must hold one read-only GitHub token"),
            ([self.security_result(0), self.security_result(0)], "must hold one read-only GitHub token"),
            ([missing, subprocess.TimeoutExpired("security", 30, output=self.marker)],
             "did not answer within 30 seconds during user Keychain list lookup"),
            ([OSError(self.marker)], "Cannot run the Keychain implicit item lookup.*check /usr/bin/security"),
            ([UnicodeDecodeError("utf-8", b"\xff", 0, 1, self.marker)], "malformed output during implicit item lookup"),
        ]
        for results, message in cases:
            with self.subTest(message=message), self.respond(), patch.object(factory.sys, "platform", "darwin"), \
                    patch.object(factory.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=self.account_home)), \
                    patch.object(factory, "ENVIRONMENT_TOKEN", "unused-environment-marker"), \
                    patch.object(factory.subprocess, "run", side_effect=results) as run, \
                    self.assertRaisesRegex(factory.FactoryError, message) as error:
                factory.capture_acceptance(self.admission, self.requirements)
            self.assertEqual(run.call_count, len(results))
            self.assertNotIn(self.marker, str(error.exception))
            self.assertNotIn(self.marker, repr(run.call_args_list))
        self.assertEqual(self.fetches, [])

    def test_user_list_must_confirm_the_exact_standard_login_path_before_any_password_read(self):
        for listing in ("", f'    "{self.other_keychain}"\n', f'"{self.login_keychain[:-3]}"\n',
                        '"Library/Keychains/login.keychain-db"\n', f'{self.login_keychain}\n',
                        f'"{self.login_keychain}\n', f'"{self.login_keychain}\x00"\n',
                        f'"{self.login_keychain}" "{self.other_keychain}"\n'):
            with self.subTest(listing=listing), self.respond(), patch.object(factory.sys, "platform", "darwin"), \
                    patch.object(factory.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=self.account_home)), \
                    patch.object(factory.subprocess, "run", side_effect=[
                        self.security_result(44), self.security_result(0, listing)]) as run, \
                    self.assertRaisesRegex(factory.FactoryError, "did not identify the standard login Keychain.*"
                                           "Keychain Access as the same user") as error:
                factory.capture_acceptance(self.admission, self.requirements)
            self.assertEqual(run.call_args_list, [call(self.lookup, **self.metadata_options),
                                                  call(self.list_keychains, **self.security_options)])
            self.assertNotIn(listing.strip() or self.marker, str(error.exception))
        self.assertEqual(self.fetches, [])


if __name__ == "__main__":
    unittest.main()


class AppleGitShim(unittest.TestCase):
    """Apple's /usr/bin/git runs xcrun on every call, and xcodebuild inside the Codex sandbox (nanoclaw, load 172)."""

    def setUp(self):
        factory.git_binary.cache_clear()
        self.addCleanup(factory.git_binary.cache_clear)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.real = Path(directory.name) / "git"
        self.real.write_text("#!/bin/sh\n")
        self.real.chmod(0o755)

    def resolve(self, found):
        from unittest.mock import patch
        with patch.object(factory.sys, "platform", "darwin"), \
                patch.object(factory.shutil, "which", return_value="/usr/bin/git"), \
                patch.object(factory.subprocess, "run", return_value=found) as run:
            return factory.git_binary(), run

    def test_the_shim_resolves_once_to_the_real_git(self):
        binary, run = self.resolve(subprocess.CompletedProcess([], 0, stdout=str(self.real) + "\n", stderr=""))
        self.assertEqual(binary, str(self.real))
        self.assertEqual(run.call_args.args[0], ["/usr/bin/xcrun", "--find", "git"])
        self.assertEqual(factory.git_binary(), str(self.real))  # Cached: no second xcrun.
        self.assertEqual(run.call_count, 1)

    def test_cold_git_discovery_shares_the_report_deadline(self):
        now = 0
        budget = factory.ReportHashBudget(1, 10, 5, clock=lambda: now)
        now = 4.75
        with budget.git_reads():
            _, run = self.resolve(subprocess.CompletedProcess([], 0, stdout=str(self.real), stderr=""))
        self.assertEqual(run.call_args.kwargs["timeout"], 0.25)

    def test_an_unresolvable_shim_refuses(self):
        for found in (subprocess.CompletedProcess([], 1, stdout="", stderr="xcrun: error"),
                      subprocess.CompletedProcess([], 0, stdout="relative/git\n", stderr=""),
                      subprocess.CompletedProcess([], 0, stdout=str(self.real) + ".missing\n", stderr="")):
            factory.git_binary.cache_clear()
            with self.subTest(found=found.stdout), self.assertRaisesRegex(factory.FactoryError, "xcrun --find git"):
                self.resolve(found)


class ManagedScratch(unittest.TestCase):
    """Each native attempt's TMPDIR is its own and goes when the attempt does (local-dev, 84 GB of temp)."""

    def test_scratch_is_short_private_and_removed_with_read_only_leftovers(self):
        receipt = {}
        with tempfile.TemporaryDirectory() as state, factory.scratch_directory(Path(state) / "receipt.json", receipt) as scratch:
            self.assertEqual(scratch.stat().st_mode & 0o777, 0o700)
            outside = Path(state) / "outside"
            outside.mkdir(mode=0o500)
            (scratch / "outside-link").symlink_to(outside, target_is_directory=True)
            nested = scratch / "go-build123" / "pkg"
            nested.mkdir(parents=True)
            (nested / "a.o").write_text("object")
            os.chmod(nested / "a.o", 0o400)
            os.chmod(nested, 0o500)
            with tempfile.TemporaryDirectory() as repository:
                subprocess.run([factory.git_binary(), "init", "-q", repository], check=True)
                environment = factory.worker_environment({"PATH": os.defpath}, repository, scratch=scratch)
            self.assertEqual(environment["TMPDIR"], str(scratch))
            self.assertEqual(outside.stat().st_mode & 0o777, 0o500)
        self.assertFalse(scratch.exists())
        self.assertIs(receipt["scratch_removed"], True)

    def test_unverified_identity_leaves_scratch_intact(self):
        with tempfile.TemporaryDirectory() as state:
            scratch = Path(state) / "sf-fixture"
            scratch.mkdir(mode=0o700)
            marker = scratch / "marker"
            marker.write_text("keep")
            with ExitStack() as handles, redirect_stderr(io.StringIO()) as errors:
                fd = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY)
                handles.callback(os.close, fd)
                self.assertIs(factory.remove_scratch(scratch, None, fd), False)
            self.assertIn(f"scratch {scratch} was not fully removed", errors.getvalue())
            self.assertEqual(marker.read_text(), "keep")

    def test_cleanup_repairs_held_root_and_openable_child(self):
        with tempfile.TemporaryDirectory() as state, patch.object(factory, "SCRATCH_ROOT", state):
            receipt = {}
            with factory.scratch_directory(Path(state) / "receipt.json", receipt) as scratch:
                nested = scratch / "nested"
                nested.mkdir()
                (nested / "payload").write_text("owned")
                nested.chmod(0o500)
                scratch.chmod(0)
            self.assertIs(receipt["scratch_removed"], True)
            self.assertFalse(scratch.exists())

    @unittest.skipUnless(sys.platform == "linux" and os.getuid() != 0, "Requires unprivileged Linux directory repair")
    def test_cleanup_removes_unreadable_directory(self):
        with tempfile.TemporaryDirectory() as state, patch.object(factory, "SCRATCH_ROOT", state):
            receipt = {}
            with factory.scratch_directory(Path(state) / "receipt.json", receipt) as scratch:
                nested = scratch / "nested"
                nested.mkdir()
                (nested / "payload").write_text("owned")
                nested.chmod(0)
            self.assertIs(receipt["scratch_removed"], True)
            self.assertFalse(scratch.exists())

    @unittest.skipUnless(sys.platform == "linux" and os.getuid() != 0, "Requires unprivileged Linux directory repair")
    def test_descriptor_repair_preserves_substituted_symlink_target(self):
        with tempfile.TemporaryDirectory() as state, patch.object(factory, "SCRATCH_ROOT", state):
            root = Path(state)
            outside = root / "outside"
            outside.mkdir()
            (outside / "payload").write_bytes(b"keep")
            outside.chmod(0o555)
            chmod = os.chmod
            receipt = {}
            with ExitStack() as effects, redirect_stderr(io.StringIO()):
                with factory.scratch_directory(root / "receipt.json", receipt) as scratch:
                    nested = scratch / "nested"
                    nested.mkdir(mode=0)

                    def replace_before_chmod(path, mode):
                        nested.rename(scratch / "moved")
                        nested.symlink_to(outside, target_is_directory=True)
                        chmod(path, mode)

                    effects.enter_context(patch.object(os, "chmod", side_effect=replace_before_chmod))
            self.assertIs(receipt["scratch_removed"], False)
            self.assertTrue(nested.is_symlink())
            self.assertEqual(outside.stat().st_mode & 0o777, 0o555)
            self.assertEqual((outside / "payload").read_bytes(), b"keep")
            outside.chmod(0o700)

    @unittest.skipUnless(sys.platform == "darwin" and os.getuid() != 0, "Requires unprivileged Darwin directory repair")
    def test_permission_repair_refuses_substituted_links(self):
        open_directory = os.open
        for symlink in (True, False):
            with self.subTest(symlink=symlink), tempfile.TemporaryDirectory() as state:
                root = Path(state)
                outside = root / "outside-directory"
                outside.mkdir(mode=0o700)
                marker = outside / "marker"
                marker.write_text("keep")
                marker.chmod(0o400)
                outside.chmod(0o500)
                receipt = {}
                with patch.object(factory, "SCRATCH_ROOT", state), ExitStack() as effects, redirect_stderr(io.StringIO()):
                    with factory.scratch_directory(root / "receipt.json", receipt) as scratch:
                        nested = scratch / "nested"
                        nested.mkdir(mode=0)
                        scratch.chmod(0)  # Force the descriptor repair pass before the child open.

                        def replace_before_open(path, flags, *args, **kwargs):
                            if path == nested.name:
                                nested.rmdir()
                                if symlink:
                                    nested.symlink_to(outside, target_is_directory=True)
                                else:
                                    os.link(marker, nested)
                            return open_directory(path, flags, *args, **kwargs)

                        effects.enter_context(patch.object(os, "open", side_effect=replace_before_open))
                self.assertIs(receipt["scratch_removed"], False)
                self.assertTrue(os.path.samestat(nested.stat(), (outside if symlink else marker).stat()))
                self.assertEqual(outside.stat().st_mode & 0o777, 0o500)
                self.assertEqual(marker.stat().st_mode & 0o777, 0o400)
                self.assertEqual(marker.read_text(), "keep")

    @unittest.skipUnless(sys.platform == "darwin" and os.getuid() != 0, "Requires unprivileged Darwin directory repair")
    def test_unopenable_child_survives_until_owner_restores_access(self):
        for mode in (0, 0o100):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as state, patch.object(factory, "SCRATCH_ROOT", state):
                receipt = {}
                with ExitStack() as handles, redirect_stderr(io.StringIO()) as errors:
                    with factory.scratch_directory(Path(state) / "receipt.json", receipt) as scratch:
                        fd = os.open(scratch, os.O_DIRECTORY)
                        handles.callback(os.close, fd)
                        nested = scratch / "nested"
                        nested.mkdir()
                        payload = nested / "payload"
                        payload.write_bytes(b"owned")
                        nested.chmod(mode)
                        original = nested.stat()
                    self.assertIs(receipt["scratch_removed"], False)
                    self.assertIn(f"scratch {scratch} was not fully removed", errors.getvalue())
                    self.assertTrue(os.path.samestat(nested.stat(), original))
                    self.assertEqual(nested.stat().st_mode, original.st_mode)
                    nested.chmod(0o700)
                    self.assertEqual(payload.read_bytes(), b"owned")
                    self.assertIs(factory.remove_scratch(scratch, receipt["scratch_identity"], fd), True)
                    self.assertFalse(scratch.exists())

    @unittest.skipUnless(sys.platform == "darwin" and os.getuid() != 0, "Requires unprivileged Darwin HFS+")
    def test_physical_mount_stays_at_original_name(self):
        for moment in ("before cleanup", "denied open", "nested claim", "fixture failure"):
            with self.subTest(moment=moment), ExitStack() as cleanup:
                root = Path(tempfile.mkdtemp())
                image = root / "fixture.dmg"
                cleanup.enter_context(patch.object(factory, "SCRATCH_ROOT", str(root)))
                scratch = None

                def devices():
                    inventory = subprocess.run(["hdiutil", "info", "-plist"], check=True, capture_output=True)
                    return [disk["system-entities"][0]["dev-entry"]
                            for disk in plistlib.loads(inventory.stdout)["images"]
                            if Path(disk["image-path"]).resolve() == image.resolve()]

                def detach():
                    for device in devices():
                        subprocess.run(["hdiutil", "detach", device], check=True, capture_output=True)
                    self.assertFalse(devices(), f"Inspect attached fixture image {image}")

                def dispose():
                    # An inventory or detach failure stops here, preserving the image for inspection.
                    detach()
                    if scratch is not None and scratch.exists():
                        scratch.chmod(0o700)
                        if (scratch / "nested").exists():
                            (scratch / "nested").rmdir()
                        scratch.rmdir()
                    image.unlink(missing_ok=True)
                    root.rmdir()

                cleanup.callback(dispose)
                scratch = factory.new_scratch()
                child = scratch / "nested"
                subprocess.run(["hdiutil", "create", "-quiet", "-size", "16m", "-fs", "HFS+", "-volname",
                                "scratch-test", image], check=True, capture_output=True)
                child.mkdir(mode=0 if moment == "denied open" else 0o700)
                original = child.stat()
                fd = os.open(scratch, os.O_DIRECTORY)
                cleanup.callback(os.close, fd)
                info = os.fstat(fd)
                identity = dict(st_dev=info.st_dev, st_ino=info.st_ino)
                mounted = None
                marker = child / "payload"
                open_directory = os.open
                rename = factory.rename_scratch_exclusive

                def attach():
                    nonlocal mounted
                    subprocess.run(["hdiutil", "attach", "-owners", "on", "-nobrowse", "-mountpoint", child,
                                    image], check=True, capture_output=True)
                    self.assertTrue(child.is_mount())
                    child.chmod(0o755)
                    marker.write_bytes(b"preserve")
                    marker.chmod(0o444)
                    mounted = child.stat()

                def open_child(path, flags, *args, **kwargs):
                    try:
                        return open_directory(path, flags, *args, **kwargs)
                    except PermissionError:
                        if path == child.name and moment == "denied open":
                            attach()
                        raise

                def claim(path, destination, src_fd, dst_fd):
                    if path == child.name and moment == "nested claim":
                        attach()
                    return rename(path, destination, src_fd, dst_fd)

                if moment == "fixture failure":
                    with self.assertRaisesRegex(ValueError, "fixture setup"), cleanup.pop_all():
                        attach()
                        raise ValueError("fixture setup")
                    self.assertFalse(root.exists())
                    continue
                if moment == "before cleanup":
                    attach()
                elif moment == "denied open":
                    scratch.chmod(0)
                with patch.object(os, "open", side_effect=open_child), patch.object(factory, "rename_scratch_exclusive", side_effect=claim), \
                        redirect_stderr(io.StringIO()):
                    self.assertIs(factory.remove_scratch(scratch, identity, fd), False)
                self.assertTrue(child.is_mount())
                assert mounted is not None
                self.assertTrue(os.path.samestat(child.stat(), mounted))
                self.assertEqual(child.stat().st_mode & 0o777, 0o755)
                self.assertEqual(marker.stat().st_mode & 0o777, 0o444)
                self.assertEqual(marker.read_bytes(), b"preserve")
                detach()
                self.assertTrue(os.path.samestat(child.stat(), original))
                self.assertEqual(child.stat().st_mode, original.st_mode)
                child.chmod(0o700)
                self.assertIs(factory.remove_scratch(scratch, identity, fd), True)
                self.assertFalse(scratch.exists())

    def test_cleanup_checks_child_mount_before_listing_or_repair(self):
        identify = go_cache.mount_id
        for mode in (0o700, 0):
            if mode == 0 and os.getuid() == 0:
                continue
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as state:
                root = Path(state)
                receipt = {}
                with patch.object(factory, "SCRATCH_ROOT", state), redirect_stderr(io.StringIO()), ExitStack() as effects:
                    with factory.scratch_directory(root / "receipt.json", receipt) as scratch:
                        child = scratch / "nested"
                        child.mkdir()
                        marker = child / "marker"
                        marker.write_text("keep")
                        child.chmod(mode)
                        child_info = child.stat()

                        def mount(fd):
                            if os.path.samestat(child_info, os.fstat(fd)):
                                return "other mount"
                            return identify(fd)

                        list_directory = os.listdir

                        def list_children(fd):
                            self.assertFalse(os.path.samestat(child_info, os.fstat(fd)))
                            return list_directory(fd)

                        effects.enter_context(patch.object(go_cache, "mount_id", side_effect=mount))
                        effects.enter_context(patch.object(os, "listdir", side_effect=list_children))
                self.assertIs(receipt["scratch_removed"], False)
                self.assertEqual(child.stat().st_mode & 0o777, mode)
                child.chmod(0o700)
                self.assertEqual(marker.read_text(), "keep")

    def test_cleanup_and_permission_retry_stay_with_the_recorded_directory(self):
        open_directory = os.open
        list_directory = os.listdir
        for moment in ("during removal", "before permission repair", "after permission repair"):
            with self.subTest(moment=moment), tempfile.TemporaryDirectory() as state:
                root = Path(state)
                receipt = {}
                with patch.object(factory, "SCRATCH_ROOT", state), redirect_stderr(io.StringIO()), ExitStack() as effects:
                    with factory.scratch_directory(root / "receipt.json", receipt) as scratch:
                        (scratch / "nested").mkdir()

                        def replace():
                            scratch.rename(root / "original")
                            scratch.mkdir(mode=0o700)
                            (scratch / "nested").mkdir()
                            (scratch / "nested/marker").write_text("keep")
                            (scratch / "nested").chmod(0o500)

                        def open_child(name, flags, *args, **kwargs):
                            if name == "nested" and moment == "during removal":
                                replace()
                            return open_directory(name, flags, *args, **kwargs)

                        calls = 0

                        def list_children(descriptor):
                            nonlocal calls
                            calls += 1
                            if calls == 1 and moment != "during removal":
                                if moment == "before permission repair":
                                    replace()
                                raise PermissionError("fixture requires repair")
                            if calls == 2 and moment == "after permission repair":
                                replace()
                            return list_directory(descriptor)

                        effects.enter_context(patch.object(os, "open", side_effect=open_child))
                        effects.enter_context(patch.object(os, "listdir", side_effect=list_children))
                self.assertIs(receipt["scratch_removed"], False)
                self.assertEqual((scratch / "nested/marker").read_text(), "keep")
                self.assertEqual((scratch / "nested").stat().st_mode & 0o777, 0o500)
                (scratch / "nested").chmod(0o700)

    def test_exact_leaf_cleanup(self):
        with tempfile.TemporaryDirectory() as state, patch.object(factory, "SCRATCH_ROOT", state):
            root = Path(state)
            receipt = {}
            missing = root / "missing"
            with factory.scratch_directory(root / "receipt.json", receipt) as scratch:
                (scratch / "dangling").symlink_to(missing)
                (scratch / "unreadable").touch(mode=0)
                os.mkfifo(scratch / "fifo")
                with socket.socket(socket.AF_UNIX) as endpoint:
                    endpoint.bind(str(scratch / "socket"))
            self.assertIs(receipt["scratch_removed"], True)
            self.assertFalse(scratch.exists())
            self.assertFalse(missing.exists())
            self.assertFalse(list(root.glob(".sf-cleanup-*")))

    def test_denied_leaf_pin_retains_entry(self):
        with tempfile.TemporaryDirectory() as state, patch.object(factory, "SCRATCH_ROOT", state):
            receipt = {}
            with ExitStack() as effects, redirect_stderr(io.StringIO()):
                with factory.scratch_directory(Path(state) / "receipt.json", receipt) as scratch:
                    leaf = scratch / "entry"
                    leaf.write_bytes(b"keep")
                    original = leaf.stat()
                    effects.enter_context(patch.object(os, "link", side_effect=PermissionError(errno.EPERM, "denied")))
            self.assertIs(receipt["scratch_removed"], False)
            self.assertTrue(os.path.samestat(original, leaf.stat()))
            self.assertEqual(leaf.read_bytes(), b"keep")
            self.assertFalse(list(Path(state).glob(".sf-cleanup-*")))

    def test_private_claim_preserves_replacements_and_occupants(self):
        rename = factory.rename_scratch_exclusive
        for kind in ("root", "directory", "file"):
            for occupied in (False, True):
                with self.subTest(kind=kind, occupied=occupied), tempfile.TemporaryDirectory() as state:
                    root = Path(state)
                    receipt = {}
                    replacements = []
                    with patch.object(factory, "SCRATCH_ROOT", state), ExitStack() as effects:
                        errors = effects.enter_context(redirect_stderr(io.StringIO()))
                        with factory.scratch_directory(root / "receipt.json", receipt) as scratch:
                            target = scratch if kind == "root" else scratch / "entry"
                            if kind == "directory":
                                target.mkdir()
                            elif kind == "file":
                                target.write_bytes(b"owned")
                            original = target.stat()

                            if kind == "file":
                                real_stat = os.stat

                                def discard_last_public_link(path, *args, **kwargs):
                                    info = real_stat(path, *args, **kwargs)
                                    if not replacements and os.path.samestat(info, original):
                                        target.unlink()
                                        target.write_bytes(b"foreign")
                                        replacements.append(real_stat(target))
                                        self.assertEqual(info.st_nlink, 2, "first observation must already be pinned")
                                    return info

                                effects.enter_context(patch.object(os, "stat", side_effect=discard_last_public_link))

                            def swap_before_claim(name, destination, src_fd, dst_fd):
                                if name == target.name:
                                    if kind == "file":
                                        pin = os.stat("pin", dir_fd=dst_fd, follow_symlinks=False)
                                        self.assertTrue(os.path.samestat(pin, original))
                                        self.assertEqual(pin.st_nlink, 1)
                                    else:
                                        target.rename(root / "original")
                                        target.mkdir()
                                        replacements.append(target.stat())
                                rename(name, destination, src_fd, dst_fd)
                                if name == target.name and occupied:
                                    target.mkdir()
                                    replacements.append(target.stat())

                            effects.enter_context(patch.object(factory, "rename_scratch_exclusive",
                                                               side_effect=swap_before_claim))
                    self.assertIs(receipt["scratch_removed"], False)
                    if kind != "file":
                        self.assertTrue(os.path.samestat((root / "original").stat(), original))
                    self.assertTrue(os.path.samestat(target.stat(), replacements[-1]))
                    retained = target
                    if occupied:
                        holder, = root.glob(".sf-cleanup-*")
                        retained = holder / "claimed"
                        self.assertTrue(os.path.samestat(retained.stat(), replacements[0]))
                        self.assertIn(str(retained), errors.getvalue())
                    else:
                        self.assertFalse(list(root.glob(".sf-cleanup-*")))
                    if kind == "file":
                        self.assertEqual(retained.read_bytes(), b"foreign")
