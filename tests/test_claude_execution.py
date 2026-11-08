"""Fixed observed native shapes; no provider or live Rex calls."""
import asyncio
import copy
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import claude_worker
import dashboard
import factory
import integration
import native_claude
from test_codex_worker import FakeProcess
from test_rex_launcher import FakeRex

MODEL = "claude-sonnet-5-5"
SESSION = "00000000-0000-0000-0000-000000000001"


def events(cwd, session=SESSION):
    report = dict(outcome="approved", artifacts=["README.md"])
    return [dict(type="system", subtype="init", cwd=cwd, session_id=session, model=MODEL,
                 tools=["Read", "StructuredOutput"], mcp_servers=[], permissionMode="dontAsk",
                 slash_commands=[], apiKeySource="none", claude_code_version="2.1.319", additional_directories=[]),
            dict(type="assistant", session_id=session, parent_tool_use_id=None,
                 message=dict(model=MODEL, content=[dict(type="tool_use", id="output-1", name="StructuredOutput", input=report)])),
            dict(type="user", session_id=session, parent_tool_use_id=None,
                 message=dict(content=[dict(type="tool_result", tool_use_id="output-1", content="Structured output provided successfully")])),
            dict(type="rate_limit_event", session_id=session, rate_limit_info=dict(status="allowed", utilization=None)),
            dict(type="result", session_id=session, subtype="success", is_error=False, terminal_reason="completed",
                 api_error_status=None, safety_stops=0, permission_denials=[], subagent_stats=dict(spawned=0), num_turns=1,
                 result=json.dumps(report), structured_output=report, usage=dict(input_tokens=2, output_tokens=20),
                 modelUsage={MODEL: dict(provider="firstParty", canonicalModel=MODEL, costBasis="list", costUSD=0.01, inputTokens=2, outputTokens=20)})]


class Decoder(unittest.TestCase):
    def parse(self, data):
        return native_claude.parse_execution(data, session_id=SESSION, cwd="/fixture", model=MODEL, max_turns=3)

    def test_original_shape_and_unknown_limits(self):
        data = events("/fixture")
        original = copy.deepcopy(data)
        self.assertEqual(self.parse(data)["report"]["outcome"], "approved")
        self.assertIsNone(self.parse(data)["rate_limits"][0]["utilization"])
        self.assertEqual(data, original)

    def test_conflicts_and_unsupported_lifecycles(self):
        for index, key, value in [(0,"tools",["Read","Bash","StructuredOutput"]), (0,"mcp_servers",[{}]),
                (0,"model","alias"), (0,"cwd","/other"), (1,"session_id","other"),
                (4,"is_error",True), (4,"num_turns",4), (4,"result",'{"outcome":"blocked"}'),
                (4,"structured_output",None), (4,"usage",None), (4,"modelUsage",{}), (4,"subtype","error_max_turns")]:
            with self.subTest(index=index,key=key):
                data=events("/fixture");data[index][key]=value
                with self.assertRaises(native_claude.ClaudeProbeError):self.parse(data)
        for data in [events("/fixture")[1:], events("/fixture")[:-1], events("/fixture")*2,
                     [events("/fixture")[0], events("/fixture")[0]],
                     events("/fixture")+[dict(type="error",session_id=SESSION)]]:
            with self.assertRaises(native_claude.ClaudeProbeError):self.parse(data)

    def test_tool_escape_and_assistant_model_refuse(self):
        for name in ["Bash", "Agent", "mcp__tool"]:
            data=events("/fixture");data[1]["message"]["content"][0]["name"]=name
            with self.assertRaises(native_claude.ClaudeProbeError):self.parse(data)
        data=events("/fixture");data[1]["message"]["model"]="alias"
        with self.assertRaises(native_claude.ClaudeProbeError):self.parse(data)

    def test_read_result_must_match_a_permitted_tool_and_scope(self):
        for outside, denied in [(False, False), (True, True), (True, False)]:
            data = events("/fixture")
            path = "/outside/file" if outside else "/fixture/file"
            call = dict(type="assistant", session_id=SESSION, parent_tool_use_id=None,
                message=dict(model=MODEL, content=[dict(type="tool_use", id="read-1", name="Read", input=dict(file_path=path))]))
            result = dict(type="user", session_id=SESSION, parent_tool_use_id=None,
                message=dict(content=[dict(type="tool_result", tool_use_id="read-1", is_error=denied, content="fixture")] ),
                tool_use_result=dict(type="text", file=dict(filePath=path, content="fixture")))
            data[1:1] = [call, result]
            if outside and not denied:
                with self.assertRaises(native_claude.ClaudeProbeError):self.parse(data)
            else:
                self.assertEqual(self.parse(data)["report"]["outcome"], "approved")

    def test_malformed_unused_fields_and_usage_refuse(self):
        for index, key, value in [(4, "subagent_stats", []), (4, "usage", dict(input_tokens=True, output_tokens=20))]:
            data = events("/fixture"); data[index][key] = value
            with self.assertRaises(native_claude.ClaudeProbeError):self.parse(data)
        data = events("/fixture"); data[-1]["modelUsage"][MODEL]["outputTokens"] = 21
        with self.assertRaises(native_claude.ClaudeProbeError):self.parse(data)

    def test_owner_read_preserves_event_envelope_and_line_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events"
            envelope = dict(schema_version=1, kind="claude-event-line", data='{"ok":true}\n')
            path.write_text(json.dumps(envelope) + "\n")
            path.chmod(0o600)
            with patch.object(claude_worker, "NATIVE_LINE_LIMIT", 32):
                self.assertEqual(claude_worker.read_events(path), [dict(ok=True)])
                for payload in [b'\xff', b'{}\n', b'x' * 193,
                        json.dumps({**envelope, "schema_version": 2}).encode(),
                        json.dumps({**envelope, "data": "x" * 33}).encode(),
                        json.dumps({**envelope, "data": '{"ok":true,"ok":false}'}).encode()]:
                    with self.subTest(payload=payload):
                        path.write_bytes(payload)
                        with self.assertRaises(native_claude.ClaudeProbeError):
                            claude_worker.read_events(path)


