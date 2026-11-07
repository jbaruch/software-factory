"""Native result/lifecycle tests use fake processes and Rex, never model calls."""
import asyncio
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
import copy
import hashlib
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import codex_worker as worker
import factory
from test_rex_launcher import FakeRex


def events(outcome="approved"):
    return [dict(type="thread.started", thread_id="thread-fixture"),
            dict(type="turn.started"),
            dict(type="item.completed", item=dict(type="agent_message", text="Checking the candidate")),
            dict(type="item.completed", item=dict(type="agent_message", text=json.dumps(
                dict(outcome=outcome, artifacts=["evidence/qa.txt"])))),
            dict(type="turn.completed", usage=dict(input_tokens=100, output_tokens=30))]


class FakeProcess:
    def __init__(self, payload, exit_code=0, timeout=False):
        self.lines = iter([json.dumps(event).encode() + b"\n" for event in payload])
        self.stdout = self
        self.returncode = None
        self.exit_code, self.timeout = exit_code, timeout
        self.signals = []

    async def readline(self):
        line = next(self.lines, None)
        if line is None and self.timeout:
            raise TimeoutError("fixture")
        return line or b""

    async def wait(self):
        self.returncode = self.exit_code
        return self.returncode

    def send_signal(self, signal):
        self.signals.append(signal)


