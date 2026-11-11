"""Grok execution/collection uses fake native processes and fake Rex only."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import asyncio
import copy
from contextlib import nullcontext, redirect_stderr, redirect_stdout
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
        self.events = [dict(type="text", data='{"outcome":"blocked","artifacts":[]}'),
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

    def launch_writer(self, **changes):
        command = self.make_command(**{**dict(assignment_id="implement", role="backend", write=True,
                                              commit_message="Add fixture notes", sandbox=sys.executable), **changes})
        with patch.object(factory, "Rex", FakeRex):
            factory.launch(self.path, task="task", assignment_id="implement", role="backend", goal="Add fixture notes",
                cwd=str(self.repo), command=command, purpose="work", binary=sys.executable, server="unix:///test.sock")
        actual = [call for call in FakeRex.calls if call[0] == "new" or call[:2] == ("window", "new")][-1]
        argv = actual[actual.index("--") + 1:]
        argv = argv[argv.index("--") + 1:]
        return grok_worker.parser().parse_args(argv[4:])

    def execute_writer(self, args, *, edit=True):
        events = [dict(type="end", stopReason="end_turn", sessionId=args.native_session, requestId="writer-request",
                       cwd=str(self.repo), num_turns=2, structuredOutput=dict(outcome="completed", artifacts=["NOTES.md"]),
                       usage=dict(input_tokens=1, output_tokens=2), modelUsage=None)]
        async def spawn(*command, **options):
            self.spawns.append((command, options))
            if edit:
                (self.repo / "NOTES.md").write_text("Notes\n")
            return FakeProcess(events)
        with redirect_stdout(StringIO()):
            return asyncio.run(grok_worker.execute(args, spawn=spawn, probe=self.probe, configuration=self.configuration,
                clock=lambda: 1000, timer=lambda: 10, git_scope=lambda *a, **k: nullcontext(), scan=lambda *a, **k: None))

    def collect_writer(self):
        record = factory.load_task(self.path)
        with patch.object(factory, "status", return_value=dict(state="exited", exit=dict(exit_code=0))):
            return factory.collect_grok(self.path, "implement", self.rex, record, record["assignments"]["implement"])

    def test_writer_edits_are_committed_by_the_factory_and_collected(self):
        args = self.launch_writer()
        self.assertNotIn("Candidate path inventory", Path(factory.load_task(self.path)["assignments"]["implement"]["brief"]).read_text())
        receipt = self.execute_writer(args)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual((receipt["schema_version"], receipt["writer"], receipt["commit"]["state"]), (3, True, "created"))
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

    def test_writer_without_edits_records_an_unchanged_commit(self):
        receipt = self.execute_writer(self.launch_writer(), edit=False)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual((receipt["commit"]["state"], receipt["candidate"]), ("unchanged", self.candidate))

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
        with patch.dict(os.environ, {"GH_TOKEN": "fixture-private-token"}):
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
        self.assertEqual(command[command.index("--tools") + 1], "read_file")
        # Observed live (grok 1.0.77): use_tool reached MCP servers under --tools read_file; this rule refused it.
        self.assertEqual(command[command.index("--deny") + 1], "mcp__*")
        self.assertEqual(command[command.index("--prompt-file") + 1], str(self.args.brief))
        self.assertNotIn("--single", command)
        self.assertNotIn("GH_TOKEN", options["env"])
        receipt = json.loads(self.args.receipt.read_text())
        checkout = factory.reader_checkout_path(self.args.receipt)
        self.assertEqual((receipt["schema_version"], options["cwd"]), (4, str(checkout)))
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

    def test_legacy_code_review_without_a_range_stays_inspection_only(self):
        historical = copy.deepcopy(self.assignment)
        historical["request"]["role"] = "code-review"
        command = historical["request"]["command"]
        command[command.index("--role") + 1] = "code-review"
        before = self.path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "remains inspection-only"):
            factory.collect_grok(self.path, "inspect", self.rex, self.record, historical)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.spawns, [])

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
        self.assertEqual(evidence["diff"], "")
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
    """Grok reports count toward integration like Claude's: independent, collected, current."""
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
        events = [dict(type="end", stopReason="end_turn", sessionId=session, requestId="fixture-request",
                       cwd=str(f.repo), num_turns=1, usage=None, modelUsage=None,
                       structuredOutput=dict(outcome="completed" if write else "approved", artifacts=["fixture"]))]
        async def spawn(*command, **options):
            return echo_cwd(FakeProcess(events), options)
        probe, configuration = fake_grok_discovery(f.repo, f.root, efforts)
        with redirect_stdout(StringIO()):
            receipt = asyncio.run(grok_worker.execute(args, spawn=spawn, probe=probe, configuration=configuration,
                clock=lambda: 1000, timer=lambda: 10, git_scope=lambda *a, **k: nullcontext(), scan=lambda *a, **k: None))
        return record, assignment, receipt

    def test_collected_grok_approval_counts_beside_the_codex_release(self):
        selected = integration.approvals(self.record, "release", ["grok-qa"])
        self.assertEqual(selected["grok-qa"]["context"], self.REVIEW_SESSION)

    def test_grok_writer_contributes_and_cannot_approve_its_own_work(self):
        writer_session = "00000000-0000-0000-0000-00000000000b"
        record, assignment, receipt = self.run_grok("grok-writer", "backend", writer_session, write=True)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        with patch.object(factory, "status", return_value=dict(state="exited", exit=dict(exit_code=0))):
            record = factory.collect_grok(self.fixture.path, "grok-writer", None, record, assignment)
            self.assertEqual(integration.contributor_context("grok-writer", record, record["assignments"]["grok-writer"]),
                             writer_session)
            self.assertEqual(integration.approvals(record, "release", ["grok-qa"])["grok-qa"]["context"], self.REVIEW_SESSION)
            with self.assertRaisesRegex(factory.FactoryError, "read-only approved"):
                integration.approvals(record, "release", ["grok-writer"])

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