class ClaudeWorker(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(native_claude.OVERRIDE_PREFIXES)}
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
        factory.admit(self.path, task="task", goal="Inspect fixture", repository=str(self.base), acceptance=["README.md"], corrections=0)
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
        self.args = claude_worker.parser().parse_args(argv[4:])
        previous = Path.cwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, previous)
        self.events = events(str(self.repo), self.args.native_session)
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
        return factory.claude_command(self.path, **{**dict(task="task", assignment_id="inspect", claude=sys.executable,
            model=MODEL, effort="low", cwd=str(self.repo), role="qa", seconds=30, turns=3), **changes})

    async def spawn(self, *command, **options):
        self.prompt = options["stdin"].read().decode()
        self.spawns.append((command, options))
        return self.process

    def probe(self, binary, cwd) -> dict:
        self.probes.append((binary, cwd))
        return dict(schema_version=1, kind="claude-native-auth", harness="claude", binary=str(binary), cwd=str(cwd),
            cli_version="2.1.319 (Claude Code)", started_at=1000, observed_at=1000, probe_seconds=0.25,
            auth=dict(loggedIn=True, authMethod="claude.ai", apiProvider="firstParty"))

    def execute(self):
        with redirect_stdout(StringIO()):
            return asyncio.run(claude_worker.execute(self.args, spawn=self.spawn, probe=self.probe,
                clock=lambda:1000, timer=lambda:10))

    def collect(self):
        self.record=factory.load_task(self.path)
        self.assignment=self.record["assignments"]["inspect"]
        with patch.object(factory,"status",return_value=dict(state="exited")):
            return factory.collect_claude(self.path,"inspect",self.rex,self.record,self.assignment)

    def test_execute_collect_reconnect_and_metadata(self):
        self.assertEqual(self.execute()["state"],"completed")
        self.assertEqual(self.prompt, self.args.brief.read_text())
        command=self.spawns[0][0]
        for flag in ["--safe-mode","--restricted","--strict-mcp-config","--no-session-persistence"]:
            self.assertIn(flag,command)
        result=self.collect()
        self.assertTrue(result["assignments"]["inspect"]["native_collected"])
        self.assertEqual(self.collect(),result)
        self.assertEqual(self.make_command(),self.command)
        before=copy.deepcopy(FakeRex.calls)
        with patch.object(factory,"Rex",FakeRex):
            factory.launch(self.path,task="task",assignment_id="inspect",role="qa",goal="Inspect the exact fixture",
                cwd=str(self.repo),command=self.command,purpose="work",binary=sys.executable,server="unix:///test.sock")
        self.assertEqual([c for c in FakeRex.calls[len(before):] if c[0] in {"new","run"}],[])
        metadata=dashboard.requested_native(result,"inspect",result["assignments"]["inspect"])
        self.assertEqual(metadata["harness"],"claude")
        self.assertEqual(metadata["model"],MODEL)
        with self.assertRaises(factory.FactoryError):self.execute()

    def test_failure_timeout_and_partial_report_never_collect(self):
        self.process=FakeProcess(self.events,exit_code=1)
        self.assertEqual(self.execute()["state"],"failed")
        with self.assertRaises(factory.FactoryError):self.collect()

    def test_timeout_retains_original_stream(self):
        self.process=FakeProcess(self.events[:-1],timeout=True)
        receipt=self.execute()
        self.assertEqual(receipt["state"],"failed")
        self.assertIn("expired",receipt["error"])
        self.assertTrue(self.args.events.exists())
        with self.assertRaises(factory.FactoryError):self.collect()

    def test_receipt_and_events_tampering_refuse(self):
        self.execute()
        original=self.args.receipt.read_bytes()
        for key,value in [("native_session","other"),("role","security"),("requested_model","alias"),
                          ("schema_version",2),("exit_code",1),("brief_sha256","0"*64),("usage",{}),
                          ("worktree_git_dir","/other"),("seconds",True)]:
            with self.subTest(key=key):
                receipt=json.loads(original);receipt[key]=value;factory.save(self.args.receipt,receipt)
                with self.assertRaises(factory.FactoryError):self.collect()
        self.args.receipt.write_bytes(original)
        self.args.events.write_text('{"schema_version":2,"kind":"claude-event-line","data":"{}"}\n')
        with self.assertRaises(factory.FactoryError):self.collect()

    def test_original_checkout_required_then_historical_provenance(self):
        self.execute(); result=self.collect()
        self.git("worktree","remove","--force",str(self.repo))
        factory.claude_evidence("inspect",result,result["assignments"]["inspect"],collected=True)
        result["assignments"]["inspect"]["native_collected"]=False
        with self.assertRaises(factory.FactoryError):
            factory.claude_evidence("inspect",result,result["assignments"]["inspect"],collected=True)

    def test_original_brief_and_actual_rex_exit_required(self):
        self.execute()
        with patch.object(factory,"status",return_value=dict(state="failed")):
            with self.assertRaises(factory.FactoryError):
                factory.collect_claude(self.path,"inspect",self.rex,self.record,self.assignment)
        self.args.brief.write_text("replacement")
        with self.assertRaises(factory.FactoryError):self.collect()

    def assert_special_file_refused(self, path, read, error):
        original = path.read_bytes() if path.exists() else None
        original_open = os.open
        required_flags = os.O_NOFOLLOW | os.O_NONBLOCK
        for kind in ("fifo", "fifo-link", "directory", "file-link"):
            with self.subTest(kind=kind):
                if path.exists():
                    path.unlink()
                target = self.root / (path.name + "." + kind)
                if kind == "fifo":
                    os.mkfifo(path, 0o600)
                elif kind == "directory":
                    path.mkdir()
                else:
                    if kind == "fifo-link":
                        os.mkfifo(target, 0o600)
                    else:
                        target.write_text("replacement")
                    path.symlink_to(target)
                opened = []

                def safe_open(name, flags, *args, **kwargs):
                    if Path(name) == path:
                        # Assert before the real open: a regression must never block.
                        self.assertEqual(flags & required_flags, required_flags)
                        opened.append(name)
                    return original_open(name, flags, *args, **kwargs)

                try:
                    with patch.object(Path, "open", side_effect=AssertionError("Unsafe Path.open")), \
                            patch.object(os, "open", side_effect=safe_open):
                        with self.assertRaises(error):
                            read()
                    self.assertEqual(len(opened), 1)
                finally:
                    if kind == "directory":
                        path.rmdir()
                    else:
                        path.unlink()
                    if original is not None:
                        path.write_bytes(original)
                        path.chmod(0o600)

    def test_event_special_files_refuse_before_blocking(self):
        self.assert_special_file_refused(self.args.events,
            lambda: claude_worker.read_events(self.args.events), native_claude.ClaudeProbeError)

    def test_execution_brief_special_files_refuse_before_probe_or_spawn(self):
        before = self.path.read_bytes()
        self.assert_special_file_refused(self.args.brief, self.execute, factory.FactoryError)
        self.assertEqual(self.probes, [])
        self.assertEqual(self.spawns, [])
        self.assertFalse(self.args.events.exists())
        self.assertFalse(self.args.receipt.exists())
        self.assertEqual(self.path.read_bytes(), before)

    def test_collection_brief_special_files_refuse_without_mutation(self):
        self.execute()
        before = self.path.read_bytes()
        receipt, stream = self.args.receipt.read_bytes(), self.args.events.read_bytes()
        self.assert_special_file_refused(self.args.brief, self.collect, factory.FactoryError)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.args.receipt.read_bytes(), receipt)
        self.assertEqual(self.args.events.read_bytes(), stream)

    def test_hold_budget_and_invalid_allowance_do_not_launch(self):
        before = self.path.read_bytes()
        with redirect_stderr(StringIO()):
            code = factory.main(["--state-dir", str(self.root), "claude-start", "--task", "task", "--id", "invalid",
                "--role", "qa", "--goal", "Inspect", "--cwd", str(self.repo), "--purpose", "work",
                "--claude", sys.executable, "--model", MODEL, "--effort", "low", "--seconds", "0"])
        self.assertEqual(code, 1)
        self.assertEqual(self.path.read_bytes(), before)
        factory.hold_task(self.path, "Operator hold", clock=lambda: 1000)
        command = self.make_command(assignment_id="held")
        with patch.object(factory, "Rex", FakeRex), self.assertRaises(factory.FactoryError):
            factory.launch(self.path, task="task", assignment_id="held", role="qa", goal="Inspect",
                cwd=str(self.repo), command=command, purpose="work", binary=sys.executable, server="unix:///test.sock")
        self.assertNotIn("held", factory.load_task(self.path)["assignments"])
        factory.hold_task(self.path, None, clock=lambda:1000)
        command = self.make_command(assignment_id="correction")
        with patch.object(factory, "Rex", FakeRex), self.assertRaises(factory.FactoryError):
            factory.launch(self.path, task="task", assignment_id="correction", role="qa", goal="Inspect",
                cwd=str(self.repo), command=command, purpose="correction", binary=sys.executable, server="unix:///test.sock")

    def test_initial_collection_requires_clean_original_candidate(self):
        self.execute()
        (self.repo / "README.md").write_text("Changed after execution")
        with self.assertRaises(factory.FactoryError):self.collect()

    def test_auth_failure_does_not_infer(self):
        def bad(binary,cwd):
            value=self.probe(binary,cwd);value["auth"]["authMethod"]="api_key";return value
        with self.assertRaises(factory.FactoryError):
            asyncio.run(claude_worker.execute(self.args,spawn=self.spawn,probe=bad))
        self.assertEqual(self.spawns,[])

    def test_manual_result_has_no_collection_authority(self):
        self.execute()
        result=factory.record_result(self.path,assignment_id="inspect",candidate=self.candidate,
            requirements="requirements-1",outcome="approved",artifacts=[str(self.args.receipt),"README.md"])
        with self.assertRaises(factory.FactoryError):
            factory.claude_evidence("inspect",result,result["assignments"]["inspect"],collected=True)
        self.assertTrue(self.collect()["assignments"]["inspect"]["native_collected"])


