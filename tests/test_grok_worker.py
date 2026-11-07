"""Grok execution/collection uses fake native processes and fake Rex only."""
import asyncio
import copy
from contextlib import redirect_stdout
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
        return self.process

    async def probe(self, binary, cwd) -> dict[str, Any]:
        self.probes.append((binary, cwd))
        return dict(schema_version=1, kind="grok-native-metadata", binary=str(binary), cwd=str(self.repo),
                    observed_at=1000, probe_seconds=0.25,
                    authentication=dict(auth_mode="Oidc", subscription_tier="Fixture", gate=None),
                    models=[dict(modelId="fixture-model", _meta=dict(reasoningEfforts=[dict(value="high")]))])

    def execute(self):
        with redirect_stdout(StringIO()):
            return asyncio.run(grok_worker.execute(self.args, spawn=self.spawn, probe=self.probe, clock=lambda: 1000, timer=lambda: 10))

    def collect(self):
        with patch.object(factory, "status", return_value=dict(state="exited")):
            return factory.collect_grok(self.path, "inspect", self.rex, self.record, self.assignment)

    def test_original_events_bound_receipt_and_idempotent_collection(self):
        with patch.dict(os.environ, {"GH_TOKEN": "fixture-private-token"}):
            receipt = self.execute()
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual(receipt["report"]["outcome"], "approved")
        self.assertEqual(receipt["candidate"], self.candidate)
        self.assertEqual(receipt["discovery"]["probe_seconds"], 0.25)
        self.assertEqual(self.args.receipt.stat().st_mode & 0o777, 0o600)
        self.assertEqual(grok_worker.read_events(self.args.events), self.events)
        command, options = self.spawns[0]
        self.assertIn("--no-subagents", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertNotIn("GH_TOKEN", options["env"])
        self.assertEqual(options["cwd"], ".")
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
        with patch.object(factory, "Rex", FakeRex), patch.object(factory, "status", return_value=dict(state="exited")), redirect_stdout(output):
            code = factory.main(["--state-dir", str(self.root), "grok-collect", "--task", "task", "--id", "inspect"])
        self.assertEqual(code, 0)
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
        for change in (dict(native_session="other"), dict(schema_version=2), dict(created_at=True),
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
        async def unavailable(binary, cwd):
            result = await original(binary, cwd)
            result["authentication"]["gate"] = {"message": "Fixture capacity hold"}
            return result
        self.probe = unavailable
        with self.assertRaisesRegex(factory.FactoryError, "eligibility or model/effort is unavailable"):
            self.execute()
        self.assertEqual(self.spawns, [])
        self.assertFalse(self.args.events.exists())


if __name__ == "__main__":
    unittest.main()
