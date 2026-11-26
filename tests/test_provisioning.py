"""Owner provisioning conformance with real temporary Git repositories, never providers/Rex."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import copy
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
import errno
import fcntl
from io import StringIO
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import go_cache
import dashboard
import factory


class ProvisionedRepository(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="factory-provision-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "source"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.state.mkdir()
        self.path = self.state / "fixture.json"
        self.release_intent = self.path.with_suffix(".worktree-release-intent.json")
        self.release_receipt = self.path.with_suffix(".worktree-release.json")
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

    @contextmanager
    def release_locks(self, task="fixture"):
        with factory._task_lock(self.state, task) as (_, task_fd):
            yield (task_fd,)


class WorktreeProvisioning(ProvisionedRepository):
    def filename_candidate(self, names):
        blob = subprocess.check_output([factory.git_binary(), "-C", str(self.repo),
            "hash-object", "-w", "--stdin"], input=b"filename fixture\n").decode().strip()
        self.git("read-tree", "--empty")
        for name in names:
            self.git("-c", "core.ignorecase=false", "-c", "core.precomposeunicode=false",
                     "update-index", "--add", "--cacheinfo",
                     "100644," + blob + "," + name)
        candidate = self.git("commit-tree", self.git("write-tree"), "-m", "Filename fixture")
        raw = subprocess.check_output([factory.git_binary(), "-C", str(self.repo),
            "ls-tree", "-r", "-z", "--name-only", candidate])
        self.assertEqual(set(raw.split(b"\0")) - {b""}, {name.encode() for name in names})
        factory.set_inputs(self.path, candidate=candidate, requirements="requirements-v1")
        return candidate

    def test_release_removes_the_provisioned_worktree_and_keeps_the_branch(self):
        # Observed (#78): after delivery the release skill could not remove the locked worktree.
        self.provision()
        admin = factory.git_dir(self.destination)
        with patch.object(sys, "stdin", StringIO("fixture\n")) as terminal, patch.object(terminal, "isatty", return_value=True), \
                patch.object(go_cache, "process_check", return_value={}):
            result, output, errors = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(result, 0, errors)
        self.assertIn("independent exclusion", errors)
        released = json.loads(output)
        self.assertEqual((released["worktree"], released["branch"]), (str(self.destination), "factory/fixture/backend"))
        self.assertFalse(self.destination.exists())
        self.assertFalse(admin.exists())
        self.assertNotIn(str(self.destination), self.git("worktree", "list", "--porcelain"))
        self.assertEqual(self.git("rev-parse", "factory/fixture/backend"), released["head"])
        # A retry after a lost reply finds nothing left to remove (acr-226), and says so instead of refusing.
        result, output, errors = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(result, 0, errors)
        again = json.loads(output)
        self.assertEqual((again["already_released"], again["head"], again["worktree"]), (True, None, str(self.destination)))

    def test_fresh_release_refuses_changed_receipt_bytes(self):
        self.provision()
        release_proof = factory.release_proof

        def append_whitespace(path, record):
            with path.open("ab") as stream:
                stream.write(b" ")
            return release_proof(path, record)

        with patch.object(go_cache, "confirm_window"), patch.object(go_cache, "process_check", return_value={}), \
                patch.object(factory, "release_proof", side_effect=append_whitespace):
            code, _, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 1)
        self.assertIn("Release receipt readback differs", error)

        original = self.release_receipt.read_bytes()
        self.assertTrue(original.endswith(b"\n "))
        code, output, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(output)["already_released"])
        self.assertEqual(self.release_receipt.read_bytes(), original)

    def fail_owner_directory_sync(self, phase=None):
        sync = os.fsync
        owner_directory = self.state.stat()

        def fault(fd):
            if os.path.samestat(os.fstat(fd), owner_directory) and (
                    phase is None or phase == "released" and self.release_receipt.exists()
                    or phase == "absent" and json.loads(self.release_intent.read_bytes())["phase"] == phase):
                raise OSError(errno.EIO, "owner directory fsync")
            sync(fd)

        return patch.object(os, "fsync", fault)

    def retry_terminal_sync(self, phase):
        receipt = self.release_intent if phase == "absent" else self.release_receipt
        with self.fail_owner_directory_sync(phase):
            code, _, error = self.cli("release-worktree", "--task", "fixture")
            self.assertEqual(code, 1)
            self.assertIn("owner directory fsync", error)
            visible = receipt.read_bytes()
            if phase == "absent":
                self.assertEqual(json.loads(visible)["phase"], phase)
                self.assertFalse(self.release_receipt.exists())
            else:
                self.assertEqual(json.loads(visible)["schema_version"], 2)
        with patch.object(factory, "provisioning_git", wraps=factory.provisioning_git) as git:
            with self.fail_owner_directory_sync(phase):
                code, _, error = self.cli("release-worktree", "--task", "fixture")
                self.assertEqual(code, 1)
                self.assertIn("owner directory fsync", error)
                self.assertEqual(receipt.read_bytes(), visible)
            code, output, error = self.cli("release-worktree", "--task", "fixture")
            self.assertEqual(code, 0, error)
            self.assertFalse(any(call.args[1:3] == ("worktree", "remove") for call in git.call_args_list))
        self.assertEqual(receipt.read_bytes(), visible)
        return json.loads(output)

    def test_release_retries_directory_sync_before_removing_checkout(self):
        self.provision()
        admin = factory.git_dir(self.destination)
        with patch.object(go_cache, "confirm_window"), patch.object(go_cache, "process_check", return_value={}):
            with self.fail_owner_directory_sync():
                for _ in range(2):
                    code, _, error = self.cli("release-worktree", "--task", "fixture")
                    self.assertEqual(code, 1)
                    self.assertIn("owner directory fsync", error)
                    intent = json.loads(self.release_intent.read_bytes())
                    self.assertEqual(intent["phase"], "removing")
                    self.assertTrue(self.destination.is_dir())
                    self.assertTrue(admin.is_dir())
                    self.assertEqual(self.git("rev-parse", intent["branch"]), intent["checkout"]["head"])
            again = self.retry_terminal_sync("released")
        self.assertTrue(again["already_released"])
        self.assertFalse(self.destination.exists())
        self.assertFalse(admin.exists())
        released = json.loads(self.release_receipt.read_bytes())
        self.assertEqual(released["checkout"], intent["checkout"])
        self.assertEqual(released["branch"], intent["branch"])
        self.assertNotIn("phase", released)
        self.assertEqual(self.git("rev-parse", intent["branch"]), intent["checkout"]["head"])

    def test_release_git_keeps_task_and_admin_locks_after_parent_sigkill(self):
        line = "release-fixture"
        result, _, errors = self.cli("line", "--task", "fixture", "--line", line)
        self.assertEqual(result, 0, errors)
        result, _, errors = self.cli("provision", "--task", "fixture", "--id", "backend", "--path", str(self.destination))
        self.assertEqual(result, 0, errors)
        admin = factory.git_dir(self.destination)
        other = self.state / "other.json"
        factory.admit(other, task="other", line=line, goal="Independent writer", repository=str(self.repo),
                      acceptance=["requirements.md"])
        factory.set_inputs(other, candidate="HEAD", requirements="requirements-v1")
        fifo = self.root / "git-trace"
        os.mkfifo(fifo)
        # Park the real Git executable at its trace2 FIFO open, before it can remove
        # the checkout. Instrument only the child boundary and the attended fixture.
        release = """
