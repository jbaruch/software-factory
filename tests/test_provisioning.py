"""Owner provisioning conformance with real temporary Git repositories, never providers/Rex."""
import copy
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import dashboard
import factory


class WorktreeProvisioning(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="factory-provision-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.path = self.state / "fixture.json"
        self.destination = self.root / "writer"
        self.git("init", "--quiet", "--initial-branch=main")
        (self.repo / "source.bin").write_bytes(b"raw\0bytes\xff\r\n")
        (self.repo / "tool").write_text("#!/bin/sh\necho fixture\n")
        (self.repo / "tool").chmod(0o755)
        (self.repo / ".gitattributes").write_text("*.bin -text filter=fixture\n")
        (self.repo / "nested").mkdir()
        (self.repo / "nested/source").write_text("nested source\n")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "fixture")
        self.admit()
        factory.set_inputs(self.path, candidate="HEAD", requirements="requirements-v1")

    def git(self, *args):
        return subprocess.check_output([factory.git_binary(), "-C", str(self.repo),
            "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + os.devnull, *args],
            env=factory.git_environment(), text=True).strip()

    def admit(self):
        return factory.admit(self.path, task="fixture", goal="Implement a fixture",
            repository=str(self.repo), acceptance=["requirements.md"], corrections=2, investigations=1)

    def provision(self, **changes):
        with factory.execution_lock(self.state), factory.task_lock(self.state, "fixture"):
            return factory.provision_worktree(self.path,
                **{**dict(worktree_id="backend", destination=str(self.destination)), **changes})

    def cli(self, *args):
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = factory.main(["--state-dir", str(self.state), *args])
        return result, output.getvalue(), errors.getvalue()

    def test_cli_creates_independent_raw_checkout_without_callbacks_or_source_changes(self):
        sentinel = self.root / "callback-ran"
        hook = self.repo / ".git/hooks/post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 91\n")
        hook.chmod(0o755)
        for key in ("filter.fixture.clean", "filter.fixture.smudge", "core.fsmonitor"):
            # fsmonitor is tested separately: unsupported executable monitors refuse pre-effect.
            if key != "core.fsmonitor":
                self.git("config", key, f"touch '{sentinel}'; exit 91")
        self.git("config", "filter.fixture.required", "true")
        self.git("config", "core.autocrlf", "true")
        (self.repo / "source.bin").write_bytes(b"dirty source must survive")
        (self.repo / "untracked").write_text("preserve")
        head = self.git("rev-parse", "HEAD")
        index = (self.repo / ".git/index").read_bytes()
        result, output, errors = self.cli("provision", "--task", "fixture", "--id", "backend",
                                          "--path", str(self.destination))
        self.assertEqual((result, errors), (0, ""))
        view = json.loads(output)
        self.assertEqual(view["provisioning"]["state"], "provisioned")
        self.assertTrue(view["provisioning"]["inputs_current"])
        self.assertEqual(view["allowances"]["correction"]["remaining"], 2)
        self.assertFalse(sentinel.exists())
        self.assertEqual((self.destination / "source.bin").read_bytes(), b"raw\0bytes\xff\r\n")
        self.assertEqual((self.destination / "tool").stat().st_mode & 0o777, 0o755)
        self.assertEqual((self.destination / "source.bin").stat().st_nlink, 1)
        self.assertEqual((self.repo / ".git/index").read_bytes(), index)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual((self.repo / "source.bin").read_bytes(), b"dirty source must survive")
        self.assertEqual((self.repo / "untracked").read_text(), "preserve")
        factory.check_checkout(str(self.repo), str(self.destination), head,
            expected_identity=view["admission"]["repository_identity"])
        snapshot = dashboard.snapshot(self.state)
        self.assertEqual(snapshot["tasks"][0]["provisioning"], view["provisioning"])
        self.assertIn("Writer worktree backend: provisioned", dashboard.render(snapshot))

    def test_intent_precedes_git_and_completed_lost_reply_replays_without_mutation(self):
        original_git, original_save = factory.provisioning_git, factory.save
        observed = []
        def inspect(repository, *args, **kwargs):
            if args[:2] == ("worktree", "add"):
                saved = factory.load_task(self.path)["provisioning"]
                self.assertEqual(saved["state"], "provisioning")
                self.assertEqual(saved["request"]["inputs"]["requirements"], "requirements-v1")
                self.assertFalse(self.destination.exists())
                observed.append(saved["owner_token"])
            return original_git(repository, *args, **kwargs)
        def lost_reply(path, value, **kwargs):
            original_save(path, value, **kwargs)
            if value.get("provisioning", {}).get("state") == "provisioned":
                raise OSError("lost completed response")
        with patch.object(factory, "provisioning_git", side_effect=inspect), \
                patch.object(factory, "save", side_effect=lost_reply), self.assertRaises(OSError):
            self.provision()
        self.assertEqual(len(observed), 1)
        before = self.path.read_bytes()
        # A writer can use the completed checkout; replay must never reset its edits.
        (self.destination / "source.bin").write_bytes(b"writer edit")
        record = self.provision()
        self.assertEqual(record["provisioning"]["owner_token"], observed[0])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual((self.destination / "source.bin").read_bytes(), b"writer edit")
        self.assertEqual(len(self.git("worktree", "list", "--porcelain").split("worktree ")) - 1, 2)

    def test_restart_recovers_clean_effect_when_final_save_was_not_published_even_while_held(self):
        original = factory.save
        def interrupted(path, value, **kwargs):
            if value.get("provisioning", {}).get("state") == "provisioned":
                raise OSError("restart before completion publication")
            original(path, value, **kwargs)
        with patch.object(factory, "save", side_effect=interrupted), self.assertRaises(OSError):
            self.provision()
        self.assertEqual(factory.load_task(self.path)["provisioning"]["state"], "provisioning")
        factory.hold_task(self.path, "Inspection hold")
        with patch.object(factory, "provisioning_git", wraps=factory.provisioning_git) as git:
            record = self.provision()
        self.assertFalse(any(call.args[1:3] == ("worktree", "add") for call in git.call_args_list))
        self.assertEqual(record["provisioning"]["state"], "provisioned")
        self.assertEqual(record["hold"], "Inspection hold")

    def test_lost_git_reply_retains_partial_outcome_without_repeating_or_repairing(self):
        original = factory.provisioning_git
        def lost_reply(repository, *args, **kwargs):
            result = original(repository, *args, **kwargs)
            if args[:2] == ("worktree", "add"):
                raise factory.FactoryError("lost Git reply")
            return result
        with patch.object(factory, "provisioning_git", side_effect=lost_reply), self.assertRaises(factory.FactoryError):
            self.provision()
        before = self.path.read_bytes()
        (self.destination / "inspect-me").write_text("partial outcome")
        with self.assertRaises(factory.FactoryError):
            self.provision()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual((self.destination / "inspect-me").read_text(), "partial outcome")
        self.assertFalse((self.destination / "tool").exists())
        self.assertEqual(self.git("show-ref", "--hash", "refs/heads/factory/fixture/backend"),
                         self.git("rev-parse", "HEAD"))

    def test_pre_effect_save_failure_never_runs_git_and_pending_missing_effect_is_not_retried(self):
        with patch.object(factory, "save", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.provision()
        self.assertFalse(self.destination.exists())
        self.assertIsNone(factory.load_task(self.path)["provisioning"])
        with patch.object(factory, "provisioning_git", wraps=factory.provisioning_git) as git:
            original = factory.save
            def stop_after_intent(*args, **kwargs):
                original(*args, **kwargs)
                raise OSError("reply lost before Git")
            with patch.object(factory, "save", side_effect=stop_after_intent), self.assertRaises(OSError):
                self.provision()
            self.assertFalse(any(call.args[1:3] == ("worktree", "add") for call in git.call_args_list))
        with self.assertRaisesRegex(factory.FactoryError, "outcome uncertain"):
            self.provision()
        self.assertFalse(self.destination.exists())

    def test_collisions_are_never_reused_or_removed(self):
        self.destination.mkdir()
        (self.destination / "unrelated").write_text("keep")
        with self.assertRaisesRegex(factory.FactoryError, "path already exists"):
            self.provision()
        self.assertEqual((self.destination / "unrelated").read_text(), "keep")
        self.git("branch", "factory/fixture/backend")
        with self.assertRaisesRegex(factory.FactoryError, "branch exists"):
            self.provision(destination=str(self.root / "other"))
        self.assertIsNone(factory.load_task(self.path)["provisioning"])

    def test_destination_cannot_overlap_the_task_owner_store(self):
        nested = self.state / "nested"
        nested.mkdir()
        before = self.path.read_bytes()
        for destination in (self.state / "writer", nested / "writer", self.root):
            with self.subTest(destination=destination):
                result, _, errors = self.cli("provision", "--task", "fixture", "--id", "backend",
                                             "--path", str(destination))
                self.assertEqual(result, 1)
                self.assertIn("outside the task owner store", errors)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertFalse((self.state / "writer").exists())
                self.assertFalse((nested / "writer").exists())
        self.assertEqual(self.git("branch", "--list", "factory/fixture/backend"), "")

    def test_hold_input_changes_and_request_changes_preserve_original_intent_and_limits(self):
        factory.hold_task(self.path, "Wait")
        with self.assertRaisesRegex(factory.FactoryError, "paused"):
            self.provision()
        factory.hold_task(self.path, None)
        record = self.provision()
        for changes in (dict(worktree_id="another"), dict(destination=str(self.root / "another"))):
            with self.assertRaisesRegex(factory.FactoryError, "different provisioning request"):
                self.provision(**changes)
        factory.set_inputs(self.path, candidate="HEAD", requirements="requirements-v2")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "different provisioning request"):
            self.provision()
        view = factory.task_view(factory.load_task(self.path))
        self.assertFalse(view["provisioning"]["inputs_current"])
        self.assertEqual(view["allowances"], factory.task_view(record)["allowances"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_repository_marker_change_refuses_replay(self):
        record = self.provision()
        factory.save(self.repo / ".git" / factory.REPOSITORY_MARKER,
                     dict(schema_version=1, repository_id="b" * 32))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "repository pin"):
            self.provision()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(record["provisioning"]["state"], "provisioned")

    def test_selected_candidate_and_consumed_attempt_survive_provisioning_and_input_change(self):
        # Only the transport is fake: launch validates the actual new Git checkout.
        from test_rex_launcher import FakeRex
        FakeRex.sessions, FakeRex.calls = [], []
        FakeRex.fail_after_create, FakeRex.host = False, "test-host"
        selected = factory.load_task(self.path)["inputs"]["candidate"]
        (self.repo / "later").write_text("not selected")
        self.git("add", "later")
        self.git("commit", "--quiet", "-m", "later source")
        record = self.provision()
        self.assertFalse((self.destination / "later").exists())
        self.assertEqual(record["provisioning"]["request"]["inputs"]["candidate"], selected)
        with patch.object(factory, "Rex", FakeRex):
            factory.launch(self.path, task="fixture", assignment_id="backend", role="backend",
                goal="Fix within scope", cwd=str(self.destination), command=[sys.executable, "{instructions}"],
                binary=sys.executable, server="unix:///test.sock", purpose="correction")
        before = factory.task_view(factory.load_task(self.path))["allowances"]
        self.provision()
        after = factory.task_view(factory.load_task(self.path))["allowances"]
        self.assertEqual(before, after)
        self.assertEqual(after["correction"]["remaining"], 1)
        factory.set_inputs(self.path, candidate="HEAD", requirements="requirements-v1")
        with self.assertRaisesRegex(factory.FactoryError, "different provisioning request"):
            self.provision()
        self.assertEqual(factory.task_view(factory.load_task(self.path))["allowances"], before)

    def test_materialization_refuses_a_substituted_parent_without_writing_outside(self):
        outside = self.root / "outside"
        outside.mkdir()
        original = factory.provisioning_git
        def substituted(repository, *args, **kwargs):
            result = original(repository, *args, **kwargs)
            if args[:1] == ("read-tree",):
                (self.destination / "nested").symlink_to(outside, target_is_directory=True)
            return result
        with patch.object(factory, "provisioning_git", side_effect=substituted), self.assertRaises(OSError):
            self.provision()
        self.assertEqual(list(outside.iterdir()), [])
        before = self.path.read_bytes()
        with self.assertRaises(factory.FactoryError):
            self.provision()
        self.assertEqual(self.path.read_bytes(), before)

    def test_ownership_lock_changes_cannot_adopt_an_unrelated_worktree(self):
        record = self.provision()
        locked = Path(record["provisioning"]["git_dir"]) / "locked"
        locked.write_text("another owner\n")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "ownership differs"):
            self.provision()
        self.assertEqual(locked.read_text(), "another owner\n")
        self.assertEqual(self.path.read_bytes(), before)

    def test_unsupported_trees_config_and_nested_paths_refuse_before_intent(self):
        for key, value in (("core.fsmonitor", "/bad/callback"), ("core.sparseCheckout", "true"),
                           ("extensions.worktreeConfig", "true"), ("remote.origin.promisor", "true"),
                           ("core.alternateRefsCommand", "/bad/callback")):
            self.git("config", key, value)
            try:
                with self.subTest(key=key), self.assertRaisesRegex(factory.FactoryError, "Unsupported provisioning"):
                    self.provision()
                self.assertIsNone(factory.load_task(self.path)["provisioning"])
            finally:
                self.git("config", "--unset", key)
        with self.assertRaisesRegex(factory.FactoryError, "outside existing checkouts"):
            self.provision(destination=str(self.repo / "nested"))
        (self.repo / "link").symlink_to("tool")
        self.git("add", "link")
        self.git("commit", "--quiet", "-m", "unsupported symlink")
        factory.set_inputs(self.path, candidate="HEAD", requirements="with-link")
        with self.assertRaisesRegex(factory.FactoryError, "regular-file trees"):
            self.provision()
        self.assertFalse(self.destination.exists())
        self.assertIsNone(factory.load_task(self.path)["provisioning"])

    def test_version_five_migration_preserves_gates_and_rejects_downgrade_or_damage(self):
        factory.hold_task(self.path, "Keep hold")
        original = factory.load_task(self.path)
        legacy = copy.deepcopy(original)
        legacy.update(schema_version=5)
        del legacy["provisioning"]
        factory.save(self.path, legacy)
        upgraded = factory.load_task(self.path)
        self.assertEqual(upgraded, original)
        self.assertEqual(upgraded["schema_version"], 6)
        for broken in ({**upgraded, "schema_version": 5}, {**upgraded, "provisioning": {}},
                       {key: value for key, value in upgraded.items() if key != "provisioning"}):
            factory.save(self.path, broken)
            before = self.path.read_bytes()
            with self.assertRaises(factory.FactoryError):
                factory.load_task(self.path)
            self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