class NativeResults(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repo = self.root / "repo"
        subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(self.repo)], check=True)
        for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.invalid")):
            subprocess.run(["git", "-C", str(self.repo), "config", key, value], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Fixture",
                        "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                        "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--allow-empty", "-m", "Fixture"],
                       check=True, env={**os.environ, "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
                                        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"})
        previous_cwd = Path.cwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, previous_cwd)
        self.candidate = factory.git_commit(str(self.repo), "HEAD")
        self.identity: dict = factory.repository_identity(str(self.repo), create=True)
        self.brief = self.root / "brief.md"
        self.brief.write_text("Review the exact candidate and link evidence.")
        self.args = worker.parser().parse_args([
            "--codex", sys.executable, "--repo", str(self.repo), "--cwd", str(self.repo),
            "--brief", str(self.brief), "--brief-sha256", hashlib.sha256(self.brief.read_bytes()).hexdigest(),
            "--candidate", self.candidate, "--requirements", "requirements-1", "--job", "task/qa",
            "--repo-identity", json.dumps(self.identity),
            "--worktree-git-dir", str(factory.git_dir(str(self.repo))),
            "--model", "fixture-model", "--effort", "high", "--events", str(self.root / "events.jsonl"),
            "--receipt", str(self.root / "receipt.json")])
        self.env = patch.dict(os.environ, {"CODEX_API_KEY": "", "OPENAI_API_KEY": "",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.spawns = []
        self.process = FakeProcess(events())
        self.git_scope = lambda *args, **kwargs: nullcontext()
        self.scans = []
        self.scan = lambda args, receipt, **kwargs: self.scans.append(receipt["commit"]["staged_tree"])

    async def spawn(self, *command, **options):
        self.spawns.append(command)
        return self.process

    def execute(self):
        with redirect_stdout(StringIO()):
            return asyncio.run(worker.execute(self.args, spawn=self.spawn,
                                               clock=lambda: 1000.0, timer=lambda: 10.0,
                                               git_scope=self.git_scope, scan=self.scan))

    def test_completion_preserves_original_events_and_private_bound_receipt(self):
        receipt = self.execute()
        self.assertEqual(receipt["state"], "completed")
        self.assertEqual(receipt["schema_version"], 5)
        self.assertEqual(receipt["repository_identity"], self.identity)
        self.assertEqual(receipt["worktree_git_dir"], str(factory.git_dir(str(self.repo))))
        self.assertEqual(receipt["candidate"], self.candidate)
        self.assertEqual(receipt["inputs"]["requirements"], "requirements-1")
        self.assertEqual(receipt["thread_id"], "thread-fixture")
        self.assertEqual(receipt["usage"], dict(input_tokens=100, output_tokens=30))
        self.assertEqual(receipt["finished_at"], 1000.0)
        self.assertEqual(json.loads(self.args.receipt.read_text()), receipt)
        self.assertEqual(worker.read_events(self.args.events), events())
        first = json.loads(self.args.events.read_text().splitlines()[0])
        self.assertEqual(first, dict(schema_version=1, kind="codex-event-line",
                                    data=json.dumps(events()[0]) + "\n"))
        self.assertEqual(self.args.events.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.args.receipt.stat().st_mode & 0o777, 0o600)
        self.assertIn("read-only", self.spawns[0])
        self.assertEqual(self.spawns[0][3:7], ("--disable", "multi_agent", "--disable", "multi_agent_v2"))
        self.assertEqual(self.spawns[0][self.spawns[0].index("--cd") + 1], ".")
        self.assertEqual(self.spawns[0][-1], self.brief.read_text())
        before = self.args.receipt.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "never relaunch"):
            self.execute()
        self.assertEqual(len(self.spawns), 1)
        self.assertEqual(self.args.receipt.read_bytes(), before)

    def test_duplicate_web_search_telemetry_ids_are_not_used_as_evidence(self):
        raw = (b'{"type":"item.started","item":{"id":"item_20","type":"web_search",'
               b'"id":"exec-fixed","query":"","action":{"type":"other"}}}\n')
        payload = events()
        lines = [json.dumps(event).encode() + b"\n" for event in payload]
        self.process.lines = iter([*lines[:2], raw, *lines[2:]])
        receipt = self.execute()
        self.assertEqual(receipt["state"], "completed")
        retained = [json.loads(line) for line in self.args.events.read_text().splitlines()]
        self.assertEqual(retained[2]["data"], raw.decode())
        decoded = worker.read_events(self.args.events)
        self.assertNotIn("id", decoded[2]["item"])
        self.assertEqual(worker.parse_events(decoded)["report"], dict(outcome="approved", artifacts=["evidence/qa.txt"]))

    def writer(self, *, outcome="completed", exit_code=0):
        for key, value in {"user.name": "Fixture", "user.email": "fixture@example.invalid",
                           "commit.gpgsign": "false", "core.hooksPath": "/dev/null"}.items():
            subprocess.run(["git", "-C", str(self.repo), "config", key, value], check=True)
        self.args.write = True
        if self.args.role is None:
            self.args.role = "backend"
            self.args.brief.write_text("# Assignment: backend\n\nDeliver the fixture change.\n")
            self.args.brief_sha256 = hashlib.sha256(self.args.brief.read_bytes()).hexdigest()
        self.args.commit_message = "feat(example): Add the assigned behavior\n\nExercise the factory-owned writer commit."
        self.process = FakeProcess(events(outcome), exit_code=exit_code)
        async def spawn(*command, **options):
            self.spawns.append(command)
            self.assertEqual(options["env"]["PYTHONDONTWRITEBYTECODE"], "1")
            (self.repo / "feature.py").write_text("answer = 42\n")
            return self.process
        self.spawn = spawn

    def test_factory_commits_finished_writer_changes_once(self):
        self.writer()
        receipt = self.execute()
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual(receipt["commit"]["state"], "created")
        candidate = receipt["candidate"]
        self.assertNotEqual(candidate, self.candidate)
        self.assertEqual(factory.git_run(".", "show", "-s", "--format=%P", candidate).stdout.strip(), self.candidate)
        self.assertEqual(factory.git_run(".", "show", candidate + ":feature.py").stdout, "answer = 42\n")
        self.assertEqual(factory.git_run(".", "show", "-s", "--format=%B", candidate).stdout.strip(), self.args.commit_message)
        self.assertEqual(factory.git_run(".", "status", "--porcelain").stdout, "")
        worker.check_commit_receipt(receipt, self.args, str(self.repo))
        with self.assertRaisesRegex(factory.FactoryError, "never relaunch"):
            self.execute()
        self.assertEqual(len(self.spawns), 1)
        self.assertEqual(factory.git_commit(".", "HEAD"), candidate)
        self.assertEqual(self.scans, [receipt["commit"]["staged_tree"]])

    def test_secret_scan_cannot_be_skipped_when_hooks_are_disabled(self):
        self.writer()
        def refuse(args, receipt, **kwargs):
            self.assertEqual(receipt["commit"]["state"], "committing")
            raise factory.FactoryError("Secret scan refused the staged tree")
        self.scan = refuse
        receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("Secret scan refused", receipt["error"])
        self.assertEqual(factory.git_commit(".", "HEAD"), self.candidate)
        self.assertEqual(receipt["commit"]["state"], "committing")

    def test_missing_secret_scanner_refuses_before_native_launch(self):
        self.writer()
        self.scan = worker.scan_staged
        which = shutil.which
        def unavailable(name, *args, **kwargs):
            return None if name == "gitleaks" else which(name, *args, **kwargs)
        with patch.object(worker.shutil, "which", side_effect=unavailable):
            with self.assertRaisesRegex(factory.FactoryError, "Install a native Gitleaks"):
                self.execute()
        self.assertEqual(self.spawns, [])
        self.assertFalse(self.args.receipt.exists())

    def test_zero_exit_native_impostor_is_not_a_secret_scanner(self):
        self.writer()
        self.scan = worker.scan_staged
        which = shutil.which
        def impostor(name, *args, **kwargs):
            return which("true", path=os.defpath) if name == "gitleaks" else which(name, *args, **kwargs)
        with patch.object(worker.shutil, "which", side_effect=impostor):
            with self.assertRaisesRegex(factory.FactoryError, "Scanner identity/version"):
                self.execute()
        self.assertEqual(self.spawns, [])
        self.assertFalse(self.args.receipt.exists())

    def test_scanner_errors_cannot_report_a_clean_staged_tree(self):
        (self.repo / "source.txt").write_bytes(b"staged fixture bytes\r\n")
        self.assertEqual(factory.git_run(".", "-c", "core.autocrlf=false", "add", "--all").returncode, 0)
        tree = factory.git_run(".", "write-tree").stdout.strip()
        self.args._gitleaks = Path(sys.executable)
        self.args.commit_message = "Fixture commit"
        @contextmanager
        def scope(*args, **kwargs):
            with factory.git_execution(lambda: [], {**factory.git_environment(), "TMPDIR": str(self.root)}):
                yield
        run = subprocess.run
        for code, error in ((1, b""), (0, b"scanner error")):
            with self.subTest(code=code, error=error):
                def scanner(command, **options):
                    if command[0] != str(self.args._gitleaks):
                        return run(command, **options)
                    self.assertIn(b"staged fixture bytes\r\n", options["stdin"].read())
                    return subprocess.CompletedProcess(command, code, stdout=b"", stderr=error)
                with patch.object(worker.subprocess, "run", side_effect=scanner):
                    with self.assertRaisesRegex(factory.FactoryError, "Secret scan refused"):
                        worker.scan_staged(self.args, dict(repository_identity=self.identity,
                            commit=dict(staged_tree=tree)), git_scope=scope)

    def test_secret_scan_includes_paths_message_and_git_identities(self):
        self.args._gitleaks = Path(sys.executable)
        @contextmanager
        def scope(*args, **kwargs):
            with factory.git_execution(lambda: [], {**factory.git_environment(), "TMPDIR": str(self.root)}):
                yield
        run = subprocess.run
        for location in ("filename", "message", "author", "committer"):
            with self.subTest(location=location):
                sentinel = "ghp_" + hashlib.sha256(location.encode()).hexdigest()[:36]
                name = sentinel if location == "filename" else "ordinary.txt"
                (self.repo / name).write_text("Innocuous fixture contents\n")
                self.args.commit_message = sentinel if location == "message" else "Fixture commit"
                self.assertEqual(factory.git_run(".", "add", "--all").returncode, 0)
                tree = factory.git_run(".", "write-tree").stdout.strip()
                def scanner(command, **options):
                    if command[0] != str(self.args._gitleaks):
                        return run(command, **options)
                    present = sentinel.encode() in options["stdin"].read()
                    return subprocess.CompletedProcess(command, int(present), stdout=b"", stderr=b"")
                identity = ({"GIT_AUTHOR_NAME": sentinel} if location == "author" else
                            {"GIT_COMMITTER_EMAIL": sentinel + "@fixture.invalid"} if location == "committer" else {})
                with patch.dict(os.environ, identity), patch.object(worker.subprocess, "run", side_effect=scanner):
                    with self.assertRaisesRegex(factory.FactoryError, "Secret scan refused"):
                        worker.scan_staged(self.args, dict(repository_identity=self.identity,
                            commit=dict(staged_tree=tree)), git_scope=scope)

    def test_native_environment_dump_cannot_commit_host_credentials(self):
        self.writer()
        selected = self.root / "native-configuration"
        observed = {}
        original_spawn = self.spawn
        async def dump_environment(*command, **options):
            observed.update(options["env"])
            (self.repo / "debug.json").write_text(json.dumps(options["env"], sort_keys=True))
            return await original_spawn(*command, **options)
        self.spawn = dump_environment
        with patch.dict(os.environ, GH_TOKEN="credential-sentinel-github", AWS_SECRET_ACCESS_KEY="credential-sentinel-cloud",
                        UV_INDEX_URL="https://credential-sentinel-index@example.invalid", CODEX_HOME=str(selected)):
            receipt = self.execute()
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        output = factory.git_run(".", "show", receipt["candidate"] + ":debug.json").stdout
        self.assertNotIn("credential-sentinel", output)
        self.assertEqual(observed["CODEX_HOME"], str(selected))
        self.assertEqual(observed["GIT_NO_REPLACE_OBJECTS"], "1")
        self.assertNotIn("GH_TOKEN", observed)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", observed)

    def test_alternate_python_cannot_give_a_readonly_role_writer_effects(self):
        self.writer()
        self.args.role = "qa"
        self.args.brief.write_text("# Assignment: qa\n\nIndependent verification.\n")
        self.args.brief_sha256 = hashlib.sha256(self.args.brief.read_bytes()).hexdigest()
        alternate = self.root / "alternate-python"
        alternate.symlink_to(sys.executable)
        command = [str(alternate), "-B", str(Path(worker.__file__).resolve())]
        for key, value in vars(self.args).items():
            if key == "write":
                command.append("--write")
            elif value is not None:
                command.extend(["--" + key.replace("_", "-"), str(value)])
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn("specialty does not permit writer", result.stderr)
        self.assertFalse(self.args.receipt.exists())
        self.assertFalse(self.args.events.exists())
        self.args.role = "backend"
        with self.assertRaisesRegex(factory.FactoryError, "role differs from the bound"):
            self.execute()
        self.assertEqual(self.spawns, [])

    def test_writer_cannot_replace_the_cli_used_to_confine_later_git_callbacks(self):
        self.writer()
        for directory in (self.repo, self.repo / ".git"):
            selected = directory / "selected-codex"
            selected.write_text("#!/bin/sh\nexit 0\n")
            selected.chmod(0o700)
            if directory == self.repo:
                self.assertEqual(factory.git_run(".", "add", "--all").returncode, 0)
                self.assertEqual(factory.git_run(".", "commit", "-m", "Tracked CLI fixture").returncode, 0)
                self.candidate = self.args.candidate = factory.git_commit(".", "HEAD")
            self.args.codex = selected
            with self.assertRaisesRegex(factory.FactoryError, "trusted Codex installation outside"):
                self.execute()
            self.assertFalse(self.args.receipt.exists())
            self.assertFalse(self.args.events.exists())
            self.assertEqual(self.spawns, [])

    def test_writer_cannot_replace_the_python_used_by_git_or_signing(self):
        self.writer()
        interpreter = self.repo / "replaceable-python"
        interpreter.write_text("writer-controlled interpreter fixture\n")
        self.assertEqual(factory.git_run(".", "add", "--all").returncode, 0)
        self.assertEqual(factory.git_run(".", "commit", "-m", "Runtime fixture").returncode, 0)
        self.args.candidate = factory.git_commit(".", "HEAD")
        for signing in ("false", "true"):
            with self.subTest(signing=signing), patch.object(worker.sys, "executable", str(interpreter)), \
                    patch.object(worker, "private_signer") as signer:
                self.assertEqual(factory.git_run(".", "config", "commit.gpgsign", signing).returncode, 0)
                with self.assertRaisesRegex(factory.FactoryError, "Python runtime outside"):
                    self.execute()
                signer.assert_not_called()
                self.assertFalse(self.args.receipt.exists())
                self.assertFalse(self.args.events.exists())
                self.assertEqual(self.spawns, [])

    def test_writer_refuses_script_wrappers_and_checkout_path_lookup(self):
        self.writer()
        wrapper = self.root / "codex-wrapper"
        wrapper.write_text("#!/usr/bin/env python3\nraise SystemExit(0)\n")
        wrapper.chmod(0o700)
        original = self.args.codex
        self.args.codex = wrapper
        with self.assertRaisesRegex(factory.FactoryError, "Script-form launchers"):
            self.execute()
        self.args.codex = original
        alias = self.repo / ".git/trusted-tools"
        alias.symlink_to(Path(sys.executable).resolve().parent, target_is_directory=True)
        for entry in (str(self.repo), str(alias), ""):
            with self.subTest(entry=entry), patch.dict(os.environ, PATH=entry + os.pathsep + os.defpath):
                with self.assertRaisesRegex(factory.FactoryError, "writer-controlled PATH"):
                    self.execute()
        self.assertFalse(self.args.receipt.exists())
        self.assertFalse(self.args.events.exists())
        self.assertEqual(self.spawns, [])

    def test_git_callbacks_and_postflight_use_the_closed_local_execution_scope(self):
        self.writer()
        self.git_scope = worker.isolated_git
        package = self.root / "external-cli/node_modules/@openai/codex"
        (package / "bin").mkdir(parents=True)
        (package / "package.json").write_text('{"name":"@openai/codex"}')
        self.args.codex = package / "bin/codex.js"
        self.args.codex.write_text("// Fake native transport; runtime is outside Python/Homebrew.\n")
        self.args.codex.chmod(0o700)
        hook = self.repo / ".git/hooks/pre-commit"
        hook.write_text(f"#!{sys.executable}\nimport os\nfrom pathlib import Path\nassert 'GH_TOKEN' not in os.environ\nPath('.git/environment-checked').write_text('passed')\n")
        hook.chmod(0o700)
        self.assertEqual(factory.git_run(".", "config", "core.hooksPath", ".git/hooks").returncode, 0)
        run = subprocess.run
        calls = []
        def sandbox_transport(command, **options):
            if len(command) > 1 and command[1] == "sandbox":
                calls.append((command, dict(options["env"])))
                name = command[command.index("--permission-profile") + 1]
                profile = tomllib.loads(command[command.index("-c") + 1])["permissions"][name]
                self.assertEqual(profile["filesystem"][str(self.args.codex.resolve())], "read")
                self.assertEqual(profile["filesystem"][str(package.resolve() / "vendor")], "read")
                self.assertNotIn(str(package.resolve()), profile["filesystem"])
                self.assertNotIn(str(package.resolve() / "bin"), profile["filesystem"])
                # Fake only OS isolation; Git, hooks and receipt checks stay real.
                command = command[command.index("--") + 1:]
                options["env"] = {**options["env"], "GH_TOKEN": "fixture-cli-injected-token"}
            return run(command, **options)
        with patch.dict(os.environ, GH_TOKEN="fixture-token", GITHUB_TOKEN="fixture-token",
                        AWS_SECRET_ACCESS_KEY="fixture-token"), \
                patch.object(factory.subprocess, "run", side_effect=sandbox_transport):
            receipt = self.execute()
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual((self.repo / ".git/environment-checked").read_text(), "passed")
        self.assertIsNone(factory.GIT_EXECUTION.get())
        commands = [command[command.index("--") + 1:] for command, _ in calls]
        self.assertTrue(any("add" in command for command in commands))
        self.assertTrue(any("commit" in command for command in commands))
        self.assertTrue(any("status" in command for command in commands))
        self.assertGreater(len(commands), 6)  # Includes the final checkout verification.
        names = [command[command.index("--permission-profile") + 1] for command, _ in calls]
        self.assertEqual(len(set(names)), len(calls))
        for command, environment in calls:
            name = command[command.index("--permission-profile") + 1]
            profile = tomllib.loads(command[command.index("-c") + 1])["permissions"][name]
            self.assertIs(profile["network"]["enabled"], False)
            self.assertEqual(profile["filesystem"][":root"], "deny")
            self.assertEqual(profile["filesystem"][str(self.repo.resolve())], "write")
            self.assertEqual(profile["filesystem"][str(self.repo.resolve() / ".git")], "deny" if "GIT_INDEX_FILE" in environment else "write")
            if "GIT_INDEX_FILE" in environment:
                self.assertNotIn("SSH_AUTH_SOCK", environment)
            self.assertTrue(Path(environment["TMPDIR"]).is_relative_to(self.repo.resolve() / ".git"))
            self.assertFalse(any(key in environment for key in (
                "GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY")))

    def test_standalone_cli_does_not_grant_its_parent_directory(self):
        self.args.codex = self.root / "codex"
        self.args.codex.write_text("Standalone fixture executable\n")
        self.args.codex.chmod(0o700)
        with worker.isolated_git(self.args, self.identity):
            prefix = factory.GIT_EXECUTION.get()[0]()
            name = prefix[prefix.index("--permission-profile") + 1]
            files = tomllib.loads(prefix[prefix.index("-c") + 1])["permissions"][name]["filesystem"]
            self.assertEqual(files[str(self.args.codex.resolve())], "read")
            self.assertNotIn(str(self.root.resolve()), files)

    def test_precommit_grants_refuse_host_roots_and_source_overlap(self):
        tool = self.root / "precommit-venv"
        (tool / "bin").mkdir(parents=True)
        (tool / "pyvenv.cfg").write_text("Fixture environment\n")
        (tool / "bin/python").symlink_to(Path(sys.executable).resolve())
        which = shutil.which
        def selected(name, *args, **kwargs):
            return str(tool / "bin/pre-commit") if name == "pre-commit" else which(name, *args, **kwargs)
        with patch.object(worker.shutil, "which", side_effect=selected):
            for path in (Path("/"), Path.home(), self.repo, factory.git_dir("."), Path(tempfile.gettempdir())):
                with self.subTest(cache=str(path)), patch.dict(os.environ, PRE_COMMIT_HOME=str(path)):
                    with self.assertRaisesRegex(factory.FactoryError, "dedicated tool/cache"):
                        with worker.isolated_git(self.args, self.identity):
                            self.fail("Broad callback grant was accepted")
        runtime = self.root / "other-python"
        (runtime / "bin").mkdir(parents=True)
        (runtime / "lib/python3.99").mkdir(parents=True)
        (runtime / "lib/python3.99/os.py").write_text("Fixture standard library\n")
        executable = runtime / "bin/python"
        executable.write_text("Fixture interpreter\n")
        secret = self.root / "credential"
        secret.write_text("fixture credential sentinel\n")
        (runtime / "lib/libpython.so").symlink_to(secret)
        (tool / "bin/python").unlink()
        (tool / "bin/python").symlink_to(executable)
        with patch.object(worker.shutil, "which", side_effect=selected):
            with self.assertRaisesRegex(factory.FactoryError, "runtime library outside"):
                with worker.isolated_git(self.args, self.identity):
                    self.fail("A library symlink granted an unrelated credential")
        def home_launcher(name, *args, **kwargs):
            return str(self.root / "bin/pre-commit") if name == "pre-commit" else which(name, *args, **kwargs)
        with patch.object(worker.Path, "home", return_value=self.root), \
                patch.object(worker.shutil, "which", side_effect=home_launcher):
            with self.assertRaisesRegex(factory.FactoryError, "dedicated tool/cache"):
                with worker.isolated_git(self.args, self.identity):
                    self.fail("Home-wide tool grant was accepted")
        self.assertIsNone(factory.GIT_EXECUTION.get())

    def test_native_sandbox_cannot_rewrite_configured_git_policy(self):
        self.writer()
        self.git_scope = worker.isolated_git
        run = subprocess.run
        def rewritten(command, **options):
            if len(command) > 1 and command[1] == "sandbox":
                command = command[command.index("--") + 1:]
                options["env"] = {**options["env"], "GIT_CONFIG_VALUE_0": "fixture-rewritten-hooks"}
            return run(command, **options)
        with patch.dict(os.environ, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath",
                        GIT_CONFIG_VALUE_0="/dev/null"), \
                patch.object(factory.subprocess, "run", side_effect=rewritten):
            receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("rewrote protected Git environment", receipt["error"])
        self.assertIsNone(factory.GIT_EXECUTION.get())
        self.assertEqual(factory.git_commit(".", "HEAD"), self.candidate)

    def test_unavailable_git_isolation_never_falls_back_to_host_execution(self):
        self.writer()
        self.git_scope = worker.isolated_git
        run = subprocess.run
        attempts = []
        def unavailable(command, **options):
            if len(command) > 1 and command[1] == "sandbox":
                attempts.append(command)
                raise FileNotFoundError("fixture sandbox unavailable")
            return run(command, **options)
        with patch.object(factory.subprocess, "run", side_effect=unavailable):
            receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(len(attempts), 1)
        self.assertIsNone(factory.GIT_EXECUTION.get())
        self.assertEqual(factory.git_commit(".", "HEAD"), self.candidate)
        self.assertEqual((self.repo / "feature.py").read_text(), "answer = 42\n")

    def test_callback_scope_cannot_expose_an_agent_or_write_shared_metadata(self):
        with patch.object(worker, "private_signer") as signer:
            with worker.isolated_git(self.args, self.identity, read_only=True):
                prefix, environment = factory.GIT_EXECUTION.get()
                command = prefix()
                name = command[command.index("--permission-profile") + 1]
                files = tomllib.loads(command[command.index("-c") + 1])["permissions"][name]["filesystem"]
                self.assertEqual(files[str(self.repo.resolve() / ".git")], "deny")
                self.assertEqual(files[environment["TMPDIR"]], "write")
                self.assertNotIn("SSH_AUTH_SOCK", environment)
                self.assertNotIn("--allow-unix-socket", command)
            with self.assertRaisesRegex(factory.FactoryError, "callback scope cannot expose"):
                with worker.isolated_git(self.args, self.identity, signing=True, read_only=True):
                    self.fail("Combined callback/signer scope entered")
            signer.assert_not_called()

    def test_precommit_index_edits_are_private_and_cannot_be_silently_ignored(self):
        self.writer()
        self.git_scope = worker.isolated_git
        hook = self.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\n" + shlex.quote(factory.git_binary()) + " reset -q HEAD -- feature.py\n")
        hook.chmod(0o700)
        self.assertEqual(factory.git_run(".", "config", "core.hooksPath", ".git/hooks").returncode, 0)
        run, staged_index = subprocess.run, []
        def transport(command, **options):
            if len(command) > 1 and command[1] == "sandbox":
                if "hook" in command:
                    staged_index.append((self.repo / ".git/index").read_bytes())
                command = command[command.index("--") + 1:]
            return run(command, **options)
        with patch.object(factory.subprocess, "run", side_effect=transport):
            receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("changed its private index", receipt["error"])
        self.assertEqual(len(staged_index), 1)
        self.assertEqual((self.repo / ".git/index").read_bytes(), staged_index[0])
        self.assertEqual(factory.git_commit(".", "HEAD"), self.candidate)

    def test_extra_commit_hooks_refuse_without_running_or_creating_a_commit(self):
        self.writer()
        effect = self.root / "unapproved-post-commit-effect"
        hook = self.repo / ".git/hooks/post-commit"
        hook.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(effect)!r}).write_text('called')\n")
        hook.chmod(0o700)
        self.assertEqual(factory.git_run(".", "config", "core.hooksPath", ".git/hooks").returncode, 0)
        receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("only the separately confined pre-commit", receipt["error"])
        self.assertFalse(effect.exists())
        self.assertEqual(factory.git_commit(".", "HEAD"), self.candidate)
        self.assertEqual(receipt["commit"]["state"], "preparing")

    def test_private_signer_retires_its_socket_on_success_or_rejected_key(self):
        original_agent = os.environ.get("SSH_AUTH_SOCK")
        for outcome in (0, 1):
            observed = {}
            def start(command, **options):
                socket = Path(command[command.index("-a") + 1])
                socket.touch()  # Fake agent transport; no keys or services.
                observed.update(command=command, socket=socket, options=options, pipe=BytesIO(), waited=False)
                def wait(**kwargs):
                    observed["waited"] = True
                    return 0
                return SimpleNamespace(stdin=observed["pipe"], poll=lambda: None, wait=wait)
            with patch.object(worker.shutil, "which", side_effect=lambda name, **kwargs: "/usr/bin/" + name), \
                    patch.object(worker.subprocess, "Popen", side_effect=start), \
                    patch.object(worker.subprocess, "run", return_value=subprocess.CompletedProcess([], outcome)) as load:
                if outcome:
                    with self.assertRaisesRegex(factory.FactoryError, "noninteractive signing"):
                        with worker.private_signer(self.root / "fixture-key", sys.executable):
                            self.fail("Rejected key must not yield signing access")
                else:
                    with worker.private_signer(self.root / "fixture-key", sys.executable) as socket:
                        self.assertFalse(Path(socket).is_relative_to(self.repo))
                        self.assertEqual(load.call_args.kwargs["env"]["SSH_AUTH_SOCK"], socket)
                self.assertIn("", observed["command"])
                self.assertIn("300", observed["command"])
                self.assertTrue(observed["pipe"].closed)
                self.assertTrue(observed["waited"])
                self.assertFalse(observed["socket"].exists())
                self.assertEqual(os.environ.get("SSH_AUTH_SOCK"), original_agent)

    def test_missing_private_signer_tools_refuse_before_starting_a_process(self):
        with patch.object(worker.shutil, "which", return_value=None), \
                patch.object(worker.subprocess, "Popen") as start, \
                self.assertRaisesRegex(factory.FactoryError, "Install OpenSSH"):
            with worker.private_signer(self.root / "fixture-key", sys.executable):
                self.fail("Missing signer must not yield")
        start.assert_not_called()

    def test_ssh_signing_grants_public_material_and_a_private_socket_only(self):
        public = self.root / "fixture-key.pub"
        public.write_text("ssh-ed25519 AAAAFIXTURE fixture\n")
        private = public.with_suffix("")
        private.write_text("test-only placeholder")
        for key, value in {"user.signingkey": str(public), "gpg.format": "ssh", "commit.gpgsign": "true"}.items():
            self.assertEqual(factory.git_run(".", "config", key, value).returncode, 0)
        @contextmanager
        def signer(key, python):
            self.assertEqual(key, private.resolve())
            yield str(self.root / "private-signer-socket")
        for platform in ("darwin", "linux"):
            with patch.object(worker.sys, "platform", platform), patch.object(worker, "private_signer", signer), \
                    worker.isolated_git(self.args, self.identity, signing=True):
                prefix, environment = factory.GIT_EXECUTION.get()
                command = prefix()
                name = command[command.index("--permission-profile") + 1]
                files = tomllib.loads(command[command.index("-c") + 1])["permissions"][name]["filesystem"]
                self.assertEqual(files[str(public.resolve())], "read")
                self.assertEqual(files[str(private.resolve())], "deny")
                self.assertEqual(files[environment["SSH_AUTH_SOCK"]], "read")
                self.assertEqual(files[str(self.repo.resolve() / ".git/config")], "read")
                self.assertEqual(files[str(self.repo.resolve() / ".git/hooks")], "read")
                self.assertEqual("--allow-unix-socket" in command, platform == "darwin")

    def test_factory_commit_detaches_finished_output_without_changing_its_alias(self):
        self.writer()
        original = self.spawn
        backup = self.root / "native-output-backup"
        async def linked_output(*command, **options):
            process = await original(*command, **options)
            os.link(self.repo / "feature.py", backup)
            return process
        self.spawn = linked_output
        receipt = self.execute()
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual(receipt["commit"]["state"], "created")
        source = self.repo / "feature.py"
        self.assertNotEqual(source.stat().st_ino, backup.stat().st_ino)
        self.assertEqual(source.stat().st_nlink, 1)
        self.assertEqual(backup.read_text(), "answer = 42\n")
        self.assertEqual(factory.git_run(".", "show", receipt["candidate"] + ":feature.py").stdout, "answer = 42\n")
        self.assertEqual(factory.git_run(".", "status", "--porcelain").stdout, "")

    def test_factory_commit_with_no_changes_does_not_create_an_empty_commit(self):
        self.args.write = True
        self.args.role = "backend"
        self.brief.write_text("# Assignment: backend\n\nKeep existing behavior.\n")
        self.args.brief_sha256 = hashlib.sha256(self.brief.read_bytes()).hexdigest()
        self.args.commit_message = "feat(example): Keep existing behavior"
        self.process = FakeProcess(events("completed"))
        receipt = self.execute()
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual(receipt["commit"]["state"], "unchanged")
        self.assertEqual(receipt["candidate"], self.candidate)
        worker.check_commit_receipt(receipt, self.args, str(self.repo))

    def test_unsuccessful_native_writer_does_not_commit_partial_changes(self):
        for index, (outcome, code) in enumerate((("completed", 1), ("blocked", 0), ("findings", 0))):
            self.args.events = self.root / f"failed-{index}.jsonl"
            self.args.receipt = self.root / f"failed-{index}.json"
            self.writer(outcome=outcome, exit_code=code)
            receipt = self.execute()
            self.assertEqual(receipt["state"], "failed")
            self.assertIsNone(receipt["commit"])
            self.assertEqual(factory.git_commit(".", "HEAD"), self.candidate)
            self.assertEqual((self.repo / "feature.py").read_text(), "answer = 42\n")
            (self.repo / "feature.py").unlink()

    def test_receipt_candidate_must_have_the_recorded_staged_tree(self):
        self.writer()
        receipt = self.execute()
        tree = factory.git_run(".", "rev-parse", self.candidate + "^{tree}").stdout.strip()
        sibling = factory.git_run(".", "commit-tree", tree, "-p", self.candidate, "-m", "Sibling fixture")
        self.assertEqual(sibling.returncode, 0, sibling.stderr)
        for state, candidate in (("created", sibling.stdout.strip()), ("unchanged", self.candidate)):
            damaged = json.loads(json.dumps(receipt))
            damaged["candidate"] = damaged["commit"]["candidate"] = candidate
            damaged["commit"]["state"] = state
            self.assertEqual(factory.git_run(".", "update-ref", "HEAD", candidate).returncode, 0)
            with self.subTest(state=state), self.assertRaisesRegex(factory.FactoryError, "staged tree differs"):
                worker.check_commit_receipt(damaged, self.args, str(self.repo))
        self.assertEqual(factory.git_run(".", "update-ref", "HEAD", receipt["candidate"]).returncode, 0)
        worker.check_commit_receipt(receipt, self.args, str(self.repo))

    def test_same_tree_sibling_cannot_replace_the_produced_commit(self):
        self.writer()
        receipt = self.execute()
        sibling = factory.git_run(".", "commit-tree", receipt["commit"]["staged_tree"],
                                  "-p", self.candidate, "-m", "Different sibling metadata")
        self.assertEqual(sibling.returncode, 0, sibling.stderr)
        damaged = json.loads(json.dumps(receipt))
        damaged["candidate"] = damaged["commit"]["candidate"] = sibling.stdout.strip()
        with self.assertRaisesRegex(factory.FactoryError, "assigned worktree HEAD"):
            worker.check_commit_receipt(damaged, self.args, str(self.repo))
        worker.check_commit_receipt(receipt, self.args, str(self.repo))

    def test_external_precommit_is_explicitly_unsupported_before_staging(self):
        self.writer()
        hooks = self.root / "external-hooks"
        hooks.mkdir()
        hook = hooks / "pre-commit"
        effect = self.root / "external-hook-effect"
        hook.write_text(f"#!/bin/sh\nprintf called > {shlex.quote(str(effect))}\n")
        hook.chmod(0o700)
        self.assertEqual(factory.git_run(".", "config", "core.hooksPath", str(hooks)).returncode, 0)
        before = (self.repo / ".git/index").read_bytes() if (self.repo / ".git/index").exists() else None
        receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("external hook paths", receipt["error"])
        self.assertEqual(receipt["commit"]["state"], "preparing")
        after = (self.repo / ".git/index").read_bytes() if (self.repo / ".git/index").exists() else None
        self.assertEqual(after, before)
        self.assertEqual(factory.git_commit(".", "HEAD"), self.candidate)
        self.assertFalse(effect.exists())

    def test_failed_commit_hook_retains_changes_and_never_retries(self):
        self.writer()
        hooks = self.repo / ".git/hooks"
        log = self.root / "hook-calls"
        hook = hooks / "pre-commit"
        hook.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nwith Path({str(log)!r}).open('a') as f: f.write('called\\n')\nraise SystemExit(1)\n")
        hook.chmod(0o700)
        subprocess.run(["git", "-C", str(self.repo), "config", "core.hooksPath", str(hooks)], check=True)
        receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("inspect the retained commit intent, Git HEAD", receipt["error"])
        self.assertEqual(receipt["commit"]["state"], "committing")
        self.assertEqual(factory.git_commit(".", "HEAD"), self.candidate)
        self.assertEqual((self.repo / "feature.py").read_text(), "answer = 42\n")
        with self.assertRaises(factory.FactoryError):
            self.execute()
        self.assertEqual(log.read_text(), "called\n")

    def test_lost_commit_response_preserves_uncertainty_without_a_second_commit(self):
        self.writer()
        git_run = factory.git_run
        def lost_reply(repository, *args, **options):
            result = git_run(repository, *args, **options)
            if "commit" in args:
                self.assertEqual(result.returncode, 0)
                raise factory.FactoryError("fixture lost response")
            return result
        with patch.object(factory, "git_run", side_effect=lost_reply):
            receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("outcome is uncertain", receipt["error"])
        self.assertEqual(receipt["commit"]["state"], "committing")
        candidate = factory.git_commit(".", "HEAD")
        self.assertNotEqual(candidate, self.candidate)
        with self.assertRaises(factory.FactoryError):
            self.execute()
        self.assertEqual(factory.git_commit(".", "HEAD"), candidate)
        self.assertEqual(len(self.spawns), 1)

    def test_factory_commit_works_from_an_admitted_subdirectory(self):
        self.writer()
        docs = self.repo / "docs"
        docs.mkdir()
        (docs / "contract.txt").write_text("Fixture contract\n")
        self.assertEqual(factory.git_run(".", "add", "--all").returncode, 0)
        self.assertEqual(factory.git_run(".", "commit", "-m", "Add contract fixture").returncode, 0)
        self.candidate = self.args.candidate = factory.git_commit(".", "HEAD")
        self.args.cwd = docs
        os.chdir(docs)
        receipt = self.execute()
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual(receipt["commit"]["parent"], self.candidate)
        self.assertEqual(factory.git_run(".", "show", receipt["candidate"] + ":feature.py").stdout, "answer = 42\n")
        self.assertEqual(factory.git_run(".", "status", "--porcelain").stdout, "")

    def test_configured_hook_imports_writer_edited_source(self):
        self.writer()
        helper = self.repo / "hook_helper.py"
        helper.write_text("def effect():\n    pass\n")
        hooks = self.repo / ".git/hooks"
        hook = hooks / "pre-commit"
        hook.write_text(f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path.cwd()))\nfrom hook_helper import effect\neffect()\n")
        hook.chmod(0o700)
        self.assertEqual(factory.git_run(".", "add", "--all").returncode, 0)
        self.assertEqual(factory.git_run(".", "commit", "-m", "Add authorized fixture hook").returncode, 0)
        self.candidate = self.args.candidate = factory.git_commit(".", "HEAD")
        subprocess.run(["git", "-C", str(self.repo), "config", "core.hooksPath", ".git/hooks"], check=True)
        output = []
        original_git = factory.git_run
        def observed(repository, *command, **options):
            result = original_git(repository, *command, **options)
            if "hook" in command:
                output.append(result.stdout + result.stderr)
            return result
        original_spawn = self.spawn
        async def changed(*command, **options):
            helper.write_text("def effect():\n    print('writer-edited source')\n")
            return await original_spawn(*command, **options)
        self.spawn = changed
        with patch.object(factory, "git_run", side_effect=observed):
            receipt = self.execute()
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertIn("writer-edited source", "".join(output))
        self.assertEqual(receipt["commit"]["state"], "created")
        self.assertNotEqual(receipt["candidate"], self.candidate)
        self.assertIn("workspace-write", self.spawns[0])
        self.assertFalse((self.repo / "__pycache__").exists())

    def test_active_transitive_filter_refuses_before_any_helper_execution(self):
        self.writer()
        tools = self.repo / "tools"
        tools.mkdir()
        (self.repo / ".gitattributes").write_text("*.audit filter=audit\n")
        (self.repo / "sample.audit").write_text("base\n")
        helper = tools / "filter_helper.py"
        helper.write_text("def clean(data):\n    return data\n")
        program = tools / "filter.py"
        program.write_text("import sys\nfrom filter_helper import clean\nsys.stdout.write(clean(sys.stdin.read()))\n")
        self.assertEqual(factory.git_run(".", "add", "--all").returncode, 0)
        self.assertEqual(factory.git_run(".", "commit", "-m", "Add authorized filter fixture").returncode, 0)
        self.candidate = self.args.candidate = factory.git_commit(".", "HEAD")
        subprocess.run(["git", "-C", str(self.repo), "config", "filter.audit.clean",
                        shlex.quote(sys.executable) + " -B tools/filter.py"], check=True)
        effect = self.root / "authorized-filter-intents"
        original_spawn = self.spawn
        async def changed(*command, **options):
            helper.write_text("import json\nfrom pathlib import Path\ndef clean(data):\n"
                f"    receipt = json.loads(Path({str(self.args.receipt)!r}).read_text())\n"
                f"    with Path({str(effect)!r}).open('a') as log: log.write(receipt['commit']['state'] + '\\n')\n"
                "    return data\n")
            (self.repo / "sample.audit").write_text("updated\n")
            return await original_spawn(*command, **options)
        self.spawn = changed
        receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("active Git filters", receipt["error"])
        self.assertFalse(effect.exists())
        self.assertEqual(factory.git_commit(".", "HEAD"), self.candidate)
        self.assertEqual(receipt["commit"]["state"], "preparing")

    def inherited_policy(self, kind, transport):
        self.writer()
        log = self.root / (kind + "-calls")
        program = self.root / (kind + " fixture")
        program.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nwith Path({str(log)!r}).open('a') as f: f.write('called\\n')\nraise SystemExit(1)\n")
        program.chmod(0o700)
        if kind == "hook":
            hooks = self.root / "hooks with spaces"
            hooks.mkdir()
            program.rename(hooks / "pre-commit")
            settings = {"core.hooksPath": str(hooks)}
            subprocess.run(["git", "-C", str(self.repo), "config", "--unset", "core.hooksPath"], check=True)
        else:
            settings = {"commit.gpgsign": "true", "gpg.format": "openpgp", "gpg.program": str(program)}
            subprocess.run(["git", "-C", str(self.repo), "config", "--unset", "commit.gpgsign"], check=True)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_CONFIG")}
        environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        if transport == "count":
            environment["GIT_CONFIG_COUNT"] = str(len(settings))
            for index, (key, value) in enumerate(settings.items()):
                environment[f"GIT_CONFIG_KEY_{index}"] = key
                environment[f"GIT_CONFIG_VALUE_{index}"] = value
        elif transport == "parameters":
            def quoted(value):
                return "'" + value.replace("'", "'\\''") + "'"
            environment["GIT_CONFIG_PARAMETERS"] = " ".join(quoted(key + "=" + value) for key, value in settings.items())
        else:
            config = self.root / "policy.config"
            for key, value in settings.items():
                subprocess.run(["git", "config", "--file", str(config), "--add", key, value], check=True)
            environment["GIT_CONFIG_GLOBAL"] = str(config)
        with patch.dict(os.environ, environment, clear=True):
            receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("configured hooks/signing", receipt["error"])
        self.assertFalse(log.exists())
        self.assertEqual(factory.git_commit(".", "HEAD"), self.candidate)
        self.assertEqual((self.repo / "feature.py").read_text(), "answer = 42\n")

    def test_count_supplied_hook_is_enforced(self):
        self.inherited_policy("hook", "count")

    def test_parameter_supplied_hook_is_enforced(self):
        self.inherited_policy("hook", "parameters")

    def test_global_file_supplied_hook_is_enforced(self):
        self.inherited_policy("hook", "global")

    def test_count_supplied_signing_is_enforced(self):
        self.inherited_policy("signing", "count")

    def test_parameter_supplied_signing_is_enforced(self):
        self.inherited_policy("signing", "parameters")

    def test_global_file_supplied_signing_is_enforced(self):
        self.inherited_policy("signing", "global")

    def test_factory_commit_refuses_source_symlinks_before_creating_a_commit(self):
        outside = self.root / "outside.txt"
        outside.write_text("original\n")
        self.writer()
        async def spawn(*command, **options):
            (self.repo / "source-link").symlink_to(outside)
            return self.process
        self.spawn = spawn
        receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("linked or non-regular source", receipt["error"])
        self.assertEqual(factory.git_commit(".", "HEAD"), self.candidate)
        self.assertEqual(factory.git_run(".", "diff", "--cached", "--quiet").returncode, 0)
        self.assertEqual(outside.read_text(), "original\n")

    def test_replaced_source_directory_cannot_materialize_an_external_hard_link(self):
        self.writer()
        (self.repo / ".gitignore").write_text("docs\n")
        directory = self.repo / "docs"
        directory.mkdir()
        (directory / "source.py").write_text("value = 1\n")
        self.assertEqual(factory.git_run(".", "add", "--all").returncode, 0)
        self.assertEqual(factory.git_run(".", "add", "--force", "docs/source.py").returncode, 0)
        self.assertEqual(factory.git_run(".", "commit", "-m", "Add directory fixture").returncode, 0)
        self.candidate = self.args.candidate = factory.git_commit(".", "HEAD")
        outside = self.root / "host-directory"
        outside.mkdir()
        target = outside / "source.py"
        target.write_text("host value\n")
        alias = self.root / "host-alias"
        os.link(target, alias)
        inode = target.stat().st_ino
        async def replaced(*command, **options):
            (directory / "source.py").unlink()
            directory.rmdir()
            directory.symlink_to(outside, target_is_directory=True)
            return self.process
        self.spawn = replaced
        receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("symlinked source directory", receipt["error"])
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(target.stat().st_nlink, 2)
        self.assertEqual(alias.read_text(), "host value\n")
        self.assertEqual(list(outside.iterdir()), [target])
        self.assertEqual(factory.git_commit(".", "HEAD"), self.candidate)
        self.assertEqual(factory.git_run(".", "diff", "--cached", "--quiet").returncode, 0)

    def test_bad_commit_message_refuses_before_task_locks_or_execution_artifacts(self):
        for write, message in ((False, "A message"), (True, " "), (True, "\nNo subject"),
                               (True, "x" * 73), (True, "bad\0message")):
            self.args.write, self.args.commit_message = write, message
            with self.subTest(message=message), self.assertRaisesRegex(factory.FactoryError, "commit subject"):
                self.execute()
            state = self.root / "never-created"
            argv = ["--state-dir", str(state), "codex-start", "--task", "task", "--id", "writer",
                    "--role", "backend", "--goal", "Implement", "--cwd", str(self.repo),
                    "--codex", sys.executable, "--model", "fixture", "--effort", "low", "--commit-message", message]
            if write:
                argv.append("--write")
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(factory.main(argv), 1)
            self.assertFalse(state.exists())
            self.assertFalse(self.args.receipt.exists())
            self.assertFalse(self.args.events.exists())
            self.assertEqual(self.spawns, [])

    def test_other_native_duplicate_fields_still_refuse_decoding(self):
        for raw in (
            '{"type":"thread.started","thread_id":"first","thread_id":"second"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"first","text":"second"}}',
            '{"type":"turn.completed","usage":{"input_tokens":1,"input_tokens":2,"output_tokens":3}}',
            '{"type":"item.started","item":{"type":"web_search","id":"one","id":"two","query":"first","query":"second"}}',
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                json.loads(raw, object_pairs_hook=worker.native_object)

    def test_spawn_failure_retains_an_actionable_failed_receipt(self):
        async def unavailable(*args, **kwargs):
            raise FileNotFoundError("Fixture executable disappeared")
        receipt = asyncio.run(worker.execute(self.args, spawn=unavailable, clock=lambda: 1000, timer=lambda: 10))
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("restore executable access/local process resources", receipt["error"])
        self.assertIsNone(receipt["exit_code"])
        self.assertIsNone(receipt["report"])
        self.assertEqual(json.loads(self.args.receipt.read_text()), receipt)

    def test_native_pipe_failure_retains_recovery_and_stops_owned_child(self):
        with patch.object(self.process, "readline", side_effect=ConnectionResetError("raw fixture errno")):
            receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("inspect the retained stream, storage and Rex pane", receipt["error"])
        self.assertNotIn("raw fixture errno", receipt["error"])
        self.assertIsNone(receipt["report"])
        self.assertIsNotNone(self.process.returncode)
        self.assertEqual(json.loads(self.args.receipt.read_text()), receipt)

    def test_exit_before_cleanup_kill_still_saves_the_final_receipt(self):
        class RaceProcess(FakeProcess):
            waits = 0
            async def wait(self):
                self.waits += 1
                if self.waits == 1:
                    raise TimeoutError("Fixture cleanup deadline")
                self.returncode = 0
                return 0
            def kill(self):
                raise ProcessLookupError("Already exited")
        self.process = RaceProcess(events(), timeout=True)
        receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(self.process.waits, 2)
        self.assertEqual(json.loads(self.args.receipt.read_text()), receipt)

    def test_zero_exit_without_result_is_failed_and_never_retried(self):
        self.process = FakeProcess(events()[:2])
        receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIsNone(receipt["report"])
        self.assertIn("completion", receipt["error"])
        with self.assertRaises(factory.FactoryError):
            self.execute()
        self.assertEqual(len(self.spawns), 1)

    def test_malformed_native_lines_retain_evidence_and_explain_recovery(self):
        for index, line in enumerate((b'{"truncated":\n', b'\xff\n',
                b'{"type":"thread.started","thread_id":"fixture","unused":[NaN]}\n',
                b'{"type":"thread.started","thread_id":"fixture","unused":{"n":Infinity}}\n',
                b'{"type":"thread.started","thread_id":"fixture","unused":-Infinity}\n',
                b'{"type":"thread.started","thread_id":"fixture","unused":1e999}\n',
                ('{"type":"thread.started","thread_id":"fixture","unused":{"n":' + str(10 ** 400) + '}}\n').encode())):
            self.args.events = self.root / f"bad-{index}.jsonl"
            self.args.receipt = self.root / f"bad-{index}.json"
            self.process = FakeProcess([])
            self.process.lines = iter([line])
            receipt = self.execute()
            self.assertEqual(receipt["state"], "failed")
            self.assertIn("inspect the retained stream and update the adapter", receipt["error"])
            self.assertIsNone(receipt["report"])
            record = json.loads(self.args.events.read_text())
            self.assertEqual(record["data"].encode("utf-8", errors="surrogateescape"), line)
            with self.assertRaisesRegex(worker.WorkerError, "Malformed native events; inspect"):
                worker.read_events(self.args.events)

    def test_timeout_stops_only_owned_child_and_retains_failed_attempt(self):
        self.process = FakeProcess(events()[:2], exit_code=-2, timeout=True)
        receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("allowance expired", receipt["error"])
        self.assertEqual(len(self.process.signals), 1)
        self.assertEqual(receipt["exit_code"], -2)
        self.assertEqual(receipt["thread_id"], "thread-fixture")
        self.assertTrue(self.args.events.exists())

    def test_changed_brief_and_api_override_refuse_before_native_spawn(self):
        self.brief.write_text("Changed instructions")
        with self.assertRaisesRegex(factory.FactoryError, "brief changed"):
            self.execute()
        self.assertEqual(self.spawns, [])
        self.assertFalse(self.args.events.exists())
        with patch.dict(os.environ, {"OPENAI_API_KEY": "not-a-real-key"}):
            with self.assertRaisesRegex(factory.FactoryError, "API-key overrides"):
                self.execute()

    def test_unrepresentable_allowance_refuses_before_creating_execution_artifacts(self):
        for seconds in (0, -1, True, factory.MAX_NATIVE_SECONDS + 1, 10 ** 400):
            self.args.seconds = seconds
            with self.subTest(seconds=seconds), self.assertRaisesRegex(factory.FactoryError,
                    "execution allowance of at most 86400 seconds"):
                self.execute()
            self.assertEqual(self.spawns, [])
            for path in (self.args.events, self.args.receipt, self.args.receipt.with_suffix('.schema.json')):
                self.assertFalse(path.exists())

    def test_missing_native_binary_and_damaged_brief_identify_preflight_recovery(self):
        original = self.args.codex
        self.args.codex = self.root / "missing-native"
        with self.assertRaisesRegex(factory.FactoryError, "Cannot access native CLI .*inspect/restore the saved executable"):
            self.execute()
        self.args.codex = original
        for data in (None, b'\xff'):
            if data is None:
                self.brief.unlink()
            else:
                self.brief.write_bytes(data)
            with self.assertRaisesRegex(factory.FactoryError, "Cannot read original brief .*inspect/restore the saved assignment input"):
                self.execute()
        self.assertEqual(self.spawns, [])
        self.assertFalse(self.args.receipt.exists())
        self.assertFalse(self.args.events.exists())

    def test_dirty_checkout_after_native_exit_cannot_be_verification(self):
        async def changed(*command, **options):
            (self.repo / "uncommitted.txt").write_text("Uncommitted changes")
            return self.process
        with redirect_stdout(StringIO()):
            receipt = asyncio.run(worker.execute(self.args, spawn=changed,
                                                 clock=lambda: 1000.0, timer=lambda: 10.0))
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("clean", receipt["error"])

    def test_invalid_streams_and_reports_cannot_be_registered(self):
        malformed = [events()[1:], events() + [dict(type="turn.completed")],
                     events() + [dict(type="turn.failed")], [None],
                     events() + [dict(type="item.completed", item=None)]]
        for change in (dict(artifacts=[""]), dict(outcome="accepted")):
            payload = events()
            report = dict(outcome="approved", artifacts=["evidence.txt"])
            payload[-2]["item"]["text"] = json.dumps({**report, **change})
            malformed.append(payload)
        payload = events()
        payload[-1]["usage"] = dict(input_tokens=True, output_tokens=0)
        malformed.append(payload)
        for payload in malformed:
            with self.subTest(payload=payload), self.assertRaises(worker.WorkerError):
                worker.parse_events(payload)
        payload = events()
        del payload[-1]["usage"]
        self.assertIsNone(worker.parse_events(payload)["usage"])

    def replace_with_clone(self):
        preserved = self.root / "original-repository"
        self.repo.rename(preserved)
        subprocess.run(["git", "clone", "--quiet", str(preserved), str(self.repo)], check=True)

    def test_replaced_history_sharing_checkout_refuses_before_worker_spawn(self):
        self.replace_with_clone()
        self.assertEqual(factory.git_commit(str(self.repo), "HEAD"), self.candidate)
        with self.assertRaisesRegex(factory.FactoryError, "repository marker"):
            self.execute()
        self.assertEqual(self.spawns, [])
        self.assertFalse(self.args.receipt.exists())

    def test_repository_replacement_during_execution_cannot_supply_a_completed_receipt(self):
        async def replaced(*command, **options):
            self.replace_with_clone()
            return self.process
        with redirect_stdout(StringIO()):
            receipt = asyncio.run(worker.execute(self.args, spawn=replaced))
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["repository_identity"], self.identity)
        self.assertIn("repository identity differs", receipt["error"])

    def test_readonly_roles_refuse_writer_commands_and_prebuilt_writer_launches(self):
        path = self.root / "task.json"
        factory.admit(path, task="task", goal="Independent verification", repository=str(self.repo),
                      acceptance=["fixture/acceptance"])
        before = path.read_bytes()
        for role in ("code-review", "qa", "investigator", "judge", "standby-reasoner", "observer"):
            with self.subTest(role=role):
                with self.assertRaisesRegex(factory.FactoryError, "specialty does not permit writer"):
                    factory.codex_command(path, task="task", assignment_id="review", codex=sys.executable,
                        model="fixture", effort="high", cwd=str(self.repo), write=True,
                        commit_message="Unauthorized verifier edit", role=role)
                command = factory.codex_command(path, task="task", assignment_id="review", codex=sys.executable,
                    model="fixture", effort="high", cwd=str(self.repo), write=True,
                    commit_message="Authorized implementation", role="backend")
                with self.assertRaisesRegex(factory.FactoryError, "specialty does not permit writer"):
                    factory.launch(path, task="task", assignment_id="review", role=role,
                        goal="Independent verification", cwd=str(self.repo), command=command, purpose="work",
                        binary=sys.executable, server="unix:///test.sock")
                self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / "task.review.md").exists())

    def launch_assignment(self, codex=sys.executable, *, write=False, commit_message=None, execute=True, legacy=False):
        self.path = self.root / "task.json"
        factory.admit(self.path, task="task", goal="Verify the change", repository=str(self.repo),
                      acceptance=["docs/acceptance.md"])
        command = factory.codex_command(self.path, task="task", assignment_id="qa", codex=codex,
                                        model="fixture-model", effort="high", cwd=str(self.repo),
                                        write=write, commit_message=commit_message, role="backend" if write else "qa")
        if legacy:
            del command[1:3]
        FakeRex.sessions, FakeRex.calls = [], []
        FakeRex.fail_after_create, FakeRex.host = False, "test-host"
        with patch.object(factory, "Rex", FakeRex):
            record, assignment = factory.launch(self.path, task="task", assignment_id="qa", role="backend" if write else "qa",
                goal="Review the change", cwd=str(self.repo), command=command, purpose="work",
                binary=sys.executable, server="unix:///test.sock")
        actual = next(call for call in FakeRex.calls if call[0] == "new")
        argv = actual[actual.index("--") + 1:]
        self.worker_argv = argv
        self.assertEqual(argv[:4], (sys.executable, "-E", "-s", str(factory.ROOT / "checkout_exec.py")))
        argv = argv[argv.index("--") + 1:]
        self.args = worker.parser().parse_args(argv[2:] if legacy else argv[4:])
        self.assertEqual(self.args.candidate, self.candidate)
        self.assertEqual(self.args.brief_sha256, assignment["request"]["instructions_sha256"])
        self.assertEqual(json.loads(self.args.repo_identity), record["admission"]["repository_identity"])
        if execute:
            self.execute()
        self.rex = FakeRex(sys.executable, "unix:///test.sock")
        return record, assignment

    def test_collection_rejects_a_replacement_linked_worktree_at_the_same_path(self):
        main = self.repo
        linked = self.root / "assigned"
        self.assertEqual(factory.git_run(str(main), "worktree", "add", "--detach", str(linked), "HEAD").returncode, 0)
        self.repo = linked
        self.writer()
        message = self.args.commit_message
        record, assignment = self.launch_assignment(write=True, commit_message=message, execute=False)
        os.chdir(linked)
        receipt = self.execute()
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        sibling = factory.git_run(str(main), "commit-tree", receipt["commit"]["staged_tree"],
                                  "-p", self.candidate, "-m", "Substitute sibling")
        self.assertEqual(sibling.returncode, 0)
        os.chdir(main)
        self.assertEqual(factory.git_run(str(main), "worktree", "move", str(linked), str(self.root / "retained")).returncode, 0)
        self.assertEqual(factory.git_run(str(main), "worktree", "add", "--detach", str(linked), sibling.stdout.strip()).returncode, 0)
        damaged = copy.deepcopy(receipt)
        damaged["candidate"] = damaged["commit"]["candidate"] = sibling.stdout.strip()
        self.args.receipt.write_text(json.dumps(damaged))
        before = self.path.read_bytes()
        with patch.object(factory, "status", return_value=dict(state="exited")):
            with self.assertRaisesRegex(factory.FactoryError, "worktree binding differs"):
                factory.collect_codex(self.path, "qa", self.rex, record, assignment)
        self.assertEqual(self.path.read_bytes(), before)
        legacy = copy.deepcopy(receipt)
        legacy["schema_version"] = 4
        del legacy["commit"]
        args = SimpleNamespace(**{**vars(self.args), "commit_message": None})
        with self.assertRaisesRegex(factory.FactoryError, "worktree binding differs"):
            worker.check_commit_receipt(legacy, args, str(main))

    def test_writer_receipts_without_a_factory_commit_cannot_substitute_the_candidate(self):
        self.process = FakeProcess(events("completed"))
        record, assignment = self.launch_assignment(write=True)
        original = json.loads(self.args.receipt.read_text())
        self.assertIsNone(original["commit"])
        tree = factory.git_run(".", "rev-parse", self.candidate + "^{tree}").stdout.strip()
        sibling = factory.git_run(".", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                  "-c", "commit.gpgsign=false", "commit-tree", tree,
                                  "-p", self.candidate, "-m", "Substituted candidate")
        self.assertEqual(sibling.returncode, 0, sibling.stderr)
        before = self.path.read_bytes()
        for version in (4, 5):
            with self.subTest(version=version):
                damaged = copy.deepcopy(original)
                damaged.update(schema_version=version, candidate=sibling.stdout.strip())
                if version == 4:
                    del damaged["commit"]
                self.args.receipt.write_text(json.dumps(damaged))
                with patch.object(factory, "status", return_value=dict(state="exited")):
                    with self.assertRaisesRegex(factory.FactoryError, "assigned worktree HEAD"):
                        factory.collect_codex(self.path, "qa", self.rex, record, assignment)
                self.assertEqual(self.path.read_bytes(), before)
        self.args.receipt.write_text(json.dumps(original))
        with patch.object(factory, "status", return_value=dict(state="exited")):
            result = factory.collect_codex(self.path, "qa", self.rex, record, assignment)
        self.assertEqual(result["assignments"]["qa"]["result"]["candidate"], self.candidate)

    def test_factory_writer_collection_binds_the_produced_commit_without_approving_it(self):
        self.writer()
        message = self.args.commit_message
        record, assignment = self.launch_assignment(write=True, commit_message=message)
        original = json.loads(self.args.receipt.read_text())
        before = self.path.read_bytes()
        for change in (dict(state="committing"), dict(parent="a" * 40), dict(message="Changed"),
                       dict(staged_tree=True), dict(candidate=self.candidate)):
            damaged = copy.deepcopy(original)
            damaged["commit"].update(change)
            self.args.receipt.write_text(json.dumps(damaged))
            with patch.object(factory, "status", return_value=dict(state="exited")), self.assertRaisesRegex(
                    factory.FactoryError, "commit evidence"):
                factory.collect_codex(self.path, "qa", self.rex, record, assignment)
            self.assertEqual(self.path.read_bytes(), before)
        self.args.receipt.write_text(json.dumps(original))
        with patch.object(factory, "status", return_value=dict(state="exited")):
            result = factory.collect_codex(self.path, "qa", self.rex, record, assignment)
        produced = result["assignments"]["qa"]["result"]
        self.assertEqual(produced["outcome"], "completed")
        self.assertEqual(produced["candidate"], original["candidate"])
        self.assertFalse(factory.result_current(result, produced))
        selected = factory.set_inputs(self.path, candidate=produced["candidate"],
                                      requirements=produced["inputs"]["requirements"])
        self.assertTrue(factory.result_current(selected, produced))
        self.assertEqual(len(self.spawns), 1)

    def test_historical_worker_command_reconnects_and_collects_unchanged(self):
        record, assignment = self.launch_assignment(legacy=True)
        frozen = factory.codex_command(self.path, task="task", assignment_id="qa", codex=sys.executable,
                                        model="fixture-model", effort="high", cwd=str(self.repo))
        self.assertEqual(frozen, assignment["request"]["command"])
        original = self.args.receipt.read_bytes()
        with patch.object(factory, "status", return_value=dict(state="exited")):
            result = factory.collect_codex(self.path, "qa", self.rex, record, assignment)
        self.assertEqual(result["assignments"]["qa"]["result"]["outcome"], "approved")
        self.assertEqual(self.args.receipt.read_bytes(), original)
        self.assertEqual(len(self.spawns), 1)

    def test_version_four_receipt_remains_collectable_without_migration(self):
        # Independent old-format fixture: never obtain it from the current writer.
        path = self.root / "task.json"
        factory.admit(path, task="task", goal="Inspect legacy evidence", repository=str(self.repo),
                      acceptance=["fixture/legacy-acceptance.md"])
        factory.set_inputs(path, candidate=self.candidate, requirements="requirements-1")
        stream, receipt = self.root / "task.qa.codex.jsonl", self.root / "task.qa.codex.json"
        binary, gitdir = str(Path(sys.executable).resolve()), str(factory.git_dir(str(self.repo)))
        command = [sys.executable, str(factory.ROOT / "codex_worker.py"), "--job", "task/qa",
            "--codex", binary, "--model", "fixture-model", "--effort", "high", "--seconds", "600",
            "--repo", "{repository}", "--cwd", "{cwd}", "--brief", "{brief}",
            "--brief-sha256", "{brief_sha256}", "--candidate", "{candidate}",
            "--requirements", "{requirements}", "--events", str(stream), "--receipt", str(receipt),
            "--repo-identity", "{repository_identity}", "--worktree-git-dir", gitdir]
        FakeRex.sessions, FakeRex.calls = [], []
        FakeRex.fail_after_create, FakeRex.host = False, "test-host"
        with patch.object(factory, "Rex", FakeRex):
            record, assignment = factory.launch(path, task="task", assignment_id="qa", role="qa",
                goal="Review legacy candidate", cwd=str(self.repo), command=command, purpose="work",
                binary=sys.executable, server="unix:///test.sock")
        report = {"outcome": "approved", "artifacts": ["fixture/legacy-qa.txt"]}
        usage = {"input_tokens": 17, "output_tokens": 3}
        native = [{"type": "thread.started", "thread_id": "legacy-thread"}, {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(report)}},
            {"type": "turn.completed", "usage": usage}]
        stream.write_text("".join(json.dumps({"schema_version": 1, "kind": "codex-event-line",
            "data": json.dumps(event) + "\n"}) + "\n" for event in native))
        legacy = {"schema_version": 4, "kind": "codex-execution", "job": "task/qa", "state": "completed",
            "inputs": {"candidate": self.candidate, "requirements": "requirements-1"},
            "repository_identity": self.identity, "worktree_git_dir": gitdir,
            "brief_sha256": hashlib.sha256(Path(assignment["brief"]).read_bytes()).hexdigest(),
            "cwd": str(self.repo.resolve()), "requested_model": "fixture-model", "requested_effort": "high",
            "binary": binary, "writer": False, "created_at": 1000.0, "finished_at": 1001.0,
            "elapsed_seconds": 1.0, "exit_code": 0, "thread_id": "legacy-thread", "report": report,
            "usage": usage, "candidate": self.candidate, "events": str(stream), "error": None}
        receipt.write_text(json.dumps(legacy))
        original, original_stream = receipt.read_bytes(), stream.read_bytes()
        frozen = factory.codex_command(path, task="task", assignment_id="qa", codex=binary,
            model="fixture-model", effort="high", cwd=str(self.repo))
        self.assertEqual(frozen, command)
        with patch.object(factory, "status", return_value=dict(state="exited")):
            result = factory.collect_codex(path, "qa", FakeRex(sys.executable, "unix:///test.sock"), record, assignment)
        self.assertEqual(result["assignments"]["qa"]["result"]["outcome"], "approved")
        self.assertEqual(receipt.read_bytes(), original)
        self.assertEqual(stream.read_bytes(), original_stream)
        self.assertEqual(self.spawns, [])

    def test_workspace_python_startup_code_cannot_run_in_the_host_worker(self):
        effect = self.root / "startup-effect"
        (self.repo / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(effect)!r}).write_text('ran')\n")
        self.assertEqual(factory.git_run(".", "add", "--all").returncode, 0)
        self.assertEqual(factory.git_run(".", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                                             "commit", "-m", "Add startup fixture").returncode, 0)
        self.candidate = factory.git_commit(".", "HEAD")
        binary = self.root / "native-fixture"
        payload_events = events()
        payload_events.insert(2, dict(type="item.completed", item=dict(type="command_execution", aggregated_output="x" * 100000)))
        payload = "\n".join(json.dumps(event) for event in payload_events)
        binary.write_text("#!/bin/bash\nset -euo pipefail\nprintf '%s\\n' " + shlex.quote(payload) + "\n")
        binary.chmod(0o700)
        self.launch_assignment(codex=str(binary), execute=False)
        engine = self.root / "engine"
        engine.mkdir()
        for name in ("checkout_exec.py", "codex_worker.py", "factory.py"):
            (engine / name).write_bytes((factory.ROOT / name).read_bytes())
        command = [str(engine / Path(arg).name) if arg in {
            str(factory.ROOT / "checkout_exec.py"), str(factory.ROOT / "codex_worker.py")} else arg for arg in self.worker_argv]
        result = subprocess.run(command, capture_output=True, text=True, timeout=15,
            env={**os.environ, "PYTHONPATH": str(self.repo), "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(effect.exists())
        self.assertFalse((engine / "__pycache__").exists())
        self.assertEqual(json.loads(self.args.receipt.read_text())["state"], "completed")
        self.assertEqual(worker.read_events(self.args.events), payload_events)

    def test_oversize_native_event_retains_failed_attempt_without_retry(self):
        with patch.object(self.process, "readline", side_effect=ValueError("Separator is found, but chunk is longer than limit")):
            receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("exceeded the 1 MiB line limit", receipt["error"])
        self.assertIsNotNone(self.process.returncode)
        self.assertIsNone(receipt["report"])
        self.assertEqual(json.loads(self.args.receipt.read_text()), receipt)
        with self.assertRaises(factory.FactoryError):
            self.execute()
        self.assertEqual(len(self.spawns), 1)

    def test_native_preflight_preserves_allowances_and_reconnect_survives_removed_binary(self):
        path = self.root / "task.json"
        factory.admit(path, task="task", goal="Verify the change", repository=str(self.repo),
                      acceptance=["docs/acceptance.md"])
        before = path.read_bytes()
        for binary in (self.root / "missing", self.root, self.brief):
            with self.subTest(binary=binary), self.assertRaises(factory.FactoryError):
                factory.codex_command(path, task="task", assignment_id="qa", codex=str(binary),
                                      model="fixture-model", effort="high", cwd=str(self.repo))
            self.assertEqual(path.read_bytes(), before)
        binary = self.root / "native-fixture"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o700)
        record, assignment = self.launch_assignment(codex=str(binary))
        binary.unlink()
        command = factory.codex_command(path, task="task", assignment_id="qa", codex=str(binary),
                                        model="fixture-model", effort="high", cwd=str(self.repo))
        self.assertEqual(command, assignment["request"]["command"])
        self.assertEqual(factory.load_task(path), record)

    def test_native_selection_freezes_symlink_target(self):
        binary = self.root / "native-fixture"
        binary.symlink_to(sys.executable)
        command = factory.codex_command(self.root / "task.json", task="task", assignment_id="qa",
                                        codex=str(binary), model="fixture-model", effort="high", cwd=str(self.repo))
        chosen = command[command.index("--codex") + 1]
        binary.unlink()
        binary.symlink_to("/bin/false")
        self.assertEqual(chosen, str(Path(sys.executable).resolve()))
        self.assertNotEqual(chosen, str(binary.resolve()))

    def test_blank_native_selection_refuses_before_reading_or_mutating_task(self):
        path = self.root / "task.json"
        record = factory.admit(path, task="task", goal="Verify the change", repository=str(self.repo),
                               acceptance=["docs/acceptance.md"], corrections=1, investigations=1)
        before = path.read_bytes()
        for model, effort in (("", "high"), ("  ", "high"), ("fixture-model", ""), ("fixture-model", "\t")):
            with self.subTest(model=model, effort=effort, cwd=str(self.repo)), patch.object(factory, "load_task") as read:
                with self.assertRaisesRegex(factory.FactoryError, "discovered native model and supported effort"):
                    factory.codex_command(path, task="task", assignment_id="qa", codex=sys.executable,
                                          model=model, effort=effort, cwd=str(self.repo))
                read.assert_not_called()
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(factory.task_view(record)["allowances"]["correction"]["remaining"], 1)

    def test_damaged_saved_native_arguments_name_repair_without_collection(self):
        record, assignment = self.launch_assignment()
        command = assignment["request"]["command"]
        at = command.index("--model")
        del command[at:at + 2]
        factory.save(self.path, record)
        factory.load_task(self.path)
        before = self.path.read_bytes()
        with redirect_stderr(StringIO()), self.assertRaisesRegex(factory.FactoryError,
                "restore the original assignment arguments.*without relaunching"):
            factory.collect_codex(self.path, "qa", self.rex, record, assignment)
        self.assertEqual(self.path.read_bytes(), before)

    def test_public_cli_blank_selection_does_not_upgrade_legacy_task(self):
        path = self.root / "task.json"
        record = factory.admit(path, task="task", goal="Verify the change", repository=str(self.repo),
                               acceptance=["docs/acceptance.md"], corrections=1, investigations=1)
        record["schema_version"] = 3
        record["admission"]["repository_identity"] = dict(
            common_dir=str(self.repo / ".git"), device=1, inode=2)
        factory.save(path, record)
        before = path.read_bytes()
        for model, effort in ((" ", "high"), ("fixture-model", "\t")):
            errors = StringIO()
            with self.subTest(model=model, effort=effort, cwd=str(self.repo)), redirect_stderr(errors), redirect_stdout(StringIO()):
                code = factory.main(["--state-dir", str(self.root), "codex-start", "--task", "task",
                    "--id", "qa", "--role", "qa", "--goal", "Inspect only", "--cwd", str(self.repo),
                    "--codex", sys.executable, "--model", model, "--effort", effort, "--purpose", "correction"])
            self.assertEqual(code, 1)
            self.assertIn("discovered native model and supported effort", errors.getvalue())
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(path.with_suffix(".lock").exists())

    def test_public_cli_path_and_allowance_errors_do_not_upgrade_legacy_task(self):
        path = self.root / "task.json"
        record = factory.admit(path, task="task", goal="Verify the change", repository=str(self.repo),
                               acceptance=["docs/acceptance.md"], corrections=1, investigations=1)
        record["schema_version"] = 3
        record["admission"]["repository_identity"] = dict(
            common_dir=str(self.repo / ".git"), device=1, inode=2)
        factory.save(path, record)
        before = path.read_bytes()
        for codex, seconds in (("relative-codex", "60"), (sys.executable, "0"), (sys.executable, "-1"),
                               (sys.executable, str(factory.MAX_NATIVE_SECONDS + 1)), (sys.executable, str(10 ** 400))):
            errors = StringIO()
            with self.subTest(codex=codex, seconds=seconds), redirect_stderr(errors), redirect_stdout(StringIO()):
                code = factory.main(["--state-dir", str(self.root), "codex-start", "--task", "task",
                    "--id", "qa", "--role", "qa", "--goal", "Inspect only", "--cwd", str(self.repo),
                    "--codex", codex, "--model", "fixture-model", "--effort", "high",
                    "--seconds", seconds, "--purpose", "correction"])
            self.assertEqual(code, 1)
            self.assertIn("absolute native Codex path and a positive execution allowance", errors.getvalue())
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(path.with_suffix(".lock").exists())

    def test_another_linked_worktree_cannot_run_or_supply_a_writer_result(self):
        other = self.root / "other-worktree"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "--quiet", "--detach", str(other), "HEAD"], check=True)
        self.args.write = True
        self.args.role = "backend"
        self.brief.write_text("# Assignment: backend\n\nDeliver the fixture change.\n")
        self.args.brief_sha256 = hashlib.sha256(self.brief.read_bytes()).hexdigest()
        os.chdir(other)
        try:
            with self.assertRaisesRegex(factory.FactoryError, "Assignment worktree differs"):
                self.execute()
        finally:
            os.chdir(self.repo)
        self.assertEqual(self.spawns, [])
        self.assertFalse(self.args.events.exists())
        self.assertFalse(self.args.receipt.exists())

        async def changed_actual_directory(*command, **options):
            os.chdir(other)
            return self.process
        try:
            with redirect_stdout(StringIO()):
                receipt = asyncio.run(worker.execute(self.args, spawn=changed_actual_directory,
                    clock=lambda: 1000.0, timer=lambda: 10.0))
        finally:
            os.chdir(self.repo)
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("Assignment worktree differs", receipt["error"])
        self.assertEqual(receipt["worktree_git_dir"], str(factory.git_dir(str(self.repo))))

    def test_old_worktree_unbound_command_reconnects_but_cannot_collect(self):
        record, assignment = self.launch_assignment()
        command = assignment["request"]["command"]
        index = command.index("--worktree-git-dir")
        del command[index:index + 2]
        factory.save(self.path, record)
        frozen = factory.codex_command(self.path, task="task", assignment_id="qa", codex=sys.executable,
            model="fixture-model", effort="high", cwd=str(self.repo))
        self.assertEqual(frozen, command)
        with self.assertRaisesRegex(factory.FactoryError, "predates worktree identity"):
            factory.collect_codex(self.path, "qa", self.rex, record, assignment)

    def test_reordered_native_lifecycle_cannot_register_a_result(self):
        record, assignment = self.launch_assignment()
        original = events()
        orders = ((0, 4, 3, 1), (0, 3, 1, 4), (0, 1, 4, 3), (1, 0, 3, 4),
                  (0, 1, 3, 4, 1), (0, 1, 3, 4, 3), (0, 1, 1, 3, 4), (0, 1, 3, 4, 4))
        before = self.path.read_bytes()
        for order in orders:
            stream = [original[index] for index in order]
            self.args.events.write_text("".join(json.dumps(dict(schema_version=1, kind="codex-event-line",
                data=json.dumps(event) + "\n")) + "\n" for event in stream))
            with self.subTest(order=order), patch.object(self.rex, "call", return_value={"last_exit": dict(exit_code=0)}):
                with self.assertRaises(worker.WorkerError):
                    factory.collect_codex(self.path, "qa", self.rex, record, assignment)
            self.assertEqual(self.path.read_bytes(), before)

    def test_nonfatal_startup_diagnostics_do_not_supply_lifecycle_evidence(self):
        stream = events()
        diagnostic = dict(type="item.completed", item=dict(type="error", message="Fixture startup diagnostic"))
        stream.insert(1, diagnostic)
        self.assertEqual(worker.parse_events(stream)["report"]["outcome"], "approved")
        with self.assertRaises(worker.WorkerError):
            worker.parse_events([stream[0], diagnostic, stream[-1], stream[-2], stream[2]])

    def test_legacy_command_reconnects_without_retrofitted_scope_but_refuses_collection(self):
        record, assignment = self.launch_assignment()
        command = assignment["request"]["command"]
        index = command.index("--repo-identity")
        del command[index:index + 2]
        record["schema_version"] = 2
        del record["control_events"]
        del record["admission"]["repository_identity"]
        factory.save(self.path, record)
        frozen = factory.codex_command(self.path, task="task", assignment_id="qa", codex=sys.executable,
                                       model="fixture-model", effort="high", cwd=str(self.repo))
        self.assertEqual(frozen, command)
        FakeRex.calls.clear()
        with patch.object(factory, "Rex", FakeRex):
            _, reconnected = factory.launch(self.path, task="task", assignment_id="qa", role="qa",
                goal="Review the change", cwd=str(self.repo), command=frozen, purpose="work",
                binary=sys.executable, server="unix:///test.sock")
        self.assertEqual(reconnected["block_id"], assignment["block_id"])
        self.assertFalse(any(call[0] == "new" or call[:2] == ("window", "new") for call in FakeRex.calls))
        with self.assertRaisesRegex(factory.FactoryError, "predates repository identity"):
            factory.collect_codex(self.path, "qa", self.rex, record, assignment)

    def test_impostor_worker_command_cannot_collect_a_native_result(self):
        record, assignment = self.launch_assignment()
        for prefix in (["/bin/true", str(factory.ROOT / "codex_worker.py")],
                       [sys.executable, "/tmp/codex_worker.py"]):
            impostor = copy.deepcopy(assignment)
            impostor["request"]["command"][:2] = prefix
            with self.assertRaisesRegex(factory.FactoryError, "not a native Codex worker"):
                factory.collect_codex(self.path, "qa", self.rex, record, impostor)
        self.assertIsNone(factory.load_task(self.path)["assignments"]["qa"]["result"])

    def test_unsupported_or_damaged_local_event_records_refuse_collection(self):
        record, assignment = self.launch_assignment()
        original = self.args.events.read_text().splitlines()
        first = json.loads(original[0])
        for damaged in (dict(schema_version=99, kind="codex-event-line", data=first["data"]),
                        events()[0], dict(schema_version=1, kind="codex-event-line", data=None)):
            self.args.events.write_text("\n".join([json.dumps(damaged), *original[1:]]) + "\n")
            with patch.object(self.rex, "call", return_value={"last_exit": dict(exit_code=0)}):
                with self.assertRaisesRegex(worker.WorkerError, "Unsupported native event record"):
                    factory.collect_codex(self.path, "qa", self.rex, record, assignment)
            self.assertIsNone(factory.load_task(self.path)["assignments"]["qa"]["result"])
        for number in (float("nan"), float("inf"), float("-inf")):
            damaged = {**first, "unused": {"numbers": [number]}}
            self.args.events.write_text("\n".join([json.dumps(damaged), *original[1:]]) + "\n")
            with patch.object(self.rex, "call", return_value={"last_exit": dict(exit_code=0)}):
                with self.assertRaisesRegex(worker.WorkerError, "Malformed native events"):
                    factory.collect_codex(self.path, "qa", self.rex, record, assignment)
            self.assertIsNone(factory.load_task(self.path)["assignments"]["qa"]["result"])

    def test_collection_binds_current_inputs_and_reuses_completed_result(self):
        record, assignment = self.launch_assignment()
        with patch.object(self.rex, "call", return_value={"last_exit": dict(exit_code=0)}):
            first = factory.collect_codex(self.path, "qa", self.rex, record, assignment)
            second = factory.collect_codex(self.path, "qa", self.rex, record, assignment)
        self.assertEqual(first, second)
        self.assertTrue(factory.task_view(first)["assignments"]["qa"]["result_current"])
        self.assertEqual(first["assignments"]["qa"]["result"]["artifacts"][0], str(self.args.receipt))
        factory.set_inputs(self.path, candidate=self.candidate, requirements="requirements-2")
        self.assertFalse(factory.task_view(factory.load_task(self.path))["assignments"]["qa"]["result_current"])

    def test_tampered_receipt_and_unfinished_process_cannot_supply_result(self):
        record, assignment = self.launch_assignment()
        original = json.loads(self.args.receipt.read_text())
        for change in (dict(job="task/other"), dict(report=dict(outcome="completed", artifacts=["other"])),
                       dict(schema_version=True), dict(schema_version=1.0),
                       dict(exit_code=False), dict(writer=0), dict(binary="/bin/false"), dict(schema_version=1), dict(schema_version=2), dict(schema_version=3),
                       dict(worktree_git_dir="/fixture/other-git-dir"),
                       dict(repository_identity={**self.identity, "repository_id": "b" * 32})):
            factory.save(self.args.receipt, {**copy.deepcopy(original), **change})
            with patch.object(self.rex, "call", return_value={"last_exit": dict(exit_code=0)}):
                with self.assertRaises(factory.FactoryError):
                    factory.collect_codex(self.path, "qa", self.rex, record, assignment)
            self.assertIsNone(factory.load_task(self.path)["assignments"]["qa"]["result"])
        with patch.object(self.rex, "call", return_value={"child": {"pid": 1}}):
            with self.assertRaisesRegex(factory.FactoryError, "not exited"):
                factory.collect_codex(self.path, "qa", self.rex, record, assignment)

    def test_damaged_receipt_explains_how_to_recover_without_recording_a_result(self):
        record, assignment = self.launch_assignment()
        for data in (b'{"truncated":', b'\xff'):
            self.args.receipt.write_bytes(data)
            with patch.object(self.rex, "call", return_value={"last_exit": dict(exit_code=0)}):
                with self.subTest(data=data), self.assertRaisesRegex(factory.FactoryError,
                        "Cannot read native receipt .*inspect or restore the original artifact"):
                    factory.collect_codex(self.path, "qa", self.rex, record, assignment)
            self.assertIsNone(factory.load_task(self.path)["assignments"]["qa"]["result"])

    def test_completed_receipt_requires_valid_timing_and_error_fields(self):
        record, assignment = self.launch_assignment()
        original = json.loads(self.args.receipt.read_text())
        cases = []
        for key in ("created_at", "finished_at", "elapsed_seconds", "error", "usage", "worktree_git_dir"):
            missing = copy.deepcopy(original)
            del missing[key]
            cases.append(missing)
        for key in ("created_at", "finished_at", "elapsed_seconds"):
            for bad in (None, "1000", True, -1, float("nan"), float("inf"), 10 ** 400):
                cases.append({**original, key: bad})
        cases.extend(({**original, "error": "failed"}, {**original, "finished_at": 999}))
        before = self.path.read_bytes()
        for index, receipt in enumerate(cases):
            factory.save(self.args.receipt, receipt)
            with self.subTest(index=index), patch.object(self.rex, "call", return_value={"last_exit": dict(exit_code=0)}):
                with self.assertRaisesRegex(factory.FactoryError, "Native receipt does not match|Cannot read native receipt"):
                    factory.collect_codex(self.path, "qa", self.rex, record, assignment)
            self.assertEqual(self.path.read_bytes(), before)

    def test_duplicate_receipt_and_event_keys_cannot_register_a_result(self):
        record, assignment = self.launch_assignment()
        receipt = self.args.receipt.read_text()
        events_text = self.args.events.read_text()
        duplicate_receipt = receipt.replace('"schema_version": 5', '"schema_version": 99, "schema_version": 5', 1)
        duplicate_event = events_text.replace('"schema_version": 1', '"schema_version": 99, "schema_version": 1', 1)
        for damaged_receipt, damaged_events, error in (
                (duplicate_receipt, events_text, "Cannot read native receipt"),
                (receipt, duplicate_event, "Malformed native events")):
            self.args.receipt.write_text(damaged_receipt)
            self.args.events.write_text(damaged_events)
            with patch.object(self.rex, "call", return_value={"last_exit": dict(exit_code=0)}):
                with self.assertRaisesRegex((factory.FactoryError, worker.WorkerError), error):
                    factory.collect_codex(self.path, "qa", self.rex, record, assignment)
            self.assertIsNone(factory.load_task(self.path)["assignments"]["qa"]["result"])


if __name__ == "__main__":
    unittest.main()