import subprocess, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import factory, go_cache
popen = subprocess.Popen
def trace_remove(argv, **options):
    removing = argv[-5:-3] == ["worktree", "remove"]
    if removing:
        options["env"] = {**options["env"], "GIT_TRACE2_EVENT": sys.argv[3]}
    child = popen(argv, **options)
    if removing:
        print(child.pid, flush=True)
    return child
with patch.object(subprocess, "Popen", trace_remove), patch.object(go_cache, "process_check", return_value={}), \\
        patch.object(go_cache, "confirm_window"):
    sys.exit(factory.main(["--state-dir", sys.argv[2], "release-worktree", "--task", "fixture"]))
"""
        with ExitStack() as stack:
            # Open the admin inode separately before Git can unlink it.
            locks = {}
            for name, path in (("line", factory.execution_lock_path(self.state, line)),
                               ("task", self.state / "fixture.lock"), ("admin", admin)):
                fd = os.open(path, os.O_RDONLY)
                stack.callback(os.close, fd)
                locks[name] = fd

            def busy_locks():
                busy = set()
                for name, fd in locks.items():
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        busy.add(name)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                return busy

            self.assertEqual(busy_locks(), set())
            parent = stack.enter_context(subprocess.Popen(
                [sys.executable, "-B", "-c", release, str(Path(factory.__file__).parent), str(self.state), str(fifo)],
                stdout=subprocess.PIPE, text=True, start_new_session=True))
            try:
                assert parent.stdout is not None
                self.assertTrue(select.select([parent.stdout], [], [], 15)[0], "Release never launched Git removal")
                pid = int(parent.stdout.readline())
                self.assertEqual(busy_locks(), {"task", "admin"})
                result, _, errors = self.cli("provision", "--task", "other", "--id", "backend",
                                             "--path", str(self.root / "other-writer"))
                self.assertEqual(result, 0, errors)
                self.assertTrue((self.root / "other-writer/.git").is_file())
                parent.kill()
                self.assertEqual(parent.wait(timeout=5), -signal.SIGKILL)
                os.kill(pid, 0)
                self.assertTrue(self.destination.exists())
                self.assertEqual(busy_locks(), {"task", "admin"})
                result, _, errors = self.cli("rebase-worktree", "--task", "fixture", "--onto", "HEAD")
                self.assertEqual(result, 75, errors)
                with self.assertRaises(factory.ExecutionBusy), go_cache.inode_lock(admin, shared=True):
                    self.fail("Shared claimant acquired the releasing admin directory")
                result, _, errors = self.cli("provision", "--task", "other", "--id", "backend",
                                             "--path", str(self.root / "other-writer"))
                self.assertEqual(result, 0, errors)
                self.assertTrue((self.root / "other-writer/.git").is_file())
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)
                # Open the reader even if an assertion fails, so Git can finish.
                trace = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
                stack.callback(os.close, trace)
                events = b""
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if select.select([trace], [], [], 0.02)[0]:
                        events += os.read(trace, 65536)
                    if b'"event":"atexit"' in events and not busy_locks():
                        break

            self.assertIn(b'"event":"atexit"', events)
            self.assertEqual(busy_locks(), set())
            self.assertFalse(self.destination.exists())
            self.assertFalse(admin.exists())
            intent = json.loads(self.release_intent.read_bytes())
            self.assertEqual(intent["phase"], "removing")

        owner = self.path.read_bytes()
        for _ in range(2):
            result, output, errors = self.cli("release-worktree", "--task", "fixture")
            self.assertEqual(result, 0, errors)
            self.assertEqual(json.loads(output)["phase"], "absent")
            self.assertEqual(json.loads(output)["intent_id"], factory.load_task(self.path)["provisioning"]["owner_token"])
            self.assertFalse(json.loads(output)["already_released"])
            self.assertEqual(json.loads(self.release_intent.read_bytes()), dict(intent, phase="absent"))
            with self.assertRaisesRegex(factory.FactoryError, "Missing original owner release"):
                factory.release_proof(self.release_receipt, factory.load_task(self.path))
        self.assertEqual(self.path.read_bytes(), owner)
        self.assertEqual(self.git("rev-parse", intent["branch"]), intent["checkout"]["head"])

    def check_recovery_during_provision(self, store, line):
        owner_line = "release-fixture"
        code, _, error = self.cli("line", "--task", "fixture", "--line", owner_line)
        self.assertEqual(code, 0, error)
        code, _, error = self.cli("provision", "--task", "fixture", "--id", "backend", "--path", str(self.destination))
        self.assertEqual(code, 0, error)
        owner = self.path.read_bytes()
        publish = factory.publish_record

        def stop_before_receipt(path, **options):
            if path == self.release_receipt:
                raise InterruptedError("fixture stopped after Git exit")
            return publish(path, **options)

        with patch.object(factory, "publish_record", stop_before_receipt), patch.object(go_cache, "confirm_window"), \
                patch.object(go_cache, "process_check", return_value={}):
            code, _, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 1)
        self.assertIn("fixture stopped after Git exit", error)
        removing = self.release_intent.read_bytes()
        with factory.execution_lock(self.state, owner_line):
            code, _, error = self.cli("release-worktree", "--task", "fixture")
            self.assertEqual(code, 75, error)
            self.assertEqual(self.release_intent.read_bytes(), removing)

        store.mkdir(exist_ok=True)
        other = store / "other.json"
        factory.admit(other, task="other", line=line, goal="Reuse the vacant path", repository=str(self.repo),
                      acceptance=["requirements.md"])
        factory.set_inputs(other, candidate="HEAD", requirements="requirements-v1")
        provision = [sys.executable, "-B", factory.__file__, "--state-dir", str(store), "provision",
                     "--task", "other", "--id", "writer", "--path", str(self.destination)]
        binding = go_cache.checkout_binding
        shared_line = store == self.state and line == owner_line
        attempts = []

        def provision_at_final_check(*args):
            if json.loads(self.release_intent.read_bytes())["phase"] == "absent":
                child = subprocess.run(provision, capture_output=True, timeout=30)
                attempts.append(child.returncode)
                self.assertEqual(child.returncode, 75 if shared_line else 0, child.stderr)
            return binding(*args)

        with patch.object(go_cache, "checkout_binding", provision_at_final_check):
            code, output, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(len(attempts), 1)
        if shared_line:
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)["phase"], "absent")
            subprocess.run(provision, capture_output=True, timeout=30, check=True)
        else:
            self.assertEqual(code, 1, output)
            self.assertIn("Release readback changed", error)
        replacement = other.read_bytes()
        branch = self.git("-C", str(self.destination), "symbolic-ref", "HEAD")
        self.assertEqual(branch, "refs/heads/factory/other/writer")
        code, output, _ = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 1, output)
        self.assertEqual(self.git("-C", str(self.destination), "symbolic-ref", "HEAD"), branch)
        self.assertEqual(other.read_bytes(), replacement)
        self.assertEqual(self.path.read_bytes(), owner)
        self.assertFalse(self.release_receipt.exists())

    def test_absent_readback_excludes_same_line_provision_until_final_check(self):
        self.check_recovery_during_provision(self.state, "release-fixture")

    def test_absent_readback_refuses_replacement_from_another_line(self):
        self.check_recovery_during_provision(self.state, "other-line")

    def test_absent_readback_refuses_replacement_from_another_store(self):
        self.check_recovery_during_provision(self.root / "other-store", "release-fixture")

    def test_release_preserves_shared_user_files_on_refusal(self):
        self.provision()
        source = self.destination / "source.bin"
        sibling = self.root / "user-file"
        os.link(source, sibling)
        original = source.read_bytes()
        with patch.object(go_cache, "process_check", return_value={}), patch.object(go_cache, "confirm_window"):
            code, _, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 1)
        self.assertIn("shared source file", error)
        self.assertTrue(source.samefile(sibling))
        self.assertEqual(sibling.read_bytes(), original)
        self.assertFalse(self.path.with_suffix(".worktree-release.json").exists())

    def test_release_requires_independently_excluded_checkout_users(self):
        self.provision()
        # The inventory is clear, but a direct user can enter after it returns.
        with patch.object(go_cache, "process_check", return_value={}), patch.object(sys, "stdin", StringIO("fixture\n")):
            code, _, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 1)
        self.assertIn("attended exclusive maintenance window", error)
        self.assertTrue((self.destination / "source.bin").is_file())
        self.assertFalse(self.path.with_suffix(".worktree-release.json").exists())

    def test_invalid_recorded_identity_never_enters_attended_legacy_release(self):
        record = self.provision()
        for identity in (None, [], [[0, 0]] * 4):
            with self.subTest(identity=identity):
                record["provisioning"]["checkout_inodes"] = identity
                factory.save(self.path, record)
                raw = self.path.read_bytes()
                with patch.object(go_cache, "confirm_window") as confirm:
                    code, _, error = self.cli("release-worktree", "--task", "fixture")
                self.assertEqual(code, 1)
                self.assertIn("changed original provisioning identity", error)
                confirm.assert_not_called()
                self.assertTrue(self.destination.is_dir())
                self.assertFalse(self.path.with_suffix(".worktree-release.json").exists())
                self.assertEqual(self.path.read_bytes(), raw)

    def test_legacy_release_requires_matching_registration_after_confirmation(self):
        record = self.provision()
        del record["provisioning"]["checkout_inodes"]
        factory.save(self.path, record)
        raw = self.path.read_bytes()
        git = factory.provisioning_git
        with patch.object(go_cache, "confirm_window") as confirm:
            def inventory_after_confirmation(repo, *args, **kwargs):
                if confirm.called and args == ("worktree", "list", "--porcelain", "-z"):
                    return f"worktree {self.repo}\0\0"
                return git(repo, *args, **kwargs)

            with patch.object(factory, "provisioning_git", side_effect=inventory_after_confirmation), \
                    patch.object(go_cache, "process_check", return_value={}):
                code, _, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 1)
        self.assertIn("current legacy owner registration", error)
        confirm.assert_called_once_with("fixture")
        self.assertTrue(self.destination.is_dir())
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertFalse(self.path.with_suffix(".worktree-release.json").exists())

    def interrupt_release_publication(self):
        receipt = self.path.with_suffix(".worktree-release.json")
        # Reconstruct an interrupted publication from before release intent existed.
        with patch.object(go_cache, "confirm_window"), patch.object(go_cache, "process_check", return_value={}):
            code, _, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 0, error)
        self.release_intent.unlink()
        alias = receipt.parent / json.loads(receipt.read_bytes())["publication_alias"]
        os.link(receipt, alias)
        payload = receipt.read_bytes()
        self.assertTrue(alias.samefile(receipt))
        self.assertEqual(receipt.stat().st_nlink, 2)
        code, _, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 1)
        self.assertIn(f"Interrupted private release publication at {receipt}", error)
        self.assertIn(str(alias), error)
        return receipt, alias, payload

    def test_release_readback_names_only_the_original_private_publication_alias(self):
        self.provision()
        receipt, alias, payload = self.interrupt_release_publication()
        other = alias.with_name(".saving-unrelated")
        alias.rename(other)
        code, _, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 1)
        self.assertNotIn("Remove only", error)
        self.assertEqual(other.read_bytes(), payload)
        other.rename(alias)
        alias.unlink()
        code, output, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(output)["already_released"])
        self.assertEqual(receipt.read_bytes(), payload)

    def test_attended_legacy_release_reads_back_only_its_published_observation(self):
        record = self.provision()
        original_owner = copy.deepcopy(record)
        del record["provisioning"]["checkout_inodes"]
        record["schema_version"] = 10
        record.pop("acceptance", None)
        factory.save(self.path, record)
        raw = self.path.read_bytes()
        with patch.object(go_cache, "process_check", return_value={}), \
                patch.object(go_cache, "confirm_window", side_effect=factory.FactoryError("fixture declined")):
            code, _, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 1)
        self.assertIn("fixture declined", error)
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertTrue(self.destination.is_dir())
        receipt, alias, payload = self.interrupt_release_publication()
        proof = json.loads(payload)
        self.assertEqual(proof["schema_version"], 3)
        self.assertIn("observed_checkout", proof)
        self.assertNotIn("checkout", proof)
        alias.unlink()
        code, output, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(output)["already_released"])
        self.assertEqual(receipt.read_bytes(), payload)
        self.assertEqual(self.path.read_bytes(), raw)
        with self.assertRaisesRegex(factory.FactoryError, "original owner release outcome"):
            factory.release_proof(receipt, original_owner)
        receipt.write_text(json.dumps(dict(proof, schema_version=2)))
        with self.assertRaisesRegex(factory.FactoryError, "original owner release outcome"):
            factory.release_proof(receipt, record)

    def test_legacy_receipt_readback_requires_a_single_published_name(self):
        record = self.provision()
        item = record["provisioning"]
        witness = go_cache.checkout_binding(self.destination, Path(item["git_dir"]), record["admission"]["repository_identity"])
        assert witness is not None
        receipt = dict(schema_version=1, at=1000, checkout=witness, head=witness["head"],
                       task=record["task"], worktree=str(self.destination), git_dir=item["git_dir"],
                       owner_token=item["owner_token"], branch=item["request"]["branch"], already_released=False)
        path = self.path.with_suffix(".worktree-release.json")
        with patch.object(Path, "unlink", side_effect=OSError("fixture publication interrupted")), self.assertRaises(OSError):
            factory.save(path, receipt, exclusive=True)
        original = path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "Preserve both names") as error:
            factory.release_proof(path, record)
        self.assertNotIn("Remove", str(error.exception))
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_nlink, 2)
        # An ordinary completed legacy publication carries the same payload.
        completed = self.root / path.name
        factory.save(completed, receipt, exclusive=True)
        self.assertEqual(factory.release_proof(completed, record), (receipt, original))
        self.git("worktree", "remove", "--force", "--force", str(self.destination))
        next(self.state.glob(".saving-*")).unlink()
        code, output, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(output)["already_released"])
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(self.release_intent.exists())

    def test_native_reader_uses_launch_identity_at_rex_entry(self):
        from test_rex_launcher import FakeRex
        import checkout_exec
        from contextlib import chdir

        self.provision()
        case = self
        class EntryRex(FakeRex):
            def call(self, *args, **kwargs):
                if args[0] == "new":
                    assignment = factory.load_task(case.path)["assignments"]["qa"]
                    witness = assignment["launch_checkout"]
                    case.assertEqual(assignment["state"], "launching")
                    case.assertEqual(witness["inodes"], factory.load_task(case.path)["provisioning"]["checkout_inodes"])
                    offset = args.index(str(factory.ROOT / "checkout_exec.py")) + 1
                    # This direct fixture observes the physical checkout checks. The runtime digest
                    # is exercised with the real isolated Python flags in the launcher tests.
                    if args[offset] == "--runtime-digest":
                        offset += 2
                    with chdir(case.destination / "nested"), patch.object(os, "execve") as execute:
                        check = factory.check_checkout
                        with patch.object(go_cache, "checkout_binding", wraps=go_cache.checkout_binding) as observe:
                            def normalize(*args, **kwargs):
                                case.assertEqual(observe.call_count, 1)
                                return check(*args, **kwargs)
                            with patch.object(factory, "check_checkout", side_effect=normalize):
                                checkout_exec.main(args[offset:])
                            case.assertEqual(observe.call_count, 2)
                        execute.assert_called_once()
                        execute.reset_mock()
                        with patch.object(go_cache, "checkout_binding", return_value=None), \
                                patch.object(factory, "check_checkout") as normalize_check:
                            case.assertEqual(checkout_exec.main(args[offset:]), 1)
                            normalize_check.assert_not_called()
                        with patch.object(go_cache, "checkout_binding", side_effect=[witness, None]):
                            case.assertEqual(checkout_exec.main(args[offset:]), 1)
                        execute.assert_not_called()
                return super().call(*args, **kwargs)

        command = factory.codex_command(self.path, task="fixture", assignment_id="qa", codex=sys.executable,
                                        model="fixture", effort="high", cwd=str(self.destination / "nested"), role="qa")
        with patch.object(factory, "Rex", EntryRex), patch.object(EntryRex, "sessions", []), patch.object(EntryRex, "calls", []):
            record, assignment = factory.launch(self.path, task="fixture", assignment_id="qa", role="qa", goal="Inspect",
                purpose="work", cwd=str(self.destination / "nested"), command=command, binary=sys.executable, server="unix:///fixture")
        self.assertEqual(assignment["request"]["command"], command)
        self.assertEqual(assignment["request"]["inputs"], record["inputs"])

    def test_existing_release_receipt_preserves_surviving_checkout(self):
        self.provision()
        path = self.path.with_suffix(".worktree-release.json")
        factory.save(path, "original receipt")
        with self.release_locks() as lock_fds, \
                self.assertRaisesRegex(factory.FactoryError, "existing owner release receipt"):
            factory.release_worktree(self.path, lock_fds=lock_fds)
        self.assertEqual(path.read_text(), "original receipt")
        self.assertIn(str(self.destination), self.git("worktree", "list", "--porcelain"))
        self.assertTrue(self.destination.is_dir())

    def test_missing_checkout_identifies_exact_registration_without_repair(self):
        target = self.root / "missing writer"
        admin = self.repo / ".git/worktrees/writer"
        inventory = f"worktree {self.repo}\0\0worktree {target}\0\0worktree {self.root}/sibling\0\0"
        with patch.object(factory, "provisioning_git", return_value=inventory) as git, self.assertRaises(
                factory.FactoryError) as error:
            go_cache.checkout_binding(target, admin, factory.repository_identity(self.repo))
        git.assert_called_once_with(self.repo, "worktree", "list", "--porcelain", "-z")
        self.assertIn(str(target), str(error.exception))
        self.assertIn(str(admin), str(error.exception))
        self.assertNotIn("prune", str(error.exception))
        self.assertNotIn("unlock", str(error.exception))

    def test_rebase_moves_the_worktree_onto_a_new_base_and_keeps_the_change_as_a_patch(self):
        # Owner decision: refresh a candidate whose base moved by replaying its change (patch rebase).
        self.provision()
        def worktree_git(*args):
            return subprocess.check_output([factory.git_binary(), "-C", str(self.destination), "-c", "user.name=Fixture",
                "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                "-c", "core.hooksPath=" + os.devnull, *args], env=factory.git_environment(), text=True).strip()
        (self.destination / "change.txt").write_text("the task's change\n")
        worktree_git("add", "change.txt")
        worktree_git("commit", "--quiet", "-m", "Task change")
        changed = worktree_git("rev-parse", "HEAD")
        (self.repo / "main.txt").write_text("main moved on\n")
        self.git("add", "main.txt")
        self.git("commit", "--quiet", "-m", "Main moves")
        moved = self.git("rev-parse", "HEAD")
        result, output, errors = self.cli("rebase-worktree", "--task", "fixture", "--onto", moved)
        self.assertEqual((result, errors), (0, ""))
        rebased = json.loads(output)
        self.assertEqual((rebased["previous"], rebased["candidate"]), (changed, moved))
        patch = Path(rebased["patch"])
        self.assertIn("+the task's change", patch.read_text())
        self.assertNotIn("main moved on", patch.read_text())
        self.assertEqual(patch.stat().st_mode & 0o777, 0o600)
        self.assertEqual(worktree_git("rev-parse", "HEAD"), moved)
        self.assertEqual(factory.load_task(self.path)["inputs"]["candidate"], moved)
        factory.check_checkout(str(self.repo), str(self.destination), moved)
        worktree_git("apply", "--3way", "--check", str(patch))
        # A rebase that moved the worktree but failed before selecting it resumes on retry.
        factory.set_inputs(self.path, candidate=changed, requirements="requirements-v1")
        with factory.execution_lock(self.state), factory.task_lock(self.state, "fixture"):
            resumed = factory.rebase_worktree(self.path, onto=moved)
        self.assertEqual((resumed["candidate"], factory.load_task(self.path)["inputs"]["candidate"]), (moved, moved))
        with factory.execution_lock(self.state), factory.task_lock(self.state, "fixture"), \
                self.assertRaisesRegex(factory.FactoryError, "already contains"):
            factory.rebase_worktree(self.path, onto=moved)

    def test_release_refuses_a_dirty_worktree_or_a_running_assignment(self):
        self.provision()
        (self.destination / "unfinished.txt").write_text("work in progress\n")
        with patch.object(go_cache, "confirm_window"), patch.object(go_cache, "process_check", return_value={}), \
                self.release_locks() as lock_fds, \
                self.assertRaisesRegex(factory.FactoryError, "uncommitted inputs"):
            factory.release_worktree(self.path, lock_fds=lock_fds)
        (self.destination / "unfinished.txt").unlink()
        receipt = self.state / "fixture.reader.claude.json"
        factory.save(receipt, dict(state="starting", cwd=str(self.destination),
                                   worktree_git_dir=str(factory.git_dir(self.destination))))
        with self.release_locks() as lock_fds, \
                self.assertRaisesRegex(factory.FactoryError, "orphaned native receipt"):
            factory.release_worktree(self.path, lock_fds=lock_fds)
        self.assertTrue(self.destination.exists())
        self.assertFalse(self.path.with_suffix(".worktree-release.json").exists())

    def test_release_obeys_physical_lock_and_published_foreign_store(self):
        self.provision()
        admin = factory.git_dir(self.destination)
        foreign = self.root / "foreign"
        foreign.mkdir()
        with self.release_locks() as lock_fds:
            with go_cache.launch_lock(admin, foreign), self.assertRaisesRegex(factory.ExecutionBusy, "Busy lock"):
                factory.release_worktree(self.path, lock_fds=lock_fds)
            with self.assertRaisesRegex(factory.FactoryError, "state store"):
                factory.release_worktree(self.path, lock_fds=lock_fds)
        self.assertTrue(self.destination.exists())
        self.assertFalse(self.path.with_suffix(".worktree-release.json").exists())

    def test_process_death_after_remove_before_release_publication(self):
        self.provision()
        admin = factory.git_dir(self.destination)
        owner = self.path.read_bytes()
        release = """
