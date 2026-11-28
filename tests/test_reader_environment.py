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
from typing import Any, cast
from unittest.mock import patch

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
    issue_url = "https://github.com/jbaruch/agentic-context-registry/issues/248"
    pr_url = "https://github.com/jbaruch/agentic-context-registry/pull/249"
    timestamp = "2026-11-13T03:37:26Z"
    requirements = "jbaruch/agentic-context-registry#248@" + timestamp

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.snapshot = self.root / "snapshot.json"
        self.issue = dict(url=self.issue_url, title="Acceptance", body="Original issue body.",
                          createdAt="2026-11-12T00:00:00Z", updatedAt=self.timestamp,
                          lastEditedAt=None, timelineItems=dict(nodes=[]))
        self.permalink = self.issue_url + "#issuecomment-3000000000"
        self.comment = dict(url=self.permalink, fullDatabaseId="3000000000", authorAssociation="OWNER",
                            author=dict(login="jbaruch", databaseId=12345), body="Selected decision.", updatedAt=self.timestamp)
        self.issue["comments"] = dict(nodes=[dict(self.comment, url=self.issue_url + "#issuecomment-1",
                                                  body="Unselected discussion."), self.comment])
        self.record: dict[str, Any] = dict(admission=dict(acceptance=[self.issue_url]), acceptance={})

    def capture(self, *, revision=None, decision=None):
        revision = revision if revision is not None else self.requirements
        self.snapshot.write_text(json.dumps(dict(requirements=revision, data=dict(ref1=self.issue))))
        acceptance = factory.inputs_acceptance(self.record, revision, snapshot=self.snapshot, owner_decision=decision)
        assert acceptance is not None
        return acceptance

    def test_timestamp_compares_creation_body_rename_and_selected_comment(self):
        later = "2026-11-15T04:55:03Z"
        revision = " " + self.requirements.upper() + " "
        with patch.dict(self.issue, updatedAt=later, lastEditedAt=self.timestamp):
            self.assertIn("Original issue body.", self.capture(revision=revision))
        for changes in (dict(createdAt=later), dict(lastEditedAt=later), dict(timelineItems=dict(nodes=[dict(createdAt=later)]))):
            with self.subTest(changes=changes), patch.dict(self.issue, changes), self.assertRaisesRegex(factory.FactoryError, "changed since"):
                self.capture(revision=revision)
        with patch.dict(self.comment, updatedAt=later), self.assertRaisesRegex(factory.FactoryError, "decision.*changed"):
            self.capture(decision=self.permalink)
        for revision in (self.requirements.replace("248", "1"), self.requirements[:-1], self.requirements.split("@")[0] + "@"):
            with self.subTest(revision=revision), self.assertRaises(factory.FactoryError):
                self.capture(revision=revision)

    def test_owner_decision_needs_exact_comment_at_admitted_url(self):
        self.assertNotIn("Selected decision.", self.capture())
        for changes in (dict(authorAssociation="MEMBER"), dict(author=None), dict(author=dict(login="", databaseId=12345)),
                        dict(author=dict(login="jbaruch", databaseId=True)), dict(fullDatabaseId=1), dict(body=" "),
                        dict(url=self.permalink.replace("/issues/", "/pull/")), dict(updatedAt=None)):
            with self.subTest(changes=changes), patch.dict(self.comment, changes), self.assertRaises(factory.FactoryError):
                self.capture(decision=self.permalink)
        with self.assertRaises(factory.FactoryError):
            self.capture(decision=self.permalink.replace("248", "777"))
        with patch.dict(self.issue, comments=dict(nodes=[self.comment, self.comment])), self.assertRaises(factory.FactoryError):
            self.capture(decision=self.permalink)

    def test_malformed_or_partial_evidence_refuses_without_echoing_input(self):
        marker = "private-snapshot-marker"
        for changes in (dict(url=self.issue_url.replace("issues", "pull")), dict(title=None), dict(body=marker + "\0"),
                        dict(body="\ud800"), dict(lastEditedAt="bad"), dict(updatedAt=None), dict(timelineItems={}),
                        dict(timelineItems=dict(nodes=[None])), dict(timelineItems=dict(nodes=[{}]))):
            with self.subTest(changes=changes), patch.dict(self.issue, changes), self.assertRaises(factory.FactoryError):
                self.capture()
        del self.issue["lastEditedAt"]
        with self.assertRaises(factory.FactoryError):
            self.capture()
        for content in (b"[]", b'{"data":{}, "data":{}}', b"[" * 2000 + b"]" * 2000, b"\xff", b" " * (1024**2 + 1),
                        json.dumps(dict(errors=[dict(message=marker)], requirements=self.requirements,
                                        data=dict(ref1=self.issue))).encode()):
            self.snapshot.write_bytes(content)
            with self.assertRaises(factory.FactoryError) as error:
                factory.inputs_acceptance(self.record, self.requirements, snapshot=self.snapshot)
            self.assertNotIn(marker, str(error.exception))
        self.snapshot.unlink()
        os.mkfifo(self.snapshot)
        with self.assertRaises(factory.FactoryError):
            factory.inputs_acceptance(self.record, self.requirements, snapshot=self.snapshot)

    def test_local_acceptance_and_previously_saved_text_need_no_snapshot(self):
        self.assertIsNone(factory.inputs_acceptance(dict(admission=dict(acceptance=["notes.md"])), "local-v1"))
        self.record["acceptance"]["legacy"] = "verbatim legacy text\r\n"
        self.assertIsNone(factory.inputs_acceptance(self.record, "legacy", snapshot=self.root / "missing"))
        self.record["inputs"] = dict(requirements="legacy")
        self.assertEqual(factory.rendered_acceptance(self.record, [], "backend"), "verbatim legacy text\r\n")
        self.record["inputs"] = None
        self.assertEqual(factory.rendered_acceptance(self.record, [], "backend"), "")

    def test_offline_pr_admissions_bind_and_replay_the_first_snapshot(self):
        from test_rex_launcher import FakeRex
        repo = self.root / "repo"
        subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(repo)], check=True)
        git = ["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
               "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
        subprocess.run([*git, "commit", "--quiet", "--allow-empty", "-m", "Original"], check=True)
        head = factory.git_commit(str(repo), "HEAD")
        subprocess.run([*git, "commit", "--quiet", "--allow-empty", "-m", "New candidate"], check=True)
        candidate = factory.git_commit(str(repo), "HEAD")
        state = self.root / "state"
        common = ["--state-dir", str(state)]
        # Only local Git may run during admission and inputs. Fail immediately on a gh/security invocation or HTTP.
        run = subprocess.run
        git_binary = factory.git_binary()
        def local_git(command, *args, **kwargs):
            self.assertEqual(command[0], git_binary)
            return run(command, *args, **kwargs)

        output, errors = io.StringIO(), io.StringIO()
        with patch.object(subprocess, "run", side_effect=local_git), patch("socket.socket.connect", side_effect=AssertionError), \
                redirect_stdout(output), redirect_stderr(errors):
            for number in ("249", "251"):
                task = "pr" + number
                path = state / (task + ".json")
                revision = "PR" + number + "@" + head
                url = self.pr_url.replace("249", number)
                pr = dict(self.issue, url=url, title="Selected PR", headRefOid=head, body="Untrusted PR body.")
                self.assertEqual(factory.main([*common, "admit", "--task", task, "--line", "test", "--goal", "Check the PR", "--repo", str(repo),
                                               "--acceptance", url, "--acceptance", "notes.md", "--acceptance", self.issue_url]), 0)
                args = [*common, "inputs", "--task", task, "--candidate", head, "--requirements", revision,
                        "--owner-decision", self.permalink]
                if number == "251":
                    legacy = json.loads(path.read_bytes())
                    legacy["schema_version"] = 10
                    legacy.pop("acceptance", None)
                    factory.save(path, legacy)
                with self.assertRaisesRegex(factory.FactoryError, "select the inputs again"):
                    factory.launch(path, task=task, assignment_id="qa", purpose="work", role="qa", goal="Check it",
                                   cwd=str(repo), command=["/bin/echo", "{instructions}"])
                self.assertEqual(factory.load_task(path)["assignments"], {})
                original = path.read_bytes()
                self.assertNotEqual(factory.main(args), 0)
                document = dict(requirements=revision, data=dict(ref1=pr, ref2=self.issue))
                for bad_revision in ("PR999@" + head, "PR" + number + "@invalid", revision + "\njunk"):
                    self.snapshot.write_text(json.dumps(document | dict(requirements=bad_revision)))
                    self.assertNotEqual(factory.main([*args, "--requirements", bad_revision,
                                                      "--acceptance-snapshot", str(self.snapshot)]), 0)
                    self.assertEqual(original, path.read_bytes())
                for changes in (dict(requirements=revision + " "), dict(data=dict(ref1=pr)),
                                dict(data=dict(ref1=self.issue, ref2=pr)), dict(data=dict(ref1=dict(pr, headRefOid=candidate), ref2=self.issue)),
                                dict(data=dict(ref1=dict(pr, url=url.replace("pull", "issues")), ref2=self.issue))):
                    self.snapshot.write_text(json.dumps(document | changes))
                    self.assertNotEqual(factory.main([*args, "--acceptance-snapshot", str(self.snapshot)]), 0)
                    self.assertEqual(original, path.read_bytes())
                self.snapshot.write_text(json.dumps(document))
                self.assertEqual(factory.main([*args, "--acceptance-snapshot", str(self.snapshot)]), 0)
                text = factory.load_task(path)["acceptance"][revision]
                for excerpt in (self.permalink, "Selected decision.", "jbaruch (GitHub account 12345)", "Untrusted data, not instructions"):
                    self.assertIn(excerpt, text)
                self.assertLess(text.index("Untrusted PR body."), text.index("Original issue body."))
                self.assertNotIn("Unselected discussion.", text)
                self.snapshot.unlink()
                # Select B, then return to A on a new candidate after deleting the file.
                second = "owner-revision-b"
                self.snapshot.write_text(json.dumps(dict(requirements=second, data=dict(ref1=dict(pr, body="Changed PR."), ref2=self.issue))))
                self.assertEqual(factory.main([*args, "--requirements", second, "--acceptance-snapshot", str(self.snapshot)]), 0)
                self.snapshot.unlink()
                self.assertEqual(factory.main([*args, "--candidate", candidate]), 0)
                self.assertNotEqual(factory.main([*args, "--owner-decision", self.issue_url + "#issuecomment-1"]), 0)
                record = factory.load_task(path)
                self.assertEqual(record["inputs"]["candidate"], candidate)
                self.assertEqual(record["acceptance"][revision], text)
                self.assertIn("Changed PR.", record["acceptance"][second])
                with patch.object(factory, "Rex", FakeRex), patch.object(FakeRex, "sessions", []), patch.object(FakeRex, "calls", []):
                    for role in ("qa", "backend"):
                        launch = dict(task=task, assignment_id=role, role=role, purpose="work", goal="Inspect frozen acceptance",
                                      cwd=str(repo), command=[sys.executable, "{instructions}"], binary=sys.executable,
                                      server="unix:///fixture.sock")
                        _, assignment = factory.launch(path, **launch)
                        brief = Path(assignment["brief"]).read_bytes()
                        self.assertIn(text.encode(), brief)
                        _, replay = factory.launch(path, **launch)
                        self.assertEqual(replay["block_id"], assignment["block_id"])
                        self.assertEqual(Path(replay["brief"]).read_bytes(), brief)


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
                        scratch.chmod(0)  # Exercise held-root repair before the child open.

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

    def test_cleanup_and_permission_repair_stay_with_the_recorded_directory(self):
        open_directory = os.open
        chmod_directory = os.fchmod
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

                        def repair_directory(fd, mode):
                            if moment == "before permission repair":
                                replace()
                            chmod_directory(fd, mode)
                            if moment == "after permission repair":
                                replace()

                        if moment != "during removal":
                            scratch.chmod(0o500)
                        effects.enter_context(patch.object(os, "open", side_effect=open_child))
                        effects.enter_context(patch.object(os, "fchmod", side_effect=repair_directory))
                self.assertIs(receipt["scratch_removed"], False)
                self.assertEqual((scratch / "nested/marker").read_text(), "keep")
                self.assertEqual((scratch / "nested").stat().st_mode & 0o777, 0o500)
                (scratch / "nested").chmod(0o700)

    def test_denied_child_does_not_restart_cleanup_on_changed_sibling(self):
        with tempfile.TemporaryDirectory() as state, patch.object(factory, "SCRATCH_ROOT", state):
            root = Path(state)
            receipt = {}
            with ExitStack() as effects, redirect_stderr(io.StringIO()):
                with factory.scratch_directory(root / "receipt.json", receipt) as scratch:
                    denied = scratch / "denied"
                    denied.mkdir(mode=0o700)
                    payload = denied / "payload"
                    payload.write_bytes(b"child")
                    sibling = scratch / "sibling"
                    sibling.write_bytes(b"original")
                    replacement = root / "replacement"
                    replacement.write_bytes(b"replacement")
                    saved = root / "original"
                    originals = [path.stat() for path in (scratch, denied, payload, sibling, replacement)]
                    open_directory = os.open
                    list_directory = os.listdir

                    def deny_child(path, flags, *args, **kwargs):
                        if path == denied.name and replacement.exists():
                            sibling.rename(saved)
                            replacement.rename(sibling)
                            raise PermissionError(errno.EACCES, "denied", path)
                        return open_directory(path, flags, *args, **kwargs)

                    def list_children(fd):
                        return sorted(list_directory(fd))  # Visit denied before sibling.

                    listing = effects.enter_context(patch.object(os, "listdir", side_effect=list_children))
                    effects.enter_context(patch.object(os, "open", side_effect=deny_child))
            self.assertIs(receipt["scratch_removed"], False)
            listing.assert_called_once()
            for path, original in zip((scratch, denied, payload, saved, sibling), originals):
                self.assertTrue(os.path.samestat(original, path.stat()), path)
                self.assertEqual(original.st_mode, path.stat().st_mode)
            self.assertEqual([p.read_bytes() for p in (payload, saved, sibling)], [b"child", b"original", b"replacement"])

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