class ClaudeApprovals(unittest.TestCase):
    def setUp(self):
        from test_integration import Integration
        self.fixture = Integration()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        with patch.object(factory.uuid,"uuid4",return_value=uuid.UUID(SESSION)):
            command=factory.claude_command(f.path,task="deliver",assignment_id="claude-qa",claude=sys.executable,
                model=MODEL,effort="low",cwd=str(f.repo),role="qa")
        record,assignment=factory.launch(f.path,task="deliver",assignment_id="claude-qa",role="qa",
            goal="Independent fixture QA",cwd=str(f.repo),command=command,purpose="work",binary=sys.executable,server="unix:///fixture.sock")
        replacements={"{repository}":str(f.repo),"{cwd}":str(f.repo),"{brief}":assignment["brief"],
            "{brief_sha256}":assignment["request"]["instructions_sha256"],"{candidate}":record["inputs"]["candidate"],
            "{requirements}":record["inputs"]["requirements"],"{repository_identity}":json.dumps(record["admission"]["repository_identity"],sort_keys=True)}
        self.args=claude_worker.parser().parse_args([replacements.get(part,part) for part in command[4:]])
        async def spawn(*command,**options):return FakeProcess(events(str(f.repo)))
        def probe(binary,cwd):
            return dict(schema_version=1,kind="claude-native-auth",harness="claude",binary=str(binary),cwd=str(cwd),
                cli_version="2.1.319 (Claude Code)",started_at=1000,observed_at=1000,probe_seconds=0,
                auth=dict(loggedIn=True,authMethod="claude.ai",apiProvider="firstParty"))
        with redirect_stdout(StringIO()):
            receipt=asyncio.run(claude_worker.execute(self.args,spawn=spawn,probe=probe,clock=lambda:1000,timer=lambda:10))
        self.assertEqual(receipt["state"],"completed",receipt["error"])
        with patch.object(factory,"status",return_value=dict(state="exited")):
            self.record=factory.collect_claude(f.path,"claude-qa",None,record,assignment)

    def test_collected_claude_ordinary_approval_and_codex_release(self):
        selected=integration.approvals(self.record,"release",["claude-qa"])
        self.assertEqual(selected["claude-qa"]["context"],SESSION)
        self.assertEqual(selected["release"]["context"],"context-release")

    def test_context_must_be_independent_of_codex_review(self):
        f=self.fixture
        f.review("other-release","release",context=SESSION)
        with self.assertRaisesRegex(factory.FactoryError,"distinct"):
            integration.approvals(factory.load_task(f.path),"other-release",["claude-qa"])

    def test_manual_or_stale_claude_approval_refused(self):
        record=copy.deepcopy(self.record)
        record["assignments"]["claude-qa"]["native_collected"]=False
        with self.assertRaises(factory.FactoryError):integration.approvals(record,"release",["claude-qa"])
        record=copy.deepcopy(self.record);record["inputs"]["requirements"]="changed"
        with self.assertRaises(factory.FactoryError):integration.approvals(record,"release",["claude-qa"])

    def test_claude_cannot_replace_final_codex_release(self):
        from argparse import Namespace
        with patch.object(integration.evidence, "delivery", return_value={}), patch.object(integration, "collected_native",
                return_value=(Namespace(role="release", write=False, claude=Path(sys.executable)), {},
                              dict(report=dict(outcome="approved")))):
            with self.assertRaisesRegex(factory.FactoryError, "native release"):
                integration.accepted(self.record, "delivery", "claude-qa", self.fixture.transport)

    def test_claude_cannot_replace_release_judgment(self):
        with self.assertRaises(factory.FactoryError):integration.approvals(self.record,"claude-qa",["qa"])
