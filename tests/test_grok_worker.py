"""Grok execution/collection uses fake native processes and fake Rex only."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import asyncio
import copy
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
import errno
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch
from typing import Any

import factory
import grok_worker
import integration
import native_grok
from test_abandoned_scratch import listing_cli
from test_codex_worker import FakeProcess
from test_rex_launcher import FakeRex


class GrokWorker(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("XAI_API_KEY", "GROK_CODE_XAI_API_KEY")}
        environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                           GIT_AUTHOR_DATE="2000-01-01T00:00:00Z", GIT_COMMITTER_DATE="2000-01-01T00:00:00Z")
        cleanup = patch.dict(os.environ, environment, clear=True)
        cleanup.start()
        self.addCleanup(cleanup.stop)
        self.base, self.repo = self.root / "base", self.root / "assigned"
        self.git("init", "-q", "--initial-branch=main", str(self.base), directory=self.root)
        for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.invalid"),
                           ("core.hooksPath", os.devnull), ("commit.gpgsign", "false")):
            self.git("config", key, value)
        (self.base / "README.md").write_text("Owned fixture\n")
        self.git("add", "--all")
        self.git("commit", "-qm", "Fixture")
        self.candidate = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("worktree", "add", "--quiet", "--detach", str(self.repo), self.candidate)
        self.path = self.root / "task.json"
        factory.admit(self.path, task="task", goal="Inspect fixture", repository=str(self.base), acceptance=["README.md"])
        factory.set_inputs(self.path, candidate=self.candidate, requirements="requirements-1")
        with patch.object(factory.uuid, "uuid4", return_value=uuid.UUID("00000000-0000-0000-0000-000000000001")):
            self.command = self.make_command()
        FakeRex.sessions, FakeRex.calls = [], []
        FakeRex.fail_after_create, FakeRex.host = False, "test-host"
        with patch.object(factory, "Rex", FakeRex):
            self.record, self.assignment = factory.launch(self.path, task="task", assignment_id="inspect", role="qa",
                goal="Inspect the exact fixture", cwd=str(self.repo), command=self.command, purpose="work",
                binary=sys.executable, server="unix:///test.sock")
        actual = next(call for call in FakeRex.calls if call[0] == "new")
        argv = actual[actual.index("--") + 1:]
        argv = argv[argv.index("--") + 1:]
        self.args = grok_worker.parser().parse_args(argv[4:])
        previous = Path.cwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, previous)
        self.events = [dict(type="tool_call", toolCallId="read-1", toolName="read_file", title="read_file",
                            rawInput=dict(target_file="README.md")),
            dict(type="text", data='{"outcome":"blocked","artifacts":[]}'),
            dict(type="end", stopReason="end_turn", sessionId=self.args.native_session,
                 requestId="fixture-request", cwd=str(self.repo), num_turns=1,
                 structuredOutput=dict(outcome="approved", artifacts=["README.md"]),
                 usage=dict(input_tokens=1, output_tokens=2),
                 modelUsage={"fixture-resolved": dict(modelCalls=1, costUSD=0.0)})]
        self.process = FakeProcess(self.events)
        self.spawns = []
        self.probes = []
        self.rex = FakeRex(sys.executable, "unix:///test.sock")
        self.rex.instance_id = FakeRex.instance  # As connect() records it.

    def git(self, *command, directory=None):
        result = subprocess.run([factory.git_binary(), "-C", str(directory or self.base), *command],
                                capture_output=True, text=True, env=os.environ)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def make_command(self, **changes):
        return factory.grok_command(self.path, **{**dict(task="task", assignment_id="inspect", grok=sys.executable,
            model="fixture-model", effort="high", cwd=str(self.repo), role="qa", seconds=30, turns=3), **changes})

    async def spawn(self, *command, **options):
        self.receipt_at_spawn = json.loads(self.args.receipt.read_text())
        self.spawns.append((command, options))
        return echo_cwd(self.process, options)

    async def probe(self, binary, cwd, *, configuration) -> dict[str, Any]:
        config = await configuration(binary, cwd)
        self.probes.append((binary, cwd))
        return dict(schema_version=1, kind="grok-native-metadata", route="grok-native", binary=str(binary), cwd=str(self.repo),
                    observed_at=1000, probe_seconds=0.25, configuration=config,
                    readiness="unverified", quota_observation=None, agent_version="fixture", capabilities={},
                    current_model="fixture-model",
                    authentication=dict(method="cached_token", auth_mode="Oidc", subscription_tier="Fixture", gate=None, backend_billed=False),
                    models=[dict(modelId="fixture-model", _meta=dict(reasoningEfforts=[dict(value="high")]))])

    async def configuration(self, binary, cwd):
        return dict(schema_version=2, kind="grok-subscription-configuration", cwd=str(self.repo),
                    grok_home=str(self.root / "grok-profile"), cli_version="fixture", layers=[],
                    observed_at=1000, probe_seconds=0.1, route="cached-session-without-routing-overrides",
                    sandbox="strict", tools=["read_file"], no_executable_customizations=True)

    def execute(self):
        with redirect_stdout(StringIO()):
            return asyncio.run(grok_worker.execute(self.args, spawn=self.spawn, probe=self.probe,
                configuration=self.configuration, clock=lambda: 1000, timer=lambda: 10))

    def collect(self):
        with patch.object(factory, "status", return_value=dict(state="exited")):
            return factory.collect_grok(self.path, "inspect", self.rex, self.record, self.assignment)

    def launch_writer(self, assignment_id="implement", **changes):
        command = self.make_command(**{**dict(assignment_id=assignment_id, role="backend", write=True,
                                              commit_message="Add fixture notes", sandbox=sys.executable), **changes})
        with patch.object(factory, "Rex", FakeRex):
            factory.launch(self.path, task="task", assignment_id=assignment_id, role="backend", goal="Add fixture notes",
                cwd=str(self.repo), command=command, purpose="work", binary=sys.executable, server="unix:///test.sock")
        actual = [call for call in FakeRex.calls if call[0] == "new" or call[:2] == ("window", "new")][-1]
        argv = actual[actual.index("--") + 1:]
        argv = argv[argv.index("--") + 1:]
        return grok_worker.parser().parse_args(argv[4:])

    def execute_writer(self, args, *, edit=True, artifact="NOTES.md", act=None, artifacts=None):
        # A writer reads before it reports; one that calls no tool fails (#134).
        events = [dict(type="tool_call", toolCallId="read-1", toolName="read_file", title="read_file",
                       rawInput=dict(target_file="README.md")),
                  dict(type="end", stopReason="end_turn", sessionId=args.native_session, requestId="writer-request",
                       cwd=str(self.repo), num_turns=2, structuredOutput=dict(outcome="completed", artifacts=[artifact] if artifacts is None else artifacts),
                       usage=dict(input_tokens=1, output_tokens=2), modelUsage=None)]
        async def spawn(*command, **options):
            self.spawns.append((command, options))
            if edit:
                (self.repo / "NOTES.md").write_text("Notes\n")
            if act is not None:
                act()
            return FakeProcess(events)
        with redirect_stdout(StringIO()):
            return asyncio.run(grok_worker.execute(args, spawn=spawn, probe=self.probe, configuration=self.configuration,
                clock=lambda: 1000, timer=lambda: 10, git_scope=lambda *a, **k: nullcontext(), scan=lambda *a, **k: None))

    def collect_writer(self):
        record = factory.load_task(self.path)
        with patch.object(factory, "status", return_value=dict(state="exited", exit=dict(exit_code=0))):
            return factory.collect_grok(self.path, "implement", self.rex, record, record["assignments"]["implement"])

    def test_export_checks_original_brief_bytes(self):
        from test_writer_transfer import check_provider_export_brief
        factory.set_inputs(self.path, candidate=self.candidate, requirements=self.candidate)
        args = self.launch_writer()
        receipt = self.execute_writer(args)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.collect_writer()
        check_provider_export_brief(self, args, b"substituted fixture payload")

    def test_writer_edits_are_committed_by_the_factory_and_collected(self):
        args = self.launch_writer()
        self.assertNotIn("Candidate path inventory", Path(factory.load_task(self.path)["assignments"]["implement"]["brief"]).read_text())
        receipt = self.execute_writer(args)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual((receipt["schema_version"], receipt["writer"], receipt["commit"]["state"]), (8, True, "created"))
        self.assertEqual(receipt["commit"]["parent"], self.candidate)
        self.assertEqual(factory.git_commit(".", "HEAD"), receipt["candidate"])
        self.assertEqual(self.git("show", receipt["candidate"] + ":NOTES.md").stdout, "Notes\n")
        command = self.spawns[-1][0]
        self.assertEqual(command[command.index("--permission-mode") + 1], "bypassPermissions")
        self.assertNotIn("--tools", command)  # Writers get every built-in tool, but no MCP server.
        self.assertEqual(command[command.index("--deny") + 1], "mcp__*")
        self.assertIn("do not stage or commit", command[command.index("--rules") + 1])
        collected = self.collect_writer()
        result = collected["assignments"]["implement"]["result"]
        self.assertEqual((result["outcome"], result["candidate"]), ("completed", receipt["candidate"]))
        self.assertTrue(collected["assignments"]["implement"]["native_collected"])

    def test_writer_normalization_carries_displaced_identities(self):
        source = self.repo / "README.md"
        original = source.stat()
        os.link(source, self.root / "original-input")
        args = self.launch_writer()
        source_pin = factory.load_task(self.path)["assignments"]["implement"]["writer_source"]
        self.assertEqual(source_pin, dict(root=str(self.repo), identity=factory.interpreter_identity(self.repo),
                                              displaced=[[original.st_dev, original.st_ino]]))
        between = source.stat()
        os.link(source, self.root / "rex-input")
        environment = FakeRex.writer_environment()
        current = source.stat()
        with patch.dict(os.environ, environment, clear=True):
            pinned = factory.check_worker_cwd(args, json.loads(args.repo_identity))
        if pinned is None:
            self.fail("The launched writer lost its source identities")
        root, indexed = pinned
        self.assertEqual(root, self.repo)
        self.assertTrue({(info.st_dev, info.st_ino) for info in (original, between, current)} <= indexed)

    def test_interrupted_writer_normalizations_keep_the_original_exclusions(self):
        source = self.repo / "README.md"
        original, actual = self.root / "launch-input", self.root / "entry-input"
        os.link(source, original)
        with patch.object(factory, "connect", side_effect=factory.FactoryError("fixture connection unavailable")):
            with self.assertRaisesRegex(factory.FactoryError, "connection unavailable"):
                self.launch_writer()
        self.assertTrue(source.samefile(original))
        self.launch_writer()
        self.assertFalse(source.samefile(original))
        os.link(source, actual)
        FakeRex.writer_environment()  # Stop at the captured wrapper handoff before starting the worker.
        self.assertFalse(source.samefile(actual))
        args = self.launch_writer(assignment_id="retry")
        equal = self.root / "independent"
        equal.write_bytes(source.read_bytes())
        entries = ["File: " + str(path) for path in (original, actual, equal)]
        with patch.dict(os.environ, FakeRex.writer_environment(), clear=True):
            receipt = self.execute_writer(args, artifacts=entries)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual(list(receipt["retained_files"]), [entries[-1]])
        self.assertEqual(receipt["report"]["artifacts"], entries)

    def collect_file_attempt(self, target, act=None, *, external=False):
        args = self.launch_writer()
        environment = FakeRex.writer_environment()
        entry = "File: " + str(target)
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE) as child:
            with patch.object(FakeProcess, "pid", child.pid), patch.object(factory, "remove_scratch", return_value=False):
                with patch.dict(os.environ, environment, clear=True):
                    receipt = self.execute_writer(args, artifact=entry, act=act)
            child.communicate()
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        if external:
            info = target.stat()
            self.assertEqual(receipt["retained_files"], {entry: dict(external=str(target), st_dev=info.st_dev,
                             st_ino=info.st_ino, sha256=hashlib.sha256(target.read_bytes()).hexdigest())})
        else:
            self.assertEqual(receipt["retained_files"], {})
        original = args.receipt.read_bytes()
        result = self.collect_writer()["assignments"]["implement"]["result"]
        self.assertEqual(result["artifacts"], [str(args.receipt), entry])
        self.assertEqual(args.receipt.read_bytes(), original)
        self.assertEqual(args.receipt.with_suffix(".report.txt").read_bytes(), (entry + "\n").encode())
        return args, receipt

    def test_writer_new_equal_bytes_at_literal_path_survive_checkout_advance(self):
        report = self.repo / ":(glob)result[1].md"
        args, receipt = self.collect_file_attempt(report.name, lambda: report.write_bytes((self.repo / "README.md").read_bytes()))
        expected = [dict(receipt=str(args.receipt), scratch=receipt["scratch"])]
        self.assertEqual(listing_cli(self.root), expected)
        report.unlink()
        self.git("commit", "-qam", "Remove the report in a later revision", directory=self.repo)
        result = self.git("rev-parse", "HEAD", directory=self.repo).stdout.strip()
        self.assertNotEqual(result, receipt["candidate"])
        factory.set_inputs(self.path, candidate=result, requirements="later-requirements")
        files = [args.receipt, args.events, args.brief, self.path, args.receipt.with_suffix(".report.txt"),
                 args.worktree_git_dir / "index"]
        def snapshot():
            return [(p.stat().st_ino, p.stat().st_mtime_ns, p.read_bytes()) for p in files]
        before = snapshot()
        self.assertEqual(listing_cli(self.root), expected)
        self.assertEqual(snapshot(), before)
        os.chdir(self.base)
        self.repo.rename(self.root / "parked-checkout")
        self.assertEqual(listing_cli(self.root), expected)
        for proof in (None, {**receipt["commit"], "staged_tree": result}):
            factory.save(args.receipt, {**receipt, "commit": proof})
            self.assertEqual(listing_cli(self.root), [])

    @patch.object(factory, "native_clock", new=lambda: 10)
    def test_file_hash_limits_preserve_the_collected_report(self):
        args = self.launch_writer()
        limit = 16 * 1024**2
        paths = [self.root / name for name in ("small", "too-large", "exact", "over-total", "last")]
        for path, size in zip(paths, (5, limit + 1, limit, limit, 4)):
            with path.open("wb") as stream:
                stream.truncate(size)
        missed = [self.root / f"absent-{i}" for i in range(27)]
        raw = ["File: " + str(p) for p in [*paths, *missed, self.root / "late"]]
        (self.root / "late").write_text("outside the attempt limit")
        expected = [raw[i] for i in (0, 2, 4)]
        with patch.dict(os.environ, FakeRex.writer_environment(), clear=True):
            receipt = self.execute_writer(args, artifacts=raw)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual(list(receipt["retained_files"]), expected)
        for entry in expected:
            binding = receipt["retained_files"][entry]
            with Path(binding["external"]).open("rb") as stream:
                self.assertEqual(binding["sha256"], hashlib.file_digest(stream, "sha256").hexdigest())
        self.assertEqual(receipt["report"]["artifacts"], raw)
        result = self.collect_writer()["assignments"]["implement"]["result"]
        self.assertEqual(result["artifacts"], [str(args.receipt), *raw])
        self.assertEqual(args.receipt.with_suffix(".report.txt").read_bytes(), ("\n".join(raw) + "\n").encode())

    def test_legacy_writer_blob_uses_its_original_registration(self):
        args, receipt = self.collect_file_attempt("NOTES.md")
        record = factory.load_task(self.path)
        del record["assignments"]["implement"]["writer_source"]
        factory.save(self.path, record)
        # Place the fixture receipt on the same clock as its original Git registration.
        registered = (args.worktree_git_dir / "gitdir").stat().st_ctime
        factory.save(args.receipt, {**receipt, "created_at": registered, "finished_at": registered})
        before = [item.read_bytes() for item in (self.path, args.receipt, args.events)]
        self.assertEqual(listing_cli(self.root), [dict(receipt=str(args.receipt), scratch=receipt["scratch"])])
        self.assertEqual([item.read_bytes() for item in (self.path, args.receipt, args.events)], before)

    def test_public_census_reads_commit_metadata_and_blob_with_local_git(self):
        args, receipt = self.collect_file_attempt("NOTES.md")
        run = subprocess.run
        observed = []
        def observe(command, **options):
            if command[0] == factory.git_binary():
                observed.append((command, options["env"]))
            return run(command, **options)
        with patch.object(subprocess, "run", side_effect=observe):
            self.assertEqual(factory.abandoned_scratch(self.root), [dict(receipt=str(args.receipt), scratch=receipt["scratch"])])
        commands = [command for command, _ in observed]
        for kind in ("commit", "blob"):
            self.assertTrue(any("cat-file" in cmd and cmd[cmd.index("cat-file") + 1] == kind for cmd in commands))
        for command, environment in observed:
            self.assertIn("--no-replace-objects", command)
            self.assertNotIn("show", command)
            self.assertIs(environment, observed[0][1])
            for key, expected in {"GIT_TRACE2": "0", "GIT_TRACE2_EVENT": "0", "GIT_TRACE2_PERF": "0",
                                  "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1",
                                  "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": ""}.items():
                self.assertEqual(environment[key], expected)

    def test_committed_file_limits_and_git_timeouts_leave_inline_evidence_available(self):
        args, receipt = self.collect_file_attempt("NOTES.md")
        entry, = receipt["report"]["artifacts"]
        expected = [dict(receipt=str(args.receipt), scratch=receipt["scratch"])]
        run = subprocess.run
        for limit in ("deadline", "attempts", "bytes", "metadata", "blob", "late", "short"):
            with self.subTest(limit=limit):
                now = 0
                budget = factory.ReportHashBudget(int(limit != "attempts"), 5 if limit == "bytes" else 6,
                                                  5, clock=lambda: now)
                now = 5 if limit == "deadline" else 0
                def limited(command, **options):
                    nonlocal now
                    if command[0] == factory.git_binary():
                        self.assertGreater(options["timeout"], 0)
                        self.assertLessEqual(options["timeout"], 5 - now)
                        now += 0.1
                        if limit == "metadata" and "rev-parse" in command:
                            raise subprocess.TimeoutExpired(command, options["timeout"])
                        if "blob" in command:
                            if limit == "blob":
                                raise subprocess.TimeoutExpired(command, options["timeout"], output=b"No")
                            if limit == "late":
                                now = 5
                            if limit == "short":
                                return subprocess.CompletedProcess(command, 0, b"No", b"")
                    return run(command, **options)
                receipt["report"]["artifacts"] = [entry]
                factory.save(args.receipt, receipt)
                with patch.object(factory, "ReportHashBudget", return_value=budget), \
                        patch.object(subprocess, "run", side_effect=limited), redirect_stderr(StringIO()) as errors:
                    self.assertEqual(factory.abandoned_scratch(self.root), [])
                if limit != "short":
                    self.assertEqual(errors.getvalue().count("census incomplete"), 1)
                if limit in ("blob", "short"):
                    self.assertEqual(budget.remaining, 4)
                if limit == "late":
                    self.assertEqual(budget.remaining, 0)
                receipt["report"]["artifacts"].append('{"observation":"Reviewed the committed result."}')
                factory.save(args.receipt, receipt)
                with patch.object(factory, "ReportHashBudget", return_value=budget), redirect_stderr(StringIO()):
                    self.assertEqual(factory.abandoned_scratch(self.root), expected)

    def test_oversized_committed_blob_is_rejected_before_reading(self):
        report = self.repo / "README.md"
        def oversize():
            with report.open("wb") as output:
                output.truncate(16 * 1024**2 + 1)
        self.collect_file_attempt(report, oversize)
        with patch.object(subprocess, "run", wraps=subprocess.run) as run, redirect_stderr(StringIO()) as errors:
            self.assertEqual(factory.abandoned_scratch(self.root), [])
        self.assertEqual(errors.getvalue().count("census incomplete"), 1)
        self.assertFalse(any("blob" in call.args[0] for call in run.call_args_list))

    def test_committed_and_retained_files_share_attempts_and_bytes_in_either_order(self):
        reader = self.reader_report(["qa.md"])
        args, writer = self.collect_file_attempt("NOTES.md")
        record = factory.load_task(self.path)
        rows = dict(inspect=dict(receipt=str(self.args.receipt), scratch=reader["scratch"]),
                    implement=dict(receipt=str(args.receipt), scratch=writer["scratch"]))
        for order in (("inspect", "implement"), ("implement", "inspect")):
            record["assignments"] = {key: record["assignments"][key] for key in order}
            factory.save(self.path, record)
            for attempts, byte_limit in ((1, 12), (2, 6), (2, 12)):
                with self.subTest(order=order, attempts=attempts, byte_limit=byte_limit):
                    budget = factory.ReportHashBudget(attempts, byte_limit, 5)
                    with patch.object(factory, "ReportHashBudget", return_value=budget), redirect_stderr(StringIO()) as errors:
                        found = factory.abandoned_scratch(self.root)
                    complete = attempts == 2 and byte_limit == 12
                    self.assertCountEqual(found, list(rows.values()) if complete else [rows[order[0]]])
                    self.assertEqual(errors.getvalue().count("census incomplete"), int(not complete))

    def test_nested_writer_blob_survives_worktree_move(self):
        checkout = self.repo
        self.repo = checkout / "module"
        self.repo.mkdir()
        (self.repo / "input.md").write_text("Module input")
        self.git("add", "module", directory=checkout)
        self.git("commit", "-qm", "Add module", directory=checkout)
        candidate = self.git("rev-parse", "HEAD", directory=checkout).stdout.strip()
        factory.set_inputs(self.path, candidate=candidate, requirements="module-fixture")
        os.chdir(self.repo)
        target = checkout / "proof.md"
        args, receipt = self.collect_file_attempt(target, lambda: target.write_bytes(b"Original report"))
        os.chdir(self.base)
        self.git("worktree", "move", str(checkout), str(self.root / "moved"))
        self.assertEqual(listing_cli(self.root), [dict(receipt=str(args.receipt), scratch=receipt["scratch"])])

    def test_writer_changed_readme_is_a_retained_blob(self):
        report = self.repo / "README.md"
        args, receipt = self.collect_file_attempt(report, lambda: report.write_bytes(b"Verified result\x00\xff"))
        self.assertEqual(listing_cli(self.root), [dict(receipt=str(args.receipt), scratch=receipt["scratch"])])
        oid = self.git("rev-parse", receipt["candidate"] + ":README.md").stdout.strip()
        blob = self.base / ".git/objects" / oid[:2] / oid[2:]
        # A local promisor can serve the object. Listing must leave the missing proof missing.
        mirror = self.root / "mirror.git"
        self.git("clone", "--bare", "--no-hardlinks", str(self.repo), str(mirror))
        self.git("config", "uploadpack.allowAnySHA1InWant", "true", directory=mirror)
        self.git("config", "remote.fixture.url", str(mirror))
        self.git("config", "remote.fixture.promisor", "true")
        blob.rename(blob.with_suffix(".unavailable"))
        self.assertEqual(listing_cli(self.root), [])
        absent = subprocess.run([factory.git_binary(), "--no-lazy-fetch", "-C", str(self.base), "cat-file", "-e", oid],
                                capture_output=True, check=False)
        self.assertNotEqual(absent.returncode, 0)

    def test_writer_unrelated_commit_cannot_retain_readme(self):
        self.collect_file_attempt("README.md")
        self.assertEqual(listing_cli(self.root), [])

    def test_writer_mode_only_commit_cannot_retain_readme(self):
        self.collect_file_attempt("README.md", lambda: (self.repo / "README.md").chmod(0o755))
        self.assertEqual(listing_cli(self.root), [])

    def test_writer_empty_blob_and_later_uncommitted_bytes_are_ineligible(self):
        report = self.repo / "empty.md"
        self.collect_file_attempt(report.name, report.touch)
        self.assertEqual(listing_cli(self.root), [])
        report.write_text("This did not come from the original writer")
        self.assertEqual(listing_cli(self.root), [])

    def test_literal_external_binding_survives_an_independent_link(self):
        source = self.repo / "README.md"
        target = self.root / "{fixture.md:12"
        args, receipt = self.collect_file_attempt(target, lambda: target.write_bytes(source.read_bytes()), external=True)
        os.link(target, self.root / "report-copy")
        self.assertEqual(listing_cli(self.root), [dict(receipt=str(args.receipt), scratch=receipt["scratch"])])

    def test_external_file_excludes_an_indexed_inode_alias(self):
        source = self.repo / "README.md"
        target = self.root / "outside.md"
        args, receipt = self.collect_file_attempt(target, lambda: target.write_bytes(source.read_bytes()), external=True)
        expected = [dict(receipt=str(args.receipt), scratch=receipt["scratch"])]
        self.assertEqual(listing_cli(self.root), expected)
        os.link(target, self.root / "independent-link")
        self.assertEqual(listing_cli(self.root), expected)
        index = args.worktree_git_dir / "index"
        parked = index.rename(index.with_suffix(".unavailable"))
        self.assertEqual(listing_cli(self.root), expected)
        parked.rename(index)
        target.unlink()
        os.link(source, target)
        self.assertEqual(listing_cli(self.root), [])

    def test_a_completed_writer_without_edits_fails_and_commits_nothing(self):
        # #166: a completed writer whose checkout ends unchanged has nothing to commit or verify.
        args = self.launch_writer()
        receipt = self.execute_writer(args, edit=False)
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("nothing to commit or verify", receipt["error"])
        self.assertEqual(receipt["commit"]["state"], "unchanged")
        self.assertEqual(factory.git_commit(".", "HEAD"), self.candidate)
        record = factory.load_task(self.path)
        assignment = record["assignments"]["implement"]
        with patch.object(factory, "Rex", FakeRex):
            with self.assertRaisesRegex(factory.FactoryError, "confirm the original writer"):
                integration.contributor_context("implement", record, assignment)
            with patch.dict(FakeRex.exits, {assignment["block_id"]: 1}):
                factory.tidy(self.path, factory.connect(record), record, only={"implement"}, close_session=False)
        self.assertEqual(assignment["closed"]["exit"]["exit_code"], 1)
        self.assertEqual(integration.contributor_context("implement", record, assignment), args.native_session)
        self.assertIsNone(assignment["result"])

    def test_writer_mode_requires_a_writing_role_commit_message_and_sandbox(self):
        for changes, message in ((dict(role="qa"), "does not permit writer"),
                                 (dict(commit_message=None), "--commit-message"),
                                 (dict(sandbox=None), "commit sandbox"),
                                 (dict(base=self.candidate), "--base is for code review")):
            with self.subTest(changes=changes), self.assertRaisesRegex(factory.FactoryError, message):
                self.make_command(**{**dict(assignment_id="implement", role="backend", write=True,
                                            commit_message="Add fixture notes", sandbox=sys.executable), **changes})

    def test_writer_and_reviewer_receipts_never_substitute_for_each_other(self):
        # The reviewer runs first: a writer's commit moves the shared fixture checkout.
        self.assertEqual(self.execute()["state"], "completed")
        reviewer = json.loads(self.args.receipt.read_text())
        self.args.receipt.write_text(json.dumps({**reviewer, "schema_version": 3, "writer": True}))
        with self.assertRaisesRegex(factory.FactoryError, "differs from its frozen"):
            self.collect()
        args = self.launch_writer()
        self.assertEqual(self.execute_writer(args)["state"], "completed")
        receipt = json.loads(args.receipt.read_text())
        for damaged in ({**receipt, "schema_version": 2}, {key: value for key, value in receipt.items() if key != "writer"}):
            args.receipt.write_text(json.dumps(damaged))
            with self.subTest(keys=sorted(damaged)), self.assertRaisesRegex(factory.FactoryError, "differs from its frozen"):
                self.collect_writer()

    def test_lost_pane_defers_to_the_completed_receipt(self):
        lost = dict(state="lost", reason="Rex server restarted after launch")
        with patch.object(factory, "status", return_value=lost), self.assertRaises(factory.FactoryError):
            factory.collect_grok(self.path, "inspect", self.rex, self.record, self.assignment)
        self.assertEqual(self.execute()["state"], "completed")
        with patch.object(factory, "status", return_value=lost):
            result = factory.collect_grok(self.path, "inspect", self.rex, self.record, self.assignment)
        self.assertEqual(result["assignments"]["inspect"]["result"]["outcome"], self.collect()["assignments"]["inspect"]["result"]["outcome"])

    def test_collect_cli_shows_the_saved_result_when_tidy_fails(self):
        self.assertEqual(self.execute()["state"], "completed")
        exited = dict(state="exited", exit=dict(exit_code=0))
        output, error = StringIO(), StringIO()
        with patch.object(factory, "Rex", FakeRex), patch.object(factory, "status", return_value=exited), \
                patch.object(factory, "tidy", side_effect=factory.FactoryError("Rex command failed")), \
                redirect_stdout(output), redirect_stderr(error):
            code = factory.main(["--state-dir", str(self.root), "grok-collect", "--task", "task", "--id", "inspect"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["assignments"]["inspect"]["result"]["outcome"],
                         factory.load_task(self.path)["assignments"]["inspect"]["result"]["outcome"])
        self.assertIn("Collected result saved, but its tab was not tidied; run tidy: Rex command failed", error.getvalue())

    def test_existing_effect_reconnects_without_relaunch(self):
        before = self.path.read_bytes()
        calls = copy.deepcopy(FakeRex.calls)
        self.assertEqual(self.make_command(), self.command)
        with patch.object(factory, "Rex", FakeRex):
            _, assignment = factory.launch(self.path, task="task", assignment_id="inspect", role="qa",
                goal="Inspect the exact fixture", cwd=str(self.repo), command=self.command, purpose="work",
                binary=sys.executable, server="unix:///test.sock")
        self.assertEqual(assignment, self.assignment)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([call for call in FakeRex.calls if call[0] == "new"],
                         [call for call in calls if call[0] == "new"])

    def test_original_events_bound_receipt_and_idempotent_collection(self):
        with (patch.dict(os.environ, {"GH_TOKEN": "fixture-private-token"}),
              patch.object(factory, "native_start_time", return_value=123456)):
            receipt = self.execute()
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual(receipt["report"]["outcome"], "approved")
        self.assertEqual(receipt["candidate"], self.candidate)
        self.assertEqual(receipt["discovery"]["probe_seconds"], 0.25)
        self.assertEqual(self.args.receipt.stat().st_mode & 0o777, 0o600)
        command, options = self.spawns[0]
        self.assertEqual(grok_worker.read_events(self.args.events),
                         [{**event, "cwd": options["cwd"]} if event.get("type") == "end" else event for event in self.events])
        self.assertIn("--no-subagents", command)
        self.assertEqual(command[command.index("--permission-mode") + 1], "bypassPermissions")
        self.assertNotIn("--sandbox", command)
        self.assertNotIn("--tools", command)  # A reviewer in its throwaway checkout gets every built-in tool.
        # Observed live (grok 1.0.77): use_tool reached MCP servers under --tools read_file; this rule refused it.
        self.assertEqual(command[command.index("--deny") + 1], "mcp__*")
        self.assertEqual(command[command.index("--prompt-file") + 1], str(self.args.brief))
        self.assertNotIn("--single", command)
        self.assertNotIn("GH_TOKEN", options["env"])
        receipt = json.loads(self.args.receipt.read_text())
        checkout = factory.reader_checkout_path(self.args.receipt)
        self.assertEqual((receipt["schema_version"], options["cwd"]), (8, str(checkout)))
        self.assertEqual(self.receipt_at_spawn["scratch"], options["env"]["TMPDIR"])
        self.assertIsNone(self.receipt_at_spawn["native_process"])
        self.assertEqual(receipt["native_process"], dict(pid=self.process.pid, start_time=123456, scope=factory.process_scope()))
        self.assertIs(receipt["scratch_removed"], True)
        self.assertFalse(Path(receipt["scratch"]).exists())
        self.assertFalse(checkout.exists())
        result = self.collect()
        before = self.path.read_bytes()
        self.collect()
        self.assertEqual(self.path.read_bytes(), before)
        report = result["assignments"]["inspect"]["result"]
        self.assertEqual(report["outcome"], "approved")
        self.assertEqual(report["candidate"], self.candidate)
        self.assertEqual(len(self.spawns), 1)
        self.assertEqual(self.make_command(), self.command)
        self.assertEqual(len(self.probes), 1)

    def test_new_brief_contains_candidate_path_inventory(self):
        import review_evidence
        instructions = self.args.brief.read_text()
        evidence = json.loads(instructions.partition(review_evidence.MARKER)[2])
        self.assertEqual(evidence["candidate"], self.candidate)
        self.assertEqual(evidence["requirements"], "requirements-1")
        self.assertEqual(evidence["paths"], ["README.md"])
        self.assertIsNone(evidence["base"])
        self.assertEqual(hashlib.sha256(instructions.encode()).hexdigest(), self.args.brief_sha256)

    def test_legacy_scratchless_receipt_collects_and_verifies_in_place(self):
        receipt = self.execute()
        old = {key: value for key, value in receipt.items() if key not in factory.SCRATCH_RECEIPT_FIELDS | {"interpreter", "retained_files"}}
        factory.save(self.args.receipt, {**old, "schema_version": 4})
        original = self.args.receipt.read_bytes()
        record = self.collect()
        _, verified, _ = factory.grok_evidence("inspect", record, record["assignments"]["inspect"], collected=True)
        self.assertEqual(verified["report"]["outcome"], "approved")
        self.assertEqual(self.args.receipt.read_bytes(), original)

    def test_legacy_code_review_without_a_range_stays_inspection_only(self):
        historical = copy.deepcopy(self.assignment)
        historical["request"]["role"] = "code-review"
        command = historical["request"]["command"]
        command[command.index("--role") + 1] = "code-review"
        del historical["native_launch"]  # Pre-range reviews also predate launch attestations.
        before = self.path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "first collection requires the original released"):
            factory.collect_grok(self.path, "inspect", self.rex, self.record, historical)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.spawns, [])

        # Even an already collected original result cannot supply a missing review range.
        self.execute()
        collected = self.collect()["assignments"]["inspect"]
        historical.update(native_collected=collected["native_collected"], result=collected["result"])
        before, spawns = self.path.read_bytes(), list(self.spawns)
        with self.assertRaisesRegex(factory.FactoryError, "remains inspection-only"):
            factory.collect_grok(self.path, "inspect", self.rex, self.record, historical)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.spawns, spawns)

    def test_code_review_requires_an_explicit_range_before_launch(self):
        with self.assertRaisesRegex(factory.FactoryError, "Supply --base"):
            self.make_command(assignment_id="review", role="code-review")
        bypass = self.make_command(assignment_id="review")
        bypass[bypass.index("--role") + 1] = "code-review"
        before, calls = self.path.read_bytes(), list(FakeRex.calls)
        with patch.object(factory, "Rex", FakeRex), self.assertRaisesRegex(factory.FactoryError, "Supply --base"):
            factory.launch(self.path, task="task", assignment_id="review", role="code-review",
                goal="Review the selected diff", cwd=str(self.repo), command=bypass, purpose="work")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(FakeRex.calls, calls)
        command = self.make_command(assignment_id="review", role="code-review", base=self.candidate)
        with patch.object(factory, "Rex", FakeRex):
            record, assignment = factory.launch(self.path, task="task", assignment_id="review", role="code-review",
                goal="Review the selected diff", cwd=str(self.repo), command=command, purpose="work")
        self.assertIn('--base', assignment["request"]["command"])
        instructions = Path(assignment["brief"]).read_text()
        import review_evidence
        evidence = json.loads(instructions.partition(review_evidence.MARKER)[2])
        self.assertEqual(evidence["base"], self.candidate)
        self.assertEqual(evidence["candidate"], self.candidate)
        self.assertEqual(evidence["changed_paths"], [])
        self.assertNotIn("diff", evidence)
        before, launches = self.path.read_bytes(), len(FakeRex.calls)
        with patch.object(factory, "Rex", FakeRex):
            factory.launch(self.path, task="task", assignment_id="review", role="code-review",
                goal="Review the selected diff", cwd=str(self.repo), command=command, purpose="work")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len([call for call in FakeRex.calls[launches:] if call[0] in {"new", "window"}]), 0)

    def test_a_reader_that_writes_into_its_directory_leaves_the_shared_worktree_clean(self):
        # Observed (#85): Grok loads the operator's MCP servers whatever --tools says, and
        # Playwright's wrote .playwright-mcp/ into the writer worktree a reader shared.
        spawn = self.spawn
        async def mcp_output(*command, **options):
            output = Path(options["cwd"]) / ".playwright-mcp"
            output.mkdir()
            (output / "console.log").write_text("browser session\n")
            return await spawn(*command, **options)
        self.spawn = mcp_output
        self.assertEqual(self.execute()["state"], "completed")
        factory.check_checkout(str(self.base), str(self.repo), self.candidate)
        self.assertFalse(factory.reader_checkout_path(self.args.receipt).exists())
        self.assertEqual(self.collect()["assignments"]["inspect"]["result"]["outcome"], "approved")

    def reader_report(self, entries):
        self.events[-1]["structuredOutput"] = dict(outcome="approved", artifacts=entries)
        self.process = FakeProcess(self.events)
        spawn = self.spawn
        async def write_reports(*command, **options):
            for name in entries:
                path = Path(options["cwd"]) / name
                if not path.exists():
                    path.write_bytes(b"Proof\n")
            return await spawn(*command, **options)
        self.spawn = write_reports
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE) as child:
            with patch.object(FakeProcess, "pid", child.pid), patch.object(factory, "remove_scratch", return_value=False):
                receipt = self.execute()
            child.communicate()
        self.assertEqual((receipt["exit_code"], receipt["state"]), (0, "completed"), receipt["error"])
        self.assertFalse(factory.reader_checkout_path(self.args.receipt).exists())
        original = self.args.receipt.read_bytes()
        result = self.collect()["assignments"]["inspect"]["result"]
        self.assertEqual((result["outcome"], result["artifacts"]), ("approved", [str(self.args.receipt), *entries]))
        self.assertEqual(self.args.receipt.read_bytes(), original)
        self.assertEqual(self.args.receipt.with_suffix(".report.txt").read_bytes(), ("\n".join(entries) + "\n").encode())
        return receipt

    def test_a_report_file_left_in_the_reader_checkout_is_retained(self):
        receipt = self.reader_report(["qa-report.md", "README.md"])
        kept, = self.args.receipt.with_suffix(".checkout-artifacts").iterdir()
        self.assertEqual(kept.read_bytes(), b"Proof\n")
        expected = [dict(receipt=str(self.args.receipt), scratch=receipt["scratch"])]
        self.assertEqual(listing_cli(self.root), expected)
        kept.write_bytes(b"other")
        self.assertEqual(listing_cli(self.root), [])

    def fail_copy_sync(self, fail_at):
        provision = factory.provisioning_file
        count = 0
        @contextmanager
        def create(root, name):
            nonlocal count
            if root == self.args.receipt.with_suffix(".checkout-artifacts"):
                count += 1
                if count == fail_at:
                    with provision(root, name) as output, patch.object(os, "fsync", side_effect=OSError(errno.ENOSPC, "disk full")):
                        yield output
                    return
            with provision(root, name) as output:
                yield output
        return patch.object(factory, "provisioning_file", create)

    def test_optional_storage_failure_leaves_the_native_result_collectible(self):
        with self.fail_copy_sync(1), redirect_stderr(StringIO()) as errors:
            receipt = self.reader_report(["qa.md"])
        self.assertIn("qa.md", errors.getvalue())
        self.assertIn("disk full", errors.getvalue())
        self.assertEqual(receipt["retained_files"], {})
        self.assertEqual(listing_cli(self.root), [])

    def test_optional_storage_failure_preserves_earlier_and_later_copies(self):
        with self.fail_copy_sync(2), redirect_stderr(StringIO()) as errors:
            receipt = self.reader_report(["first.md", "second.md", "third.md"])
        self.assertIn("second.md", errors.getvalue())
        self.assertEqual(list(receipt["retained_files"]), ["first.md", "third.md"])
        self.assertEqual(listing_cli(self.root), [dict(receipt=str(self.args.receipt), scratch=receipt["scratch"])])

    def test_expired_native_deadline_skips_optional_publication(self):
        now = 10
        check = factory.check_tool_evidence
        def after_native(*args):
            nonlocal now
            now += self.args.seconds
            return check(*args)
        with patch.object(factory, "native_clock", side_effect=lambda: now), \
                patch.object(factory, "check_tool_evidence", side_effect=after_native):
            receipt = self.reader_report(["qa.md"])
        self.assertEqual(receipt["retained_files"], {})
        self.assertFalse(self.args.receipt.with_suffix(".checkout-artifacts").exists())
        self.assertEqual(listing_cli(self.root), [])

    def test_optional_git_uses_the_time_left_after_native_execution(self):
        now = 10.0
        check = factory.check_tool_evidence
        run = subprocess.run
        timeouts = []
        def after_native(*args):
            nonlocal now
            now += self.args.seconds - 0.25
            return check(*args)
        def slow_git(command, **options):
            if "ls-files" in command and options.get("timeout", 600) < 1:
                timeouts.append(options["timeout"])
                raise subprocess.TimeoutExpired(command, options["timeout"])
            return run(command, **options)
        with patch.object(factory, "native_clock", side_effect=lambda: now), \
                patch.object(factory, "check_tool_evidence", side_effect=after_native), \
                patch.object(subprocess, "run", side_effect=slow_git), redirect_stderr(StringIO()) as errors:
            receipt = self.reader_report(["qa.md"])
        self.assertEqual(timeouts, [0.25])
        self.assertIn("qa.md", errors.getvalue())
        self.assertEqual(receipt["retained_files"], {})

    def test_reset_restores_a_checkout_a_failed_reader_left_dirty(self):
        # The canary's worktree was dirtied by a reader that ran in place before readers got checkouts.
        self.process = FakeProcess(self.events, exit_code=1)
        self.assertEqual(self.execute()["state"], "failed")
        (self.repo / ".playwright-mcp").mkdir()
        (self.repo / ".playwright-mcp" / "console.log").write_text("browser session\n")
        with self.assertRaisesRegex(factory.FactoryError, "uncommitted inputs"):
            factory.check_checkout(str(self.base), str(self.repo), self.candidate)
        result = factory.reset_writer(self.path, assignment_id="inspect")
        self.assertIn("rerun the reader under a new ID", result["next"])
        factory.check_checkout(str(self.base), str(self.repo), self.candidate)

    def test_failed_native_process_cannot_supply_a_result(self):
        self.process = FakeProcess(self.events, exit_code=1)
        receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "frozen completed assignment"):
            self.collect()
        self.assertEqual(self.path.read_bytes(), before)

    def test_wrong_native_session_refuses_completion(self):
        self.events[-1]["sessionId"] = "another-session"
        self.process = FakeProcess(self.events)
        receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("completion differs", receipt["error"])
        self.assertIsNone(receipt["report"])

    def test_command_line_reconnect_and_collection_reuse_the_original_effect(self):
        self.assertEqual(self.execute()["state"], "completed")
        before = len([call for call in FakeRex.calls if call[0] == "new"])
        output = StringIO()
        with patch.object(factory, "Rex", FakeRex), patch.object(factory, "status", return_value=dict(state="exited")), redirect_stdout(output):
            code = factory.main(["--state-dir", str(self.root), "grok-start", "--task", "task",
                "--id", "inspect", "--role", "qa", "--goal", "Inspect the exact fixture", "--cwd", str(self.repo),
                "--purpose", "work", "--grok", sys.executable, "--model", "fixture-model", "--effort", "high",
                "--seconds", "30", "--turns", "3"])
        self.assertEqual(code, 0)
        self.assertEqual(len([call for call in FakeRex.calls if call[0] == "new"]), before)
        output = StringIO()
        exited = dict(state="exited", exit=dict(exit_code=0))  # Collection then tidies the finished tab.
        with patch.object(factory, "Rex", FakeRex), patch.object(factory, "status", return_value=exited), redirect_stdout(output):
            code = factory.main(["--state-dir", str(self.root), "grok-collect", "--task", "task", "--id", "inspect"])
        self.assertEqual(code, 0)
        closed = factory.load_task(self.path)["assignments"]["inspect"]["closed"]
        self.assertEqual((closed["state"], closed["exit"]), ("exited", dict(exit_code=0)))
        self.assertEqual(json.loads(output.getvalue())["assignments"]["inspect"]["result"]["outcome"], "approved")
        self.assertEqual(len(self.spawns), 1)

    def test_invalid_command_line_limits_refuse_before_task_directory_creation(self):
        directory = self.root / "never-created"
        with redirect_stdout(StringIO()), patch("sys.stderr", StringIO()):
            code = factory.main(["--state-dir", str(directory), "grok-start", "--task", "task", "--id", "inspect",
                "--role", "qa", "--goal", "Inspect fixture", "--cwd", str(self.repo), "--purpose", "work",
                "--grok", sys.executable, "--model", "fixture-model", "--effort", "high", "--turns", "0"])
        self.assertEqual(code, 1)
        self.assertFalse(directory.exists())

    def test_spawn_io_failure_retains_a_failed_receipt_without_a_retry(self):
        async def unavailable(*command, **options):
            self.spawns.append((command, options))
            raise OSError("private process detail")
        self.spawn = unavailable
        receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("restore local access/storage", receipt["error"])
        self.assertNotIn("private process detail", receipt["error"])
        self.assertTrue(self.args.events.exists())
        self.assertIsNone(receipt["candidate"])
        self.assertEqual(len(self.spawns), 1)

    def test_timeout_stops_owned_child_and_never_relaunches_an_existing_attempt(self):
        self.process = FakeProcess([], timeout=True)
        receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("allowance expired", receipt["error"])
        self.assertTrue(self.process.signals)
        with self.assertRaisesRegex(factory.FactoryError, "already has artifacts"):
            self.execute()
        self.assertEqual(len(self.spawns), 1)

    def test_pane_interrupt_records_a_failed_attempt_and_stops_the_child(self):
        # factory.py cancel sends Ctrl-C to the pane; asyncio.run turns that SIGINT into cancelling execute.
        class Hanging(FakeProcess):
            async def readline(self):
                await asyncio.Event().wait()
        self.process = Hanging([])
        async def interrupted():
            run = asyncio.ensure_future(grok_worker.execute(self.args, spawn=self.spawn, probe=self.probe,
                configuration=self.configuration, clock=lambda: 1000, timer=lambda: 10))
            await asyncio.sleep(0.05)
            run.cancel()
            return await run
        with redirect_stdout(StringIO()):
            receipt = asyncio.run(interrupted())
        self.assertEqual(receipt["state"], "failed")
        self.assertTrue(receipt["error"].startswith("Cancelled by an interrupt in its pane"))
        self.assertTrue(self.process.signals)
        self.assertEqual(json.loads(self.args.receipt.read_text()), receipt)

    def test_changed_source_refuses_completion(self):
        original = self.spawn
        async def edit(*command, **options):
            result = await original(*command, **options)
            (self.repo / "README.md").write_text("Changed source\n")
            return result
        self.spawn = edit
        receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIsNone(receipt["candidate"])

    def test_receipt_substitutions_and_boolean_usage_refuse_without_task_changes(self):
        original = self.execute()
        self.assertEqual(original["state"], "completed")
        before = self.path.read_bytes()
        for change in (dict(native_session="other"), dict(schema_version=3), dict(created_at=True),
                       dict(report=dict(outcome="completed", artifacts=[])),
                       dict(usage=dict(input_tokens=True, output_tokens=2))):
            damaged = copy.deepcopy(original)
            damaged.update(change)
            self.args.receipt.write_text(json.dumps(damaged))
            with self.subTest(change=change), self.assertRaises(factory.FactoryError):
                self.collect()
            self.assertEqual(self.path.read_bytes(), before)

    def test_replacement_linked_checkout_cannot_supply_collection_binding(self):
        self.assertEqual(self.execute()["state"], "completed")
        os.chdir(self.base)
        self.git("worktree", "move", str(self.repo), str(self.root / "retained"))
        self.git("worktree", "add", "--quiet", "--detach", str(self.repo), self.candidate)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "worktree binding changed"):
            self.collect()
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_stream_version_cannot_be_collected(self):
        self.assertEqual(self.execute()["state"], "completed")
        records = [json.loads(line) for line in self.args.events.read_text().splitlines()]
        records[0]["schema_version"] = 2
        self.args.events.write_text("".join(json.dumps(record) + "\n" for record in records))
        with self.assertRaisesRegex(factory.FactoryError, "Unsupported Grok event record"):
            self.collect()

    def test_api_override_and_changed_brief_refuse_before_native_artifacts(self):
        with patch.dict(os.environ, {"XAI_API_KEY": ""}):
            with self.assertRaisesRegex(factory.FactoryError, "Unset XAI_API_KEY"):
                self.execute()
        self.args.brief.write_text("Changed brief")
        with self.assertRaisesRegex(factory.FactoryError, "assignment binding changed"):
            self.execute()
        self.assertEqual(self.spawns, [])
        self.assertFalse(self.args.receipt.exists())

    def test_ineligible_subscription_or_effort_refuses_before_model_launch(self):
        original = self.probe
        async def unavailable(binary, cwd, **options):
            result = await original(binary, cwd, **options)
            result["authentication"]["gate"] = {"message": "Fixture capacity hold"}
            return result
        self.probe = unavailable
        with self.assertRaisesRegex(factory.FactoryError, "eligibility or model/effort is unavailable"):
            self.execute()
        self.assertEqual(self.spawns, [])
        self.assertFalse(self.args.events.exists())

    def test_selected_native_profile_reaches_the_model_without_host_credentials(self):
        with patch.dict(os.environ, {"GROK_HOME": str(self.root / "selected-profile"), "GH_TOKEN": "fixture-private-token"}):
            self.assertEqual(self.execute()["state"], "completed")
        self.assertEqual(self.spawns[0][1]["env"]["GROK_HOME"], str(self.root / "selected-profile"))
        self.assertNotIn("GH_TOKEN", self.spawns[0][1]["env"])

    def test_model_override_refuses_before_metadata_and_model_start(self):
        async def overridden(binary, cwd):
            raise grok_worker.native_grok.GrokProbeError("Grok authentication/model overrides are unsupported")
        self.configuration = overridden
        with self.assertRaisesRegex(factory.FactoryError, "overrides are unsupported"):
            self.execute()
        self.assertEqual(self.probes, [])
        self.assertEqual(self.spawns, [])
        self.assertFalse(self.args.receipt.exists())

    def test_older_receipt_without_subscription_configuration_stays_inspection_only(self):
        receipt = self.execute()
        receipt["schema_version"] = 1
        del receipt["configuration"]
        self.args.receipt.write_text(json.dumps(receipt))
        before = self.path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "frozen completed assignment"):
            self.collect()
        self.assertEqual(self.path.read_bytes(), before)

    def test_malformed_nested_configuration_cannot_launch_or_record_a_result(self):
        original = self.configuration
        for changed in (dict(layers=[None]), dict(layers=[{}]),
                        dict(layers=[dict(path="relative", role="user", sha256="a" * 64)]),
                        dict(layers=[dict(path=str(self.root / "config.toml"), role="", sha256="a" * 64)]),
                        dict(layers=[dict(path=str(self.root / "config.toml"), role="user", sha256="not-a-digest")]),
                        dict(cli_version=""), dict(probe_seconds=-1), dict(observed_at=True)):
            async def malformed(binary, cwd):
                return {**await original(binary, cwd), **changed}
            self.configuration = malformed
            with self.subTest(changed=changed), self.assertRaisesRegex(factory.FactoryError, "Grok configuration"):
                self.execute()
            self.assertEqual(self.spawns, [])
            self.assertFalse(self.args.receipt.exists())
        self.configuration = original
        receipt = self.execute()
        before = self.path.read_bytes()
        for changed in (dict(layers=[None]), dict(cli_version=""), dict(probe_seconds=float("inf")),
                        dict(observed_at=False)):
            damaged = copy.deepcopy(receipt)
            damaged["configuration"].update(changed)
            damaged["discovery"]["configuration"].update(changed)
            self.args.receipt.write_text(json.dumps(damaged))
            with self.subTest(changed=changed), self.assertRaises(factory.FactoryError):
                self.collect()
            self.assertEqual(self.path.read_bytes(), before)

    def test_malformed_discovery_cannot_launch_or_record_a_result(self):
        original = self.probe
        for changed in (dict(agent_version=""), dict(capabilities=None), dict(models=[None]),
                        dict(authentication={}), dict(current_model=""), dict(observed_at=-1)):
            async def malformed(binary, cwd, **options):
                return {**await original(binary, cwd, **options), **changed}
            self.probe = malformed
            with self.subTest(changed=changed), self.assertRaises(factory.FactoryError):
                self.execute()
            self.assertEqual(self.spawns, [])
            self.assertFalse(self.args.events.exists())
        self.probe = original
        receipt = self.execute()
        before = self.path.read_bytes()
        for changed in (dict(models=[None]), dict(authentication={}), dict(agent_version=""),
                        dict(probe_seconds=True)):
            damaged = copy.deepcopy(receipt)
            damaged["discovery"].update(changed)
            self.args.receipt.write_text(json.dumps(damaged))
            with self.subTest(changed=changed), self.assertRaises(factory.FactoryError):
                self.collect()
            self.assertEqual(self.path.read_bytes(), before)



def echo_cwd(process, options):
    """Grok reports the directory it runs in, and readers run in a disposable checkout (#85)."""
    if options.get("cwd", ".") != ".":
        events = [json.loads(line) for line in process.lines]
        for event in events:
            if event.get("type") == "end" and "cwd" in event:
                event["cwd"] = options["cwd"]
        process.lines = iter([json.dumps(event).encode() + b"\n" for event in events])
    return process


def fake_grok_discovery(repo, root, efforts=("high",)):
    async def configuration(binary, cwd):
        return dict(schema_version=2, kind="grok-subscription-configuration", cwd=str(repo),
                    grok_home=str(root / "grok-profile"), cli_version="fixture", layers=[],
                    observed_at=1000, probe_seconds=0.1, route="cached-session-without-routing-overrides",
                    sandbox="none", tools=["read_file"], no_executable_customizations=True)
    async def probe(binary, cwd, *, configuration) -> dict[str, Any]:
        return dict(schema_version=1, kind="grok-native-metadata", route="grok-native", binary=str(binary), cwd=str(repo),
                    observed_at=1000, probe_seconds=0.25, configuration=await configuration(binary, cwd),
                    readiness="unverified", quota_observation=None, agent_version="fixture", capabilities={},
                    current_model="fixture-model",
                    authentication=dict(method="cached_token", auth_mode="Oidc", subscription_tier="Fixture", gate=None, backend_billed=False),
                    models=[dict(modelId="fixture-model", _meta=dict(reasoningEfforts=[dict(value=item) for item in efforts]))])
    return probe, configuration


class GrokApprovals(unittest.TestCase):
    """Grok reports count toward delivery approvals like Claude's: independent, collected, current."""
    REVIEW_SESSION = "00000000-0000-0000-0000-00000000000a"

    def setUp(self):
        environment = {key: value for key, value in os.environ.items() if key not in ("XAI_API_KEY", "GROK_CODE_XAI_API_KEY")}
        cleanup = patch.dict(os.environ, environment, clear=True)
        cleanup.start()
        self.addCleanup(cleanup.stop)
        from test_integration import Integration
        self.fixture = Integration()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        record, assignment, receipt = self.run_grok("grok-qa", "qa", self.REVIEW_SESSION)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        with patch.object(factory, "status", return_value=dict(state="exited")):
            self.record = factory.collect_grok(self.fixture.path, "grok-qa", None, record, assignment)

    def run_grok(self, identity, role, session, *, write=False, effort="high", efforts=("high",), frozen=None):
        f = self.fixture
        writer: dict[str, Any] = dict(write=True, commit_message="Add fixture notes", sandbox=sys.executable) if write else {}
        with patch.object(factory.uuid, "uuid4", return_value=uuid.UUID(session)):
            command = factory.grok_command(f.path, task="deliver", assignment_id=identity, grok=sys.executable,
                model="fixture-model", effort=effort, cwd=str(f.repo), role=role, release_evidence=frozen, **writer)
        record, assignment = factory.launch(f.path, task="deliver", assignment_id=identity, role=role,
            goal="Fixture " + role, cwd=str(f.repo), command=command, purpose="work",
            binary=sys.executable, server="unix:///fixture.sock", frozen_release=frozen)
        replacements = {"{repository}": str(f.repo), "{cwd}": str(f.repo), "{brief}": assignment["brief"],
            "{brief_sha256}": assignment["request"]["instructions_sha256"], "{candidate}": record["inputs"]["candidate"],
            "{requirements}": record["inputs"]["requirements"],
            "{repository_identity}": json.dumps(record["admission"]["repository_identity"], sort_keys=True)}
        args = grok_worker.parser().parse_args([replacements.get(part, part) for part in command[4:]])
        events = [dict(type="tool_call", toolCallId="read-1", toolName="read_file", title="read_file",
                       rawInput=dict(target_file="README.md")),
                  dict(type="end", stopReason="end_turn", sessionId=session, requestId="fixture-request",
                       cwd=str(f.repo), num_turns=1, usage=None, modelUsage=None,
                       structuredOutput=dict(outcome="completed" if write else "approved", artifacts=["fixture"]))]
        async def spawn(*command, **options):
            if write:
                (f.repo / "NOTES.md").write_text("Notes\n")  # A completed writer commits its change (#166).
            return echo_cwd(FakeProcess(events), options)
        probe, configuration = fake_grok_discovery(f.repo, f.root, efforts)
        with redirect_stdout(StringIO()):
            receipt = asyncio.run(grok_worker.execute(args, spawn=spawn, probe=probe, configuration=configuration,
                clock=lambda: 1000, timer=lambda: 10, git_scope=lambda *a, **k: nullcontext(), scan=lambda *a, **k: None))
        return record, assignment, receipt

    def test_collected_grok_approval_counts_beside_the_codex_release(self):
        selected = integration.approvals(self.record, "release", ["grok-qa"])
        self.assertEqual(selected["grok-qa"]["context"], self.REVIEW_SESSION)
        with patch.object(factory, "ROOT", self.fixture.root / "another-install"):
            self.assertEqual(integration.approvals(self.record, "release", ["grok-qa"]), selected)

    def test_grok_writer_contributes_and_cannot_approve_its_own_work(self):
        writer_session = "00000000-0000-0000-0000-00000000000b"
        record, assignment, receipt = self.run_grok("grok-writer", "backend", writer_session, write=True)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        with patch.object(factory, "status", return_value=dict(state="exited", exit=dict(exit_code=0))):
            record = factory.collect_grok(self.fixture.path, "grok-writer", None, record, assignment)
            self.assertEqual(integration.contributor_context("grok-writer", record, record["assignments"]["grok-writer"]),
                             writer_session)
            self.assertEqual(integration.approvals(record, "release", ["grok-qa"])["grok-qa"]["context"], self.REVIEW_SESSION)
            # Its commit moved past the task's inputs, so it is refused before its role is even weighed.
            with self.assertRaisesRegex(factory.FactoryError, r"deliver/grok-writer: stale result \(candidate changed\)"):
                integration.approvals(record, "release", ["grok-writer"])

    def test_grok_writer_lost_with_its_host_stays_a_contributor_under_its_frozen_session(self):
        # Observed (#156): a Grok correction lost to the 2026-11-12 reboot kept a starting receipt and blocked delivery.
        writer_session = "00000000-0000-0000-0000-00000000000c"
        record, assignment, _ = self.run_grok("grok-lost", "backend", writer_session, write=True)
        request = assignment["request"]
        args = grok_worker.parser().parse_args(request["command"][4:])
        receipt_path = Path(request["command"][request["command"].index("--receipt") + 1])
        # Field for field what the Grok worker writes before its native run: an independent fixture.
        starting = dict(schema_version=3, kind="grok-execution", state="starting", job=args.job, inputs=request["inputs"],
                        repository_identity=record["admission"]["repository_identity"], worktree_git_dir=str(args.worktree_git_dir),
                        cwd=request["cwd"], brief_sha256=request["instructions_sha256"], role=args.role, binary="/fixture/grok",
                        requested_model=args.model, requested_effort=args.effort, discovery={}, configuration={},
                        native_session=writer_session, seconds=args.seconds, max_turns=args.turns, created_at=1000.0,
                        finished_at=None, elapsed_seconds=None, exit_code=None, report=None, request_id=None, usage=None,
                        model_usage=None, turns=None, candidate=None, events=str(args.events), error=None, writer=True,
                        sandbox=str(args.sandbox), commit=None)
        receipt_path.write_text(json.dumps(starting))
        with patch.object(factory, "connect"), patch.object(factory, "status", return_value=dict(state="lost")):
            self.assertEqual(integration.contributor_context("grok-lost", record, assignment), writer_session)
            damaged = [{k: v for k, v in starting.items() if k != field} for field in starting if field != "native_session"]
            damaged += [{**starting, field: value} for field, value in (
                ("request_id", "fixture-request"), ("kind", "claude-execution"), ("configuration", None),
                ("requested_effort", "other"), ("created_at", "yesterday"), ("extra", 1))]
            for receipt in damaged:
                with self.subTest(fields=sorted(set(starting) ^ set(receipt)) or [k for k in receipt if receipt[k] != starting.get(k)]):
                    receipt_path.write_text(json.dumps(receipt))
                    with self.assertRaises(factory.FactoryError):
                        integration.contributor_context("grok-lost", record, assignment)

    def test_manual_or_stale_grok_approval_refused(self):
        record = copy.deepcopy(self.record)
        record["assignments"]["grok-qa"]["native_collected"] = False
        with self.assertRaises(factory.FactoryError):
            integration.approvals(record, "release", ["grok-qa"])
        record = copy.deepcopy(self.record)
        record["inputs"]["requirements"] = "changed"
        with self.assertRaises(factory.FactoryError):
            integration.approvals(record, "release", ["grok-qa"])

    def test_a_grok_qa_report_cannot_be_the_release_judgment(self):
        with self.assertRaises(factory.FactoryError):
            integration.approvals(self.record, "grok-qa", ["qa"])

    def test_a_test_automation_approval_counts_and_a_refusal_names_the_approval(self):
        # Observed: the canary's `delivered --after tests` was refused with no hint which approval was wrong.
        record, assignment, receipt = self.run_grok("grok-tests", "test-automation", "00000000-0000-0000-0000-00000000000e")
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        with patch.object(factory, "status", return_value=dict(state="exited")):
            record = factory.collect_grok(self.fixture.path, "grok-tests", None, record, assignment)
            self.assertIn("grok-tests", integration.approvals(record, "release", ["grok-qa", "grok-tests"]))
            with self.assertRaisesRegex(factory.FactoryError, r"grok-qa \(role qa, outcome approved\)"):
                integration.approvals(record, "grok-qa", ["qa"])

    def test_grok_gives_the_release_judgment_at_its_models_maximum_effort(self):
        session = "00000000-0000-0000-0000-00000000000c"
        record, assignment, receipt = self.run_grok("grok-release", "release", session, frozen=self.fixture.frozen)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        with patch.object(factory, "status", return_value=dict(state="exited")):
            record = factory.collect_grok(self.fixture.path, "grok-release", None, record, assignment)
            selected = integration.approvals(record, "grok-release", ["qa", "grok-qa"])
        self.assertEqual(selected["grok-release"]["context"], session)
        self.assertEqual(selected["grok-release"]["evidence"], self.fixture.frozen)

    def test_grok_release_judgment_below_its_models_maximum_effort_refuses(self):
        with self.assertRaisesRegex(factory.FactoryError, "maximum effort: select high, not low"):
            self.run_grok("lazy-release", "release", "00000000-0000-0000-0000-00000000000d",
                          effort="low", efforts=("low", "high"), frozen=self.fixture.frozen)


if __name__ == "__main__":
    unittest.main()