import os, signal, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import factory, go_cache
publish = factory.publish_record
def stop_after_git(path, **kwargs):
    if path.name.endswith(".worktree-release.json"):
        os.kill(os.getpid(), signal.SIGKILL)
    return publish(path, **kwargs)
with patch.object(factory, "publish_record", stop_after_git), patch.object(go_cache, "process_check", return_value={}), \
        patch.object(go_cache, "confirm_window"):
    sys.exit(factory.main(["--state-dir", sys.argv[2], "release-worktree", "--task", "fixture"]))
"""
        result = subprocess.run([sys.executable, "-B", "-c", release, str(Path(factory.__file__).parent),
                                 str(self.state)], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        self.assertFalse(self.destination.exists())
        self.assertFalse(admin.exists())
        self.assertFalse(self.release_receipt.exists())
        intent = json.loads(self.release_intent.read_bytes())
        self.assertEqual(intent["phase"], "removing")
        observed = self.retry_terminal_sync("absent")
        self.assertEqual(observed["intent_id"], factory.load_task(self.path)["provisioning"]["owner_token"])
        self.assertEqual(json.loads(self.release_intent.read_bytes()), dict(intent, phase="absent"))
        self.assertEqual(self.path.read_bytes(), owner)

    def test_intact_remove_intent_rechecks_dirty_active_and_original_checkout(self):
        self.provision()
        git = factory.provisioning_git
        def stop_before_remove(repo, *args, **kwargs):
            if args[:2] == ("worktree", "remove"):
                raise InterruptedError("before remove")
            return git(repo, *args, **kwargs)

        with patch.object(go_cache, "confirm_window"), patch.object(go_cache, "process_check", return_value={}), \
                self.release_locks() as locks:
            with patch.object(factory, "provisioning_git", stop_before_remove), self.assertRaises(InterruptedError):
                factory.release_worktree(self.path, lock_fds=locks)
            intent = self.release_intent.read_bytes()
            (self.destination / "unfinished").touch()
            with self.assertRaisesRegex(factory.FactoryError, "uncommitted inputs"):
                factory.release_worktree(self.path, lock_fds=locks)
            (self.destination / "unfinished").unlink()
            active = self.state / "fixture.reader.claude.json"
            factory.save(active, dict(state="starting", cwd=str(self.destination),
                                      worktree_git_dir=str(factory.git_dir(self.destination))))
            with self.assertRaisesRegex(factory.FactoryError, "orphaned native receipt"):
                factory.release_worktree(self.path, lock_fds=locks)
            self.assertEqual(self.release_intent.read_bytes(), intent)
            active.unlink()
            stale = json.loads(intent)
            stale["checkout"]["head"] = "a" * 40
            factory.save(self.release_intent, stale)
            with self.assertRaisesRegex(factory.FactoryError, "intent differs"):
                factory.release_worktree(self.path, lock_fds=locks)
            self.assertTrue(self.destination.is_dir())
            self.assertFalse(self.release_receipt.exists())
            self.release_intent.write_bytes(intent)
            factory.release_worktree(self.path, lock_fds=locks)
        self.assertFalse(self.destination.exists())
        self.assertEqual(json.loads(self.release_receipt.read_bytes())["checkout"], json.loads(intent)["checkout"])

    def test_control_character_filenames_preserve_tree_index_and_disk_names(self):
        names = ["name\rsegment", "name\nsegment", "name\r\nsegment"]
        candidate = self.filename_candidate(names)
        record = self.provision()
        expected = {name.encode() for name in names}
        tree = subprocess.check_output([factory.git_binary(), "-C", str(self.repo),
            "ls-tree", "-r", "-z", "--name-only", candidate])
        index = subprocess.check_output([factory.git_binary(), "-C", str(self.destination), "ls-files", "-z"])
        disk = {os.fsencode(path.name) for path in self.destination.iterdir() if path.name != ".git"}
        self.assertEqual(set(tree.split(b"\0")) - {b""}, expected)
        self.assertEqual(set(index.split(b"\0")) - {b""}, expected)
        self.assertEqual(disk, expected)
        factory.check_checkout(str(self.repo), str(self.destination), candidate,
            expected_identity=record["admission"]["repository_identity"])
        (self.destination / names[0]).rename(self.destination / "substituted-name")
        with self.assertRaises(factory.FactoryError):
            factory.check_tracked_bytes(str(self.destination), candidate)

    def check_filename_equivalence(self, first, second, names):
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            probe = Path(temporary)
            (probe / first).write_text("fixed filesystem capability fixture")
            equivalent = (probe / second).exists()
        candidate = self.filename_candidate(names)
        if equivalent:
            with self.assertRaisesRegex(factory.FactoryError, "collide"):
                self.provision()
            self.assertIsNone(factory.load_task(self.path)["provisioning"])
            self.assertFalse(self.destination.exists())
            branch = subprocess.run([factory.git_binary(), "-C", str(self.repo),
                "show-ref", "--verify", "--quiet", "refs/heads/factory/fixture/backend"], check=False)
            self.assertEqual(branch.returncode, 1)
            self.assertEqual(list(self.root.glob(".factory-names-*")), [])
        else:
            record = self.provision()
            factory.check_checkout(str(self.repo), str(self.destination), candidate,
                expected_identity=record["admission"]["repository_identity"])

    def test_case_equivalence_is_checked_on_the_destination_filesystem(self):
        self.check_filename_equivalence("File", "file", ["File", "file"])

    def test_unicode_equivalence_is_checked_on_the_destination_filesystem(self):
        self.check_filename_equivalence("caf\u00e9", "cafe\u0301", ["caf\u00e9", "cafe\u0301"])

    def test_directory_aliases_refuse_before_intent(self):
        self.check_filename_equivalence("Dir", "dir", ["Dir/one", "dir/two"])

    def test_validation_refuses_two_git_paths_for_one_physical_file(self):
        candidate = self.filename_candidate(["File", "file"])
        checkout = self.root / "legacy-writer"
        self.git("worktree", "add", "--detach", "--no-checkout", str(checkout), candidate)
        for name in ("File", "file"):
            (checkout / name).write_bytes(b"filename fixture\n")
        original = Path.lstat
        first_info = original(checkout / "File")
        def aliased(path):
            return first_info if path == checkout / "file" else original(path)
        with patch.object(Path, "lstat", aliased), self.assertRaisesRegex(factory.FactoryError, "alias"):
            factory.check_tracked_bytes(str(checkout), candidate)

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
                patch.object(factory, "save", side_effect=lost_reply), self.assertRaises(factory.FactoryError):
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
        with patch.object(factory, "save", side_effect=interrupted), self.assertRaises(factory.FactoryError):
            self.provision()
        self.assertEqual(factory.load_task(self.path)["provisioning"]["state"], "provisioning")
        factory.hold_task(self.path, "Inspection hold")
        with patch.object(factory, "provisioning_git", wraps=factory.provisioning_git) as git:
            record = self.provision()
        self.assertFalse(any(call.args[1:3] == ("worktree", "add") for call in git.call_args_list))
        self.assertEqual(record["provisioning"]["state"], "provisioned")
        self.assertEqual(record["hold"], "Inspection hold")

    def test_completed_replay_preserves_detached_or_switched_head_and_writer_edits(self):
        self.provision()
        before = self.path.read_bytes()
        (self.destination / "source.bin").write_bytes(b"writer edit")
        self.git("-C", str(self.destination), "switch", "--quiet", "--detach")
        for branch in (None, "writer-next"):
            with self.subTest(branch=branch):
                if branch:
                    self.git("-C", str(self.destination), "switch", "--quiet", "-c", branch)
                head = (factory.git_dir(self.destination) / "HEAD").read_bytes()
                self.assertEqual(self.provision()["provisioning"]["state"], "provisioned")
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual((factory.git_dir(self.destination) / "HEAD").read_bytes(), head)
                self.assertEqual((self.destination / "source.bin").read_bytes(), b"writer edit")

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
        with patch.object(factory, "save", side_effect=OSError("disk full")), self.assertRaises(factory.FactoryError):
            self.provision()
        self.assertFalse(self.destination.exists())
        self.assertIsNone(factory.load_task(self.path)["provisioning"])
        with patch.object(factory, "provisioning_git", wraps=factory.provisioning_git) as git:
            original = factory.save
            def stop_after_intent(*args, **kwargs):
                original(*args, **kwargs)
                raise OSError("reply lost before Git")
            with patch.object(factory, "save", side_effect=stop_after_intent), self.assertRaises(factory.FactoryError):
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

    def test_missing_or_non_directory_parent_refuses_actionably_without_effects(self):
        file_parent = self.root / "file-parent"
        file_parent.write_text("preserve")
        before = self.path.read_bytes()
        worktrees = self.git("worktree", "list", "--porcelain")
        for parent in (self.root / "missing", file_parent):
            with self.subTest(parent=parent):
                code, _, error = self.cli("provision", "--task", "fixture", "--id", "backend",
                                          "--path", str(parent / "writer"))
                self.assertEqual(code, 1)
                self.assertIn("destination parent", error)
                self.assertIn("directory", error)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.git("worktree", "list", "--porcelain"), worktrees)
                self.assertEqual(self.git("branch", "--list", "factory/fixture/backend"), "")
        self.assertFalse((self.root / "missing").exists())
        self.assertEqual(file_parent.read_text(), "preserve")

    def test_unreadable_destination_parent_refuses_actionably_without_effects(self):
        original = Path.resolve
        def denied(path, *args, **kwargs):
            if path == self.destination.parent:
                raise PermissionError("fixed fixture denial")
            return original(path, *args, **kwargs)
        before = self.path.read_bytes()
        worktrees = self.git("worktree", "list", "--porcelain")
        with patch.object(Path, "resolve", denied):
            code, _, error = self.cli("provision", "--task", "fixture", "--id", "backend",
                                      "--path", str(self.destination))
        self.assertEqual(code, 1)
        self.assertIn("restore access to the destination parent directory", error)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.git("worktree", "list", "--porcelain"), worktrees)
        self.assertEqual(self.git("branch", "--list", "factory/fixture/backend"), "")
        self.assertFalse(self.destination.exists())

    def test_ancestor_and_descendant_refs_refuse_before_provisioning_intent(self):
        for ref in ("factory/fixture", "factory/fixture/backend/child", "Factory/fixture"):
            with self.subTest(ref=ref):
                self.git("branch", ref)
                before = self.path.read_bytes()
                try:
                    with self.assertRaisesRegex(factory.FactoryError, "namespace conflict"):
                        self.provision()
                    self.assertEqual(self.path.read_bytes(), before)
                    self.assertFalse(self.destination.exists())
                    self.assertEqual(len(self.git("worktree", "list", "--porcelain").split("worktree ")) - 1, 1)
                    self.assertEqual(self.git("rev-parse", ref), self.git("rev-parse", "HEAD"))
                finally:
                    self.git("branch", "-d", ref)

    def test_missing_ownership_metadata_has_actionable_recovery_without_mutation(self):
        record = self.provision()
        directory = Path(record["provisioning"]["git_dir"])
        before = self.path.read_bytes()
        for metadata in (self.destination / ".git", directory / "locked", directory / "gitdir"):
            with self.subTest(metadata=metadata.name):
                raw = metadata.read_bytes()
                if metadata.name == "locked":
                    self.git("worktree", "unlock", str(self.destination))
                else:
                    metadata.unlink()
                try:
                    code, _, error = self.cli("provision", "--task", "fixture", "--id", "backend", "--path", str(self.destination))
                    self.assertEqual(code, 1)
                    self.assertIn("inspect existing resources without retrying", error)
                    self.assertEqual(self.path.read_bytes(), before)
                    self.assertTrue((self.destination / "source.bin").is_file())
                finally:
                    metadata.write_bytes(raw)

    def test_unreadable_ownership_metadata_has_actionable_recovery(self):
        record = self.provision()
        directory = Path(record["provisioning"]["git_dir"])
        before = self.path.read_bytes()
        original = factory.read_record
        for metadata in (self.destination / ".git", directory / "locked", directory / "gitdir"):
            with self.subTest(metadata=metadata.name):
                def denied(path, **kwargs):
                    if path == metadata:
                        raise PermissionError("fixed fixture denial")
                    return original(path, **kwargs)
                with patch.object(factory, "read_record", side_effect=denied):
                    code, _, error = self.cli("provision", "--task", "fixture", "--id", "backend", "--path", str(self.destination))
                self.assertEqual(code, 1)
                self.assertIn("restore access to the original metadata", error)
                self.assertIn("without retrying, resetting or deleting", error)
                self.assertEqual(self.path.read_bytes(), before)

    def test_non_utf8_ownership_metadata_has_actionable_recovery(self):
        record = self.provision()
        directory = Path(record["provisioning"]["git_dir"])
        before = self.path.read_bytes()
        for metadata in (self.destination / ".git", directory / "locked", directory / "gitdir"):
            with self.subTest(metadata=metadata.name):
                original = metadata.read_bytes()
                metadata.write_bytes(b"\xff fixed damaged metadata")
                try:
                    code, _, error = self.cli("provision", "--task", "fixture", "--id", "backend", "--path", str(self.destination))
                    self.assertEqual(code, 1)
                    self.assertIn("original metadata", error)
                    self.assertIn("without retrying, resetting or deleting", error)
                    self.assertNotIn("codec", error)
                    self.assertEqual(self.path.read_bytes(), before)
                finally:
                    metadata.write_bytes(original)

    def test_preflight_probe_failures_tell_operator_what_to_restore_without_intent(self):
        original = factory.git_run
        def denied_config(repository, *args, **kwargs):
            if args == ("config", "--null", "--list"):
                return subprocess.CompletedProcess([], 1, "", "fixture denied")
            return original(repository, *args, **kwargs)
        for target, failure, action in (
                ("git_run", denied_config, "restore Git configuration access"),
                ("provisioning_git", factory.FactoryError("old retained-intent diagnostic"), "restore local Git object access")):
            with self.subTest(target=target), patch.object(factory, target, side_effect=failure):
                before = self.path.read_bytes()
                code, _, error = self.cli("provision", "--task", "fixture", "--id", "backend", "--path", str(self.destination))
                self.assertEqual(code, 1)
                self.assertIn(action, error)
                self.assertNotIn("retained", error)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertFalse(self.destination.exists())

    def test_cli_intent_save_failure_names_state_reconciliation_before_git(self):
        before = self.path.read_bytes()
        with patch.object(factory, "save", side_effect=PermissionError("fixed denied")):
            code, _, error = self.cli("provision", "--task", "fixture", "--id", "backend", "--path", str(self.destination))
        self.assertEqual(code, 1)
        self.assertIn("restore state-store access/space", error)
        self.assertIn("no Git worktree creation was started", error)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.git("branch", "--list", "factory/fixture/backend"), "")

    def test_cli_materialization_failure_retains_resources_for_inspection(self):
        with patch.object(factory.os, "fchmod", side_effect=PermissionError("fixed denied")):
            code, _, error = self.cli("provision", "--task", "fixture", "--id", "backend", "--path", str(self.destination))
        self.assertEqual(code, 1)
        self.assertIn("restore filesystem access/space", error)
        self.assertIn("without retrying, resetting or deleting", error)
        self.assertEqual(factory.load_task(self.path)["provisioning"]["state"], "provisioning")
        self.assertTrue(self.destination.exists())
        self.assertEqual(self.git("rev-parse", "factory/fixture/backend"), self.git("rev-parse", "HEAD"))

    def test_cli_completion_save_failure_reconciles_same_existing_effect(self):
        original = factory.save
        def denied(path, value, **kwargs):
            if value.get("provisioning", {}).get("state") == "provisioned":
                raise PermissionError("fixed denied")
            return original(path, value, **kwargs)
        with patch.object(factory, "save", side_effect=denied):
            code, _, error = self.cli("provision", "--task", "fixture", "--id", "backend", "--path", str(self.destination))
        self.assertEqual(code, 1)
        self.assertIn("restore state-store access/space", error)
        self.assertIn("same saved intent and existing worktree", error)
        pending = factory.load_task(self.path)["provisioning"]
        self.assertEqual(pending["state"], "provisioning")
        reconciled = self.provision()["provisioning"]
        self.assertEqual(reconciled["state"], "provisioned")
        witness = go_cache.checkout_binding(self.destination, Path(reconciled["git_dir"]), factory.repository_identity(self.repo))
        self.assertTrue(factory.original_checkout(reconciled, witness))
        self.assertEqual(reconciled["checkout_inodes"], pending["checkout_inodes"])
        with patch.object(sys, "stdin", StringIO("fixture\n")) as terminal, patch.object(terminal, "isatty", return_value=True), \
                patch.object(go_cache, "process_check", return_value={}):
            code, _, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 0, error)
        proof, _ = factory.release_proof(self.path.with_suffix(".worktree-release.json"), factory.load_task(self.path))
        self.assertEqual(proof["checkout"], witness)
        code, output, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(output)["already_released"])

    def test_unpublished_original_observation_preserves_pending_owner(self):
        original = factory.save
        def interrupted(path, record):
            if "checkout_inodes" in record["provisioning"]:
                raise OSError("interrupted observation publication")
            original(path, record)
        with patch.object(factory, "save", side_effect=interrupted), self.assertRaisesRegex(
                factory.FactoryError, "original checkout identity save"):
            self.provision()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "Retain the worktree"):
            self.provision()
        self.assertTrue(self.destination.is_dir())
        self.assertEqual(self.path.read_bytes(), before)

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
        with patch.object(factory, "provisioning_git", side_effect=substituted), self.assertRaises(factory.FactoryError):
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
                with self.subTest(key=key):
                    code, _, error = self.cli("provision", "--task", "fixture", "--id", "backend", "--path", str(self.destination))
                    self.assertEqual(code, 1)
                    self.assertIn("Unsupported provisioning", error)
                    self.assertIn("remove this setting or choose a supported repository", error)
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
        code, _, error = self.cli("provision", "--task", "fixture", "--id", "backend", "--path", str(self.destination))
        self.assertEqual(code, 1)
        self.assertIn("select a candidate without symlinks", error)
        self.assertFalse(self.destination.exists())
        self.assertIsNone(factory.load_task(self.path)["provisioning"])

    def test_alternates_refusal_names_supported_repository_without_effect(self):
        (self.repo / ".git/objects/info/alternates").write_text("")
        before = self.path.read_bytes()
        code, _, error = self.cli("provision", "--task", "fixture", "--id", "backend", "--path", str(self.destination))
        self.assertEqual(code, 1)
        self.assertIn("choose a repository with independent local objects", error)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.destination.exists())

    def test_version_five_migration_preserves_gates_and_rejects_downgrade_or_damage(self):
        factory.hold_task(self.path, "Keep hold")
        original = factory.load_task(self.path)
        legacy = copy.deepcopy(original)
        legacy.update(schema_version=5)
        del legacy["provisioning"]
        del legacy["allowance_events"]
        factory.save(self.path, legacy)
        upgraded = factory.load_task(self.path)
        self.assertEqual(upgraded, original)
        self.assertEqual(upgraded["schema_version"], 11)
        for broken in ({**upgraded, "schema_version": 5}, {**upgraded, "provisioning": {}},
                       {key: value for key, value in upgraded.items() if key != "provisioning"}):
            factory.save(self.path, broken)
            before = self.path.read_bytes()
            with self.assertRaises(factory.FactoryError):
                factory.load_task(self.path)
            self.assertEqual(self.path.read_bytes(), before)

    def test_schema_six_provisioning_migration_preserves_owned_effect_without_a_grant(self):
        provisioned = self.provision()
        factory.hold_task(self.path, "Keep the existing hold", request_id="operator-hold", clock=lambda: 1000)
        original = factory.load_task(self.path)
        legacy = copy.deepcopy(original)
        legacy["schema_version"] = 6
        del legacy["allowance_events"]
        factory.save(self.path, legacy)
        upgraded = factory.load_task(self.path)
        self.assertEqual(upgraded, original)
        self.assertEqual(upgraded["provisioning"], provisioned["provisioning"])
        self.assertEqual(upgraded["allowance_events"], [])
        self.assertEqual(factory.task_view(upgraded)["allowances"]["correction"], dict(limit=2, used=0, remaining=2))
        before = self.path.read_bytes()
        with patch.object(factory, "provisioning_git", side_effect=AssertionError("Migration never launches Git")):
            self.assertEqual(factory.load_task(self.path), upgraded)
        self.assertEqual(self.path.read_bytes(), before)

    def test_schema_seven_migration_preserves_additions_without_adopting_a_worktree(self):
        factory.extend_allowance(self.path, purpose="correction", additional=1, expected_limit=2,
            request_id="fixture-grant", reason="Fixture-only operator decision", clock=lambda: 1000)
        original = factory.load_task(self.path)
        legacy = copy.deepcopy(original)
        legacy["schema_version"] = 7
        del legacy["provisioning"]
        factory.save(self.path, legacy)
        with patch.object(factory, "provisioning_git", side_effect=AssertionError("Migration never launches Git")):
            upgraded = factory.load_task(self.path)
        self.assertEqual(upgraded, original)
        self.assertIsNone(upgraded["provisioning"])
        self.assertEqual(factory.task_view(upgraded)["allowances"]["correction"], dict(limit=3, used=0, remaining=3))
        self.assertFalse(self.destination.exists())

    def test_old_versions_cannot_silently_drop_new_owner_fields(self):
        current = factory.load_task(self.path)
        for version in (5, 6, 7):
            malformed = {**current, "schema_version": version}
            factory.save(self.path, malformed)
            before = self.path.read_bytes()
            with self.subTest(version=version), self.assertRaisesRegex(factory.FactoryError, "newer fields"):
                factory.load_task(self.path)
            self.assertEqual(self.path.read_bytes(), before)

    def test_migration_publication_errors_are_actionable_before_or_after_replace(self):
        original = factory.load_task(self.path)
        save = factory.save
        for version in (6, 7):
            for published in (False, True):
                with self.subTest(version=version, published=published):
                    legacy = {**original, "schema_version": version}
                    del legacy["allowance_events" if version == 6 else "provisioning"]
                    save(self.path, legacy)
                    before = self.path.read_bytes()
                    def failed(path, value, **kwargs):
                        if published:
                            save(path, value, **kwargs)
                        raise OSError("fixed migration storage failure")
                    with patch.object(factory, "save", side_effect=failed):
                        code, _, error = self.cli("pause", "--task", "fixture", "--reason", "Hold fixture")
                    self.assertEqual(code, 1)
                    self.assertIn("task schema migration save", error)
                    self.assertIn("restore state-store access/space", error)
                    self.assertIn("reload or inspect the task record", error)
                    if not published:
                        self.assertEqual(self.path.read_bytes(), before)
                    self.assertEqual(factory.load_task(self.path), original)
                    self.assertFalse(self.destination.exists())


class GitTimeouts(unittest.TestCase):
    """A writer commit failed with "Cannot run Git" while the Codex sandbox took up to 6.3 s just to start."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.repo = Path(directory.name).resolve() / "repo"
        subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(self.repo)], check=True)

    def timeout_of(self, *, sandboxed):
        seen = []
        real = subprocess.run
        def run(command, **options):
            if "diff" in command:
                seen.append(options["timeout"])
                raise subprocess.TimeoutExpired(command, options["timeout"])
            return real(command, **options)
        with patch.object(factory.subprocess, "run", side_effect=run):
            scope = factory.git_execution(lambda: [], dict(os.environ)) if sandboxed else factory.nullcontext()
            with scope, self.assertRaises(factory.FactoryError) as error:
                factory.git_run(str(self.repo), "-c", "core.fsmonitor=false", "diff", "--name-only")
        return seen, str(error.exception)

    def test_git_gets_ten_minutes_and_a_timeout_says_so(self):
        # #175: at load 265-346 on 16 cores, git rev-parse passed the earlier 60 seconds.
        for sandboxed in (True, False):
            with self.subTest(sandboxed=sandboxed):
                seen, message = self.timeout_of(sandboxed=sandboxed)
                self.assertEqual(seen, [600])
                self.assertIn("did not finish within 600 seconds (git diff)", message)
                self.assertNotIn("Cannot run Git", message)


if __name__ == "__main__":
    unittest.main()
