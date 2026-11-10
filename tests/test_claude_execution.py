"""Fixed observed native shapes; no provider or live Rex calls."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import asyncio
import copy
from contextlib import nullcontext, redirect_stdout, redirect_stderr
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
RECURSIVE_JSON = '{"recursive":"fixture"}'


def recursion_on(payload):
    original = factory.json.loads

    def loads(value, **kwargs):
        if value == payload:
            raise RecursionError("injected decoder failure")
        return original(value, **kwargs)

    return patch.object(factory.json, "loads", side_effect=loads)


def envelope_bytes(lines):
    return b"".join((json.dumps(dict(schema_version=1, kind="claude-event-line", data=line)) + "\n").encode("ascii")
                    for line in lines)


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


def writer_events(cwd, session=SESSION, *, path=None, model=MODEL):
    """Observed 2.1.319 writer shapes: edit tools in the checkout, then a report."""
    path = path or str(Path(cwd) / "NOTES.md")
    report = dict(outcome="completed", artifacts=["NOTES.md"])
    return [dict(type="system", subtype="init", cwd=cwd, session_id=session, model=model,
                 tools=list(native_claude.WRITE_TOOLS), mcp_servers=[], permissionMode="acceptEdits",
                 slash_commands=[], apiKeySource="none", claude_code_version="2.1.319", additional_directories=[]),
            dict(type="assistant", session_id=session, parent_tool_use_id=None,
                 message=dict(model=model, content=[dict(type="tool_use", id="write-1", name="Write",
                                                         input=dict(file_path=path, content="Notes\n"))])),
            dict(type="user", session_id=session, parent_tool_use_id=None,
                 message=dict(content=[dict(type="tool_result", tool_use_id="write-1", content="File created")]),
                 tool_use_result=dict(type="create", filePath=path, content="Notes\n", structuredPatch=[],
                                      originalFile=None, userModified=False)),
            dict(type="assistant", session_id=session, parent_tool_use_id=None,
                 message=dict(model=model, content=[dict(type="tool_use", id="output-1", name="StructuredOutput", input=report)])),
            dict(type="user", session_id=session, parent_tool_use_id=None,
                 message=dict(content=[dict(type="tool_result", tool_use_id="output-1", content="Structured output provided successfully")])),
            dict(type="result", session_id=session, subtype="success", is_error=False, terminal_reason="completed",
                 api_error_status=None, safety_stops=0, permission_denials=[], subagent_stats=dict(spawned=0), num_turns=2,
                 result=json.dumps(report), structured_output=report, usage=dict(input_tokens=5, output_tokens=40),
                 modelUsage={model: dict(provider="firstParty", canonicalModel=model, costBasis="list", costUSD=0.02, inputTokens=5, outputTokens=40)})]


def thinking_events(session=SESSION) -> list[dict]:
    """Fixed sanitized operational-loop/architecture 2.1.319 metadata shapes."""
    return [dict(type="system", subtype="thinking_tokens", estimated_tokens=50,
                 estimated_tokens_delta=50, session_id=session,
                 uuid="00000000-0000-0000-0000-000000000050"),
            dict(type="system", subtype="thinking_tokens", estimated_tokens=234,
                 estimated_tokens_delta=184, session_id=session,
                 uuid="00000000-0000-0000-0000-000000000234")]


class Decoder(unittest.TestCase):
    def parse(self, data):
        return native_claude.parse_execution(data, session_id=SESSION, cwd="/fixture", model=MODEL, max_turns=3)

    def test_original_shape_and_unknown_limits(self):
        data = events("/fixture")
        original = copy.deepcopy(data)
        self.assertEqual(self.parse(data)["report"]["outcome"], "approved")
        self.assertIsNone(self.parse(data)["rate_limits"][0]["utilization"])
        self.assertEqual(data, original)

    def test_thinking_estimates_preserve_raw_events_and_actual_result(self):
        data = events("/fixture")
        expected = self.parse(data)
        data[1:1] = thinking_events()
        original = copy.deepcopy(data)
        self.assertEqual(self.parse(data), expected)
        self.assertEqual(data, original)
        for metadata in ({}, {"uuid": None}, {"uuid": True}, {"uuid": False},
                         {"uuid": 7}, {"uuid": "opaque"}, {"uuid": ["opaque"]},
                         {"uuid": {"opaque": "metadata"}}):
            with self.subTest(metadata=metadata):
                data = events("/fixture")
                telemetry = thinking_events()
                for event in telemetry:
                    del event["uuid"]
                    event.update(metadata)
                data[1:1] = telemetry
                original = copy.deepcopy(data)
                self.assertEqual(self.parse(data), expected)
                self.assertEqual(data, original)
        # These are independent estimates, with no documented continuity/reset rule.
        data[2].update(estimated_tokens=0, estimated_tokens_delta=999)
        self.assertEqual(self.parse(data), expected)

    def test_thinking_estimate_counters_refuse_malformed_values(self):
        for key in ("estimated_tokens", "estimated_tokens_delta"):
            for value in (None, True, False, -1, 1.0, "50", [], {}):
                with self.subTest(key=key, value=value):
                    telemetry = thinking_events()[0]
                    telemetry[key] = value
                    data = events("/fixture")
                    data.insert(1, telemetry)
                    with self.assertRaises(native_claude.ClaudeProbeError):
                        self.parse(data)
            telemetry = thinking_events()[0]
            del telemetry[key]
            data = events("/fixture")
            data.insert(1, telemetry)
            with self.assertRaises(native_claude.ClaudeProbeError):
                self.parse(data)

    def test_thinking_session_order_and_unknown_subtype_refuse(self):
        for position, changes in ((0, {}), (5, {}), (1, {"session_id": "foreign"}),
                                  (1, {"session_id": None}), (1, {"subtype": "unknown"})):
            with self.subTest(position=position, changes=changes):
                data = events("/fixture")
                data.insert(position, {**thinking_events()[0], **changes})
                with self.assertRaises(native_claude.ClaudeProbeError):
                    self.parse(data)
        with self.assertRaises(native_claude.ClaudeProbeError):
            self.parse([events("/fixture")[0], *thinking_events()])

    def test_thinking_does_not_supply_report_or_actual_usage(self):
        for outcome in ("blocked", "findings", "completed", "approved"):
            data = events("/fixture")
            report = dict(outcome=outcome, artifacts=["README.md"])
            data[1]["message"]["content"][0]["input"] = report
            data[-1].update(structured_output=report, result=json.dumps(report))
            data[1:1] = thinking_events()
            self.assertEqual(self.parse(data)["report"], report)
        for changes in (dict(usage=None), dict(usage=dict(input_tokens=2, output_tokens=234)),
                        dict(modelUsage={}), dict(structured_output=None),
                        dict(subtype="error_max_turns"), dict(permission_denials=[{}])):
            with self.subTest(changes=changes):
                data = events("/fixture")
                data[1:1] = thinking_events()
                data[-1].update(changes)
                with self.assertRaises(native_claude.ClaudeProbeError):
                    self.parse(data)
        data = events("/fixture")
        data[1:3] = thinking_events()
        with self.assertRaises(native_claude.ClaudeProbeError):
            self.parse(data)

    def test_thinking_preserves_read_pairing_and_pending_tools(self):
        for requested, returned in (("/fixture/file", "/fixture/file"),
                                    ("/fixture/file", "/fixture/other"),
                                    ("/outside/file", "/outside/file")):
            data = self.read_data(requested, returned)
            data[2:2] = thinking_events()
            if requested == returned:
                self.assertEqual(self.parse(data)["usage"], dict(input_tokens=2, output_tokens=20))
                del data[4]  # Telemetry cannot complete a pending Read.
            with self.assertRaises(native_claude.ClaudeProbeError):
                self.parse(data)

    def test_shared_json_depth_refuses_without_chained_traceback(self):
        with recursion_on(RECURSIVE_JSON), self.assertRaisesRegex(ValueError, "nesting.*reduce") as error:
            factory.decode_json(RECURSIVE_JSON)
        self.assertTrue(error.exception.__suppress_context__)
        self.assertIsNone(error.exception.__cause__)

    def test_shared_json_preserves_valid_and_strict_values(self):
        self.assertEqual(factory.decode_json('{"a":[0,1.5,true,null],"b":-2}'),
                         dict(a=[0, 1.5, True, None], b=-2))
        for payload in ('{"a":1,"a":2}', 'NaN', 'Infinity', '-Infinity', '1e999', '9' * 400):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                factory.decode_json(payload)

    def test_successfully_parsed_deep_json_and_custom_hook(self):
        payload = "[" * 128 + '{"leaf":1}' + "]" * 128
        decoded = factory.decode_json(payload, object_pairs_hook=lambda pairs: tuple(pairs))
        for _ in range(128):
            self.assertEqual(len(decoded), 1)
            decoded = decoded[0]
        self.assertEqual(decoded, (("leaf", 1),))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events"
            path.write_bytes(envelope_bytes([payload + "\n"]))
            path.chmod(0o600)
            self.assertEqual(claude_worker.read_events(path), [json.loads(payload)])
        data = events("/fixture")
        data[-1]["result"] = payload
        with self.assertRaises(native_claude.ClaudeProbeError):
            self.parse(data)

    def test_bounded_record_uses_finite_read_even_if_file_grows(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "record"
            path.write_bytes(b"x")
            path.chmod(0o600)
            original_open = os.fdopen
            counts = []

            def open_stream(fd, mode):
                stream = original_open(fd, mode)
                original_read = stream.read

                def read(count=-1):
                    counts.append(count)
                    self.assertEqual(count, 9)
                    # Growth after descriptor validation defeats a pre-stat-only check.
                    path.write_bytes(b"x" * 100)
                    return original_read(count)

                stream.read = read
                return stream

            with patch.object(os, "fdopen", side_effect=open_stream):
                with self.assertRaises(OSError):
                    factory.read_record(path, private=True, max_bytes=8)
            self.assertEqual(counts, [9])
            self.assertEqual(path.read_bytes(), b"x" * 100)

    def test_retained_stream_boundary_and_private_reader(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events"
            # Non-ASCII data also measures serialized bytes, rather than characters.
            raw = envelope_bytes(['{"text":"ü"}\n'] * 100)
            path.write_bytes(raw)
            path.chmod(0o600)
            with patch.object(claude_worker, "RETAINED_STREAM_LIMIT", len(raw)):
                self.assertEqual(claude_worker.read_events(path), [dict(text="ü")] * 100)
            with patch.object(claude_worker, "RETAINED_STREAM_LIMIT", len(raw) - 1):
                with self.assertRaises(native_claude.ClaudeProbeError):
                    claude_worker.read_events(path)
            self.assertEqual(factory.read_record(path), raw.decode())
            self.assertEqual(factory.read_record(path, private=True, max_bytes=len(raw)), raw.decode())
            with self.assertRaises(OSError):
                factory.read_record(path, private=True, max_bytes=len(raw) - 1)
            with patch.object(os, "getuid", return_value=os.getuid() + 1):
                with self.assertRaises(OSError):
                    factory.read_record(path, private=True, max_bytes=len(raw))
            path.chmod(0o644)
            with self.assertRaises(OSError):
                factory.read_record(path, private=True, max_bytes=len(raw))
            path.chmod(0o600)
            os.link(path, Path(temporary) / "link")
            with self.assertRaises(OSError):
                factory.read_record(path, private=True, max_bytes=len(raw))
            self.assertEqual(path.read_bytes(), raw)

    def test_deep_embedded_final_result_refuses(self):
        data = events("/fixture")
        data[-1]["result"] = RECURSIVE_JSON
        with recursion_on(RECURSIVE_JSON), self.assertRaises(native_claude.ClaudeProbeError):
            self.parse(data)

    def test_retained_deep_envelope_and_data_refuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events"
            for text in (RECURSIVE_JSON, json.dumps(dict(schema_version=1, kind="claude-event-line", data=RECURSIVE_JSON))):
                with self.subTest(envelope=text == RECURSIVE_JSON):
                    path.write_text(text + "\n")
                    path.chmod(0o600)
                    original = path.read_bytes()
                    target = text + "\n" if text == RECURSIVE_JSON else RECURSIVE_JSON
                    with recursion_on(target) as loads:
                        with self.assertRaisesRegex(native_claude.ClaudeProbeError, "retained Claude events"):
                            claude_worker.read_events(path)
                        self.assertTrue(any(call.args[0] == target for call in loads.call_args_list))
                    self.assertEqual(path.read_bytes(), original)

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

    def test_reads_anywhere_decode_in_bypass_mode(self):
        # No harness fence: a reviewer may read a spec, a sibling or the stdlib.
        for outside, denied in [(False, False), (True, True), (True, False)]:
            data = events("/fixture")
            path = "/outside/file" if outside else "/fixture/file"
            call = dict(type="assistant", session_id=SESSION, parent_tool_use_id=None,
                message=dict(model=MODEL, content=[dict(type="tool_use", id="read-1", name="Read", input=dict(file_path=path))]))
            result = dict(type="user", session_id=SESSION, parent_tool_use_id=None,
                message=dict(content=[dict(type="tool_result", tool_use_id="read-1", is_error=denied, content="fixture")] ),
                tool_use_result=dict(type="text", file=dict(filePath=path, content="fixture")))
            data[1:1] = [call, result]
            self.assertEqual(self.parse(data)["report"]["outcome"], "approved")

    def read_data(self, requested, returned):
        data = events("/fixture")
        data[1:1] = [
            dict(type="assistant", session_id=SESSION, parent_tool_use_id=None,
                 message=dict(model=MODEL, content=[dict(type="tool_use", id="read-1", name="Read",
                                                       input=dict(file_path=requested))])),
            dict(type="user", session_id=SESSION, parent_tool_use_id=None,
                 message=dict(content=[dict(type="tool_result", tool_use_id="read-1", content="fixture")]),
                 tool_use_result=dict(type="text", file=dict(filePath=returned, content="fixture")))]
        return data

    def test_read_result_rejects_different_in_scope_path(self):
        data = self.read_data("/fixture/.factory-state/inside-canary.txt",
                              "/fixture/.factory-state/DIFFERENT-FILE.txt")
        original = copy.deepcopy(data)
        with self.assertRaises(native_claude.ClaudeProbeError):
            self.parse(data)
        self.assertEqual(data, original)

    def test_matching_read_paths_validate_without_filesystem_resolution(self):
        for path in ("/fixture/file", "/fixture/.factory-state/inside-canary.txt",
                     "/fixture/nested/../file", "/fixture/./file", "/fixture/space and ü.txt"):
            with self.subTest(path=path):
                data = self.read_data(path, path)
                original = copy.deepcopy(data)
                with patch.object(Path, "resolve", side_effect=AssertionError("Decoder must stay offline")):
                    self.assertEqual(self.parse(data)["report"]["outcome"], "approved")
                self.assertEqual(data, original)

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


class WriterDecoder(unittest.TestCase):
    def parse(self, data, *, writer=True):
        return native_claude.parse_execution(data, session_id=SESSION, cwd="/fixture", model=MODEL, max_turns=3, writer=writer)

    def test_writer_stream_decodes_with_edit_tools_inside_the_checkout(self):
        self.assertEqual(self.parse(writer_events("/fixture"))["report"]["outcome"], "completed")

    def test_writer_and_reviewer_shapes_never_substitute_for_each_other(self):
        with self.assertRaises(native_claude.ClaudeProbeError):
            self.parse(writer_events("/fixture"), writer=False)
        with self.assertRaises(native_claude.ClaudeProbeError):
            self.parse(events("/fixture"), writer=True)

    def search(self, name, **arguments):
        data = writer_events("/fixture")
        data[1]["message"]["content"] = [dict(type="tool_use", id="search-1", name=name, input=arguments)]
        data[2]["message"]["content"] = [dict(type="tool_result", tool_use_id="search-1", content="NOTES.md")]
        del data[2]["tool_use_result"]
        return data

    def test_relative_search_paths_resolve_inside_the_checkout(self):
        for name, arguments in (("Glob", dict(pattern="**/*.md")), ("Glob", dict(pattern="*.md", path=".")),
                                ("Glob", dict(pattern="*.py", path="src")), ("Glob", dict(pattern="/fixture/**/*.md")),
                                ("Grep", dict(pattern="TODO")), ("Grep", dict(pattern="TODO", path="src", glob="*.py"))):
            with self.subTest(name=name, arguments=arguments):
                self.assertEqual(self.parse(self.search(name, **arguments))["report"]["outcome"], "completed")

    def test_searches_outside_the_checkout_decode(self):
        for name, arguments in (("Glob", dict(pattern="../**")), ("Glob", dict(pattern="/etc/**")),
                                ("Glob", dict(pattern="*.md", path="../outside")),
                                ("Grep", dict(pattern="x", path="/")), ("Grep", dict(pattern="x", glob="../*.py"))):
            with self.subTest(name=name, arguments=arguments):
                self.assertEqual(self.parse(self.search(name, **arguments))["report"]["outcome"], "completed")

    def test_malformed_searches_refuse(self):
        for name, arguments in (("Glob", dict(path=".")), ("Glob", dict(pattern="")),
                                ("Grep", dict(pattern="x", path="")), ("Grep", dict(pattern="x", glob=""))):
            with self.subTest(name=name, arguments=arguments), self.assertRaises(native_claude.ClaudeProbeError):
                self.parse(self.search(name, **arguments))

    def shell(self, **arguments):
        data = writer_events("/fixture")
        data[1]["message"]["content"] = [dict(type="tool_use", id="shell-1", name="Bash", input=arguments)]
        data[2]["message"]["content"] = [dict(type="tool_result", tool_use_id="shell-1", content="ok", is_error=False)]
        data[2]["tool_use_result"] = dict(stdout="ok\n", stderr="", interrupted=False, isImage=False, noOutputExpected=False)
        return data

    def test_writers_run_shell_commands(self):
        for arguments in (dict(command="python3 -m unittest", description="Run tests"),
                          dict(command="curl x", dangerouslyDisableSandbox=True), dict(command="sleep 9", run_in_background=True)):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.parse(self.shell(**arguments))["report"]["outcome"], "completed")

    def test_shell_calls_without_a_command_refuse(self):
        with self.assertRaises(native_claude.ClaudeProbeError):
            self.parse(self.shell(description="no command"))

    def test_sandbox_network_refusals_are_inspection_only_for_writers(self):
        data = self.shell(command="curl -sS https://example.com")
        data[2]["message"]["content"][0]["is_error"] = True
        data.insert(2, dict(type="system", subtype="permission_denied", session_id=SESSION, tool_name="SandboxNetworkAccess",
                            tool_use_id="00000000-0000-4000-8000-000000000056", decision_reason_type="asyncAgent"))
        self.assertEqual(self.parse(data)["report"]["outcome"], "completed")
        reviewer = events("/fixture")
        reviewer.insert(1, dict(data[2]))
        with self.assertRaises(native_claude.ClaudeProbeError):
            self.parse(reviewer, writer=False)

    def test_transient_api_retries_decode_and_malformed_retries_refuse(self):
        # Observed live (#45): a completed 100-turn writer retried the API mid-run.
        retry = dict(type="system", subtype="api_retry", attempt=1, max_retries=10, retry_delay_ms=621, error_status=None,
                     error="unknown", session_id=SESSION, uuid="00000000-0000-4000-8000-000000000081")
        data = writer_events("/fixture")
        self.assertEqual(self.parse([*data[:3], dict(retry), *data[3:]])["report"]["outcome"], "completed")
        without_delay = {key: value for key, value in retry.items() if key != "retry_delay_ms"}
        for damaged in ({**retry, "attempt": "1"}, {**retry, "attempt": True}, {**retry, "max_retries": None},
                        {**retry, "retry_delay_ms": -1}, without_delay, {**retry, "session_id": "other"}):
            with self.subTest(damaged=damaged), self.assertRaises(native_claude.ClaudeProbeError):
                self.parse([*data[:3], damaged, *data[3:]])
        with self.assertRaises(native_claude.ClaudeProbeError):
            self.parse([dict(retry), *data])

    def shell_task(self):
        """Observed 2.1.319 lifecycle of a Bash call Claude Code moved to the background."""
        data = self.shell(command="sleep 90")
        task = dict(task_id="bf5wd7jy7", run_id="0mw9hzo0h-5c2ecaff", session_id=SESSION)
        running = [dict(type="system", subtype="task_started", tool_use_id="shell-1", description="sleep 90",
                        is_backgrounded=False, task_type="local_bash", **task),
                   dict(type="tool_progress", tool_use_id="shell-1-heartbeat-0", tool_name="Bash", parent_tool_use_id="shell-1",
                        elapsed_time_seconds=30, heartbeat=True, session_id=SESSION),
                   dict(type="system", subtype="background_tasks_changed", session_id=SESSION,
                        tasks=[dict(task_id="bf5wd7jy7", run_id="0mw9hzo0h-5c2ecaff", task_type="local_bash", description="sleep 90")]),
                   dict(type="system", subtype="task_updated", patch=dict(is_backgrounded=True), **task)]
        finished = [dict(type="system", subtype="task_updated", patch=dict(status="failed", end_time=1794243684863), **task),
                    dict(type="system", subtype="task_notification", tool_use_id="shell-1", status="failed", output_file="",
                         summary="sleep 90", **task),
                    dict(type="system", subtype="background_tasks_changed", tasks=[], session_id=SESSION)]
        return [*data[:2], *running, data[2], *finished, *data[3:]]

    def test_long_shell_task_lifecycles_decode(self):
        self.assertEqual(self.parse(self.shell_task())["report"]["outcome"], "completed")

    def test_background_shell_tasks_are_announced_before_they_start(self):
        # Observed (canary writer-2): run_in_background lists the task in background_tasks_changed
        # one event before its task_started, then the Bash result reports the background ID.
        data = self.shell(command="npx vitest run", run_in_background=True)
        task = dict(task_id="b49xlktt3", run_id="0mw9pgmp2-5786118d", session_id=SESSION)
        announced = [dict(type="system", subtype="background_tasks_changed", session_id=SESSION,
                          tasks=[dict(task_id="b49xlktt3", run_id="0mw9pgmp2-5786118d", task_type="local_bash",
                                      description="npx vitest run")]),
                     dict(type="system", subtype="task_started", tool_use_id="shell-1", description="npx vitest run",
                          is_backgrounded=True, task_type="local_bash", **task)]
        finished = [dict(type="system", subtype="task_updated", patch=dict(status="completed", end_time=1794255705591), **task),
                    dict(type="system", subtype="task_notification", tool_use_id="shell-1", status="completed",
                         output_file="", summary="npx vitest run", **task),
                    dict(type="system", subtype="background_tasks_changed", tasks=[], session_id=SESSION)]
        stream = [*data[:2], *announced, data[2], *finished, *data[3:]]
        self.assertEqual(self.parse(stream)["report"]["outcome"], "completed")
        with self.assertRaises(native_claude.ClaudeProbeError):
            self.parse([*stream[:4], dict(stream[3]), *stream[4:]])  # started twice

    def test_shell_task_events_must_name_a_known_bash_call_or_task(self):
        for index, change in ((2, dict(tool_use_id="other-1")), (2, dict(task_type="local_agent")), (2, dict(task_id="")),
                              (3, dict(parent_tool_use_id="other-1")), (3, dict(tool_name="Write")),
                              (3, dict(parent_tool_use_id=["shell-1"])), (3, dict(elapsed_time_seconds="30")),
                              (4, dict(tasks=[dict(task_id="other", task_type="local_agent")])),
                              (4, dict(tasks=[dict(task_id="")])), (4, dict(tasks=None)),
                              (5, dict(task_id="unknown")), (5, dict(patch=None)), (8, dict(status="")),
                              (8, dict(task_id=["bf5wd7jy7"]))):
            data = self.shell_task()
            data[index] = {**data[index], **change}
            with self.subTest(index=index, change=change), self.assertRaises(native_claude.ClaudeProbeError):
                self.parse(data)
        data = self.shell_task()
        with self.assertRaises(native_claude.ClaudeProbeError):
            self.parse([*data[:3], dict(data[2]), *data[3:]])
        edit = writer_events("/fixture")
        with self.assertRaises(native_claude.ClaudeProbeError):
            self.parse([*edit[:2], {**data[2], "tool_use_id": "write-1"}, *edit[2:]])
        reviewer = events("/fixture")
        with self.assertRaises(native_claude.ClaudeProbeError):
            self.parse([*reviewer[:1], dict(data[3]), *reviewer[1:]], writer=False)

    def test_newer_claude_code_is_accepted_and_older_or_malformed_refuse(self):
        for version, accepted in (("2.1.319", True), ("2.1.400", True), ("2.2.0", True), ("3.0.1", True),
                                  ("2.1.300", False), ("2.1", False), ("latest", False)):
            data = writer_events("/fixture")
            data[0]["claude_code_version"] = version
            with self.subTest(version=version):
                if accepted:
                    self.assertEqual(self.parse(data)["report"]["outcome"], "completed")
                else:
                    with self.assertRaises(native_claude.ClaudeProbeError):
                        self.parse(data)
                self.assertEqual(native_claude.supported_version(version + " (Claude Code)", suffix=" (Claude Code)"), accepted)
        self.assertFalse(native_claude.supported_version("2.1.400", suffix=" (Claude Code)"))

    def test_agents_run_in_bypass_mode_without_a_harness_fence(self):
        for command in (native_claude.write_command(MODEL, "high", SESSION, 10), native_claude.read_command(MODEL, "low", SESSION, 3)):
            self.assertEqual(command[command.index("--permission-mode") + 1], "bypassPermissions")
            self.assertNotIn("--restricted", command)
            self.assertNotIn("--settings", command)
        writer = native_claude.write_command(MODEL, "high", SESSION, 10)
        self.assertIn("Bash", writer[writer.index("--tools") + 1:writer.index("--strict-mcp-config")])
        import factory_commit
        self.assertIn(factory_commit.WRITER_CONTRACT, writer[writer.index("--system-prompt") + 1])
        self.assertIn("ignores gitleaks:allow", factory_commit.WRITER_CONTRACT)

    def test_bypass_mode_rendering_notifications_decode_anywhere(self):
        # Observed live: bypassPermissions runs emit system/ui_invalidate before init.
        notice = dict(type="system", subtype="ui_invalidate", event="ui.render", session_id=SESSION,
                      uuid="00000000-0000-4000-8000-000000000099")
        data = writer_events("/fixture")
        data[0]["permissionMode"] = "bypassPermissions"
        self.assertEqual(self.parse([dict(notice), *data[:2], dict(notice), *data[2:]])["report"]["outcome"], "completed")
        for damaged in ({**notice, "event": None}, {**notice, "session_id": "other"}):
            with self.subTest(damaged=damaged), self.assertRaises(native_claude.ClaudeProbeError):
                self.parse([damaged, *data])

    def test_streams_from_confined_and_bypass_modes_both_decode(self):
        for writer, legacy in ((True, "acceptEdits"), (False, "dontAsk")):
            for mode in (legacy, "bypassPermissions"):
                data = writer_events("/fixture") if writer else events("/fixture")
                data[0]["permissionMode"] = mode
                with self.subTest(writer=writer, mode=mode):
                    self.assertIn(self.parse(data, writer=writer)["report"]["outcome"], {"completed", "approved"})
        data = writer_events("/fixture")
        data[0]["permissionMode"] = "dontAsk"
        with self.assertRaises(native_claude.ClaudeProbeError):
            self.parse(data)

    def test_refused_attempts_outside_the_checkout_do_not_fail_the_writer(self):
        # Observed live: a mistyped path the harness refused, then the corrected edit.
        data = writer_events("/fixture")
        refused = [dict(type="assistant", session_id=SESSION, parent_tool_use_id=None,
                        message=dict(model=MODEL, content=[dict(type="tool_use", id="typo-1", name="Edit",
                            input=dict(file_path="/fixture-typo/NOTES.md", old_string="a", new_string="b"))])),
                   dict(type="user", session_id=SESSION, parent_tool_use_id=None, tool_use_result="Error: File does not exist.",
                        message=dict(content=[dict(type="tool_result", tool_use_id="typo-1", is_error=True,
                                                   content="<tool_use_error>File does not exist.</tool_use_error>")])),
                   dict(type="assistant", session_id=SESSION, parent_tool_use_id=None,
                        message=dict(model=MODEL, content=[dict(type="tool_use", id="search-1", name="Glob",
                                                                input=dict(pattern="../**"))])),
                   dict(type="user", session_id=SESSION, parent_tool_use_id=None, tool_use_result="Error: outside the working directory",
                        message=dict(content=[dict(type="tool_result", tool_use_id="search-1", is_error=True, content="denied")]))]
        self.assertEqual(self.parse(data[:1] + refused + data[1:])["report"]["outcome"], "completed")

    def test_edits_outside_the_checkout_decode_but_mismatched_results_refuse(self):
        # The factory commit takes only the worktree, so an outside edit never becomes candidate content.
        for path in ("/elsewhere/NOTES.md", "/fixture/../elsewhere/NOTES.md"):
            with self.subTest(path=path):
                self.assertEqual(self.parse(writer_events("/fixture", path=path))["report"]["outcome"], "completed")
        mismatch = writer_events("/fixture")
        mismatch[2]["tool_use_result"]["filePath"] = "/fixture/OTHER.md"
        with self.assertRaises(native_claude.ClaudeProbeError):
            self.parse(mismatch)


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
        self.rex.instance_id = FakeRex.instance  # As connect() records it.

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

    def launch_writer(self, **changes):
        command = self.make_command(**{**dict(assignment_id="implement", role="backend", model="claude-opus-5-5",
                                              effort="high", write=True, commit_message="Add fixture notes",
                                              sandbox=sys.executable), **changes})
        with patch.object(factory, "Rex", FakeRex):
            factory.launch(self.path, task="task", assignment_id="implement", role="backend", goal="Add fixture notes",
                cwd=str(self.repo), command=command, purpose="work", binary=sys.executable, server="unix:///test.sock")
        actual = [call for call in FakeRex.calls if call[0] == "new" or call[:2] == ("window", "new")][-1]
        argv = actual[actual.index("--") + 1:]
        argv = argv[argv.index("--") + 1:]
        return claude_worker.parser().parse_args(argv[4:])

    def execute_writer(self, args, *, edit=True):
        async def spawn(*command, **options):
            self.spawns.append((command, options))
            if edit:
                (self.repo / "NOTES.md").write_text("Notes\n")
            return FakeProcess(writer_events(str(self.repo), args.native_session, model=args.model))
        with redirect_stdout(StringIO()):
            return asyncio.run(claude_worker.execute(args, spawn=spawn, probe=self.probe, clock=lambda: 1000,
                timer=lambda: 10, git_scope=lambda *a, **k: nullcontext(), scan=lambda *a, **k: None))

    def test_writer_edits_are_committed_by_the_factory_and_collected(self):
        args = self.launch_writer()
        receipt = self.execute_writer(args)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual((receipt["schema_version"], receipt["writer"], receipt["commit"]["state"]), (2, True, "created"))
        self.assertEqual(receipt["commit"]["parent"], self.candidate)
        self.assertNotEqual(receipt["candidate"], self.candidate)
        self.assertEqual(factory.git_commit(".", "HEAD"), receipt["candidate"])
        self.assertEqual(self.git("show", receipt["candidate"] + ":NOTES.md").stdout, "Notes\n")
        command = self.spawns[-1][0]
        self.assertEqual(command[command.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(command[command.index("--tools") + 1:command.index("--tools") + 6], ("Read", "Edit", "Write", "Glob", "Grep"))
        self.assertEqual((command[command.index("--model") + 1], command[command.index("--effort") + 1]), ("claude-opus-5-5", "high"))
        record = factory.load_task(self.path)
        with patch.object(factory, "status", return_value=dict(state="exited", exit=dict(exit_code=0))):
            collected = factory.collect_claude(self.path, "implement", self.rex, record, record["assignments"]["implement"])
        result = collected["assignments"]["implement"]["result"]
        self.assertEqual((result["outcome"], result["candidate"]), ("completed", receipt["candidate"]))
        self.assertTrue(collected["assignments"]["implement"]["native_collected"])

    def test_writer_without_edits_records_an_unchanged_commit(self):
        receipt = self.execute_writer(self.launch_writer(), edit=False)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual((receipt["commit"]["state"], receipt["candidate"]), ("unchanged", self.candidate))

    def test_writer_mode_requires_a_writing_role_commit_message_and_supported_effort(self):
        for changes, message in ((dict(role="qa"), "does not permit writer"),
                                 (dict(commit_message=None), "--commit-message"),
                                 (dict(sandbox=None), "commit sandbox"),
                                 (dict(effort="turbo"), "supported effort"),
                                 (dict(base=self.candidate), "--base is for code review")):
            with self.subTest(changes=changes), self.assertRaisesRegex(factory.FactoryError, message):
                self.make_command(**{**dict(assignment_id="implement", role="backend", write=True,
                                            commit_message="Add fixture notes", sandbox=sys.executable), **changes})

    def test_reviewers_accept_any_discovered_model_and_supported_effort(self):
        command = self.make_command(assignment_id="opus-review", model="claude-opus-5-5", effort="max")
        self.assertEqual(command[command.index("--model") + 1], "claude-opus-5-5")
        self.assertNotIn("--write", command)

    def collect(self):
        self.record=factory.load_task(self.path)
        self.assignment=self.record["assignments"]["inspect"]
        with patch.object(factory,"status",return_value=dict(state="exited")):
            return factory.collect_claude(self.path,"inspect",self.rex,self.record,self.assignment)

    def test_lost_pane_defers_to_the_completed_receipt(self):
        self.record=factory.load_task(self.path)
        self.assignment=self.record["assignments"]["inspect"]
        lost=dict(state="lost",reason="Rex server restarted after launch")
        with patch.object(factory,"status",return_value=lost),self.assertRaises(factory.FactoryError):
            factory.collect_claude(self.path,"inspect",self.rex,self.record,self.assignment)
        self.assertEqual(self.execute()["state"],"completed")
        self.record=factory.load_task(self.path)
        self.assignment=self.record["assignments"]["inspect"]
        with patch.object(factory,"status",return_value=lost):
            result=factory.collect_claude(self.path,"inspect",self.rex,self.record,self.assignment)
        self.assertTrue(result["assignments"]["inspect"]["native_collected"])

    def test_execute_collect_reconnect_and_metadata(self):
        self.assertEqual(self.execute()["state"],"completed")
        self.assertEqual(self.prompt, self.args.brief.read_text())
        command=self.spawns[0][0]
        for flag in ["--safe-mode","--strict-mcp-config","--no-session-persistence"]:
            self.assertIn(flag,command)
        self.assertNotIn("--restricted",command)
        self.assertEqual(command[command.index("--permission-mode")+1],"bypassPermissions")
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

    def test_thinking_worker_collection_preserves_raw_and_actual_usage(self):
        self.events[1:1] = thinking_events(self.args.native_session)
        self.process = FakeProcess(self.events)
        receipt = self.execute()
        self.assertEqual(receipt["state"], "completed")
        self.assertEqual(receipt["usage"], dict(input_tokens=2, output_tokens=20))
        self.assertEqual(receipt["model_usage"], self.events[-1]["modelUsage"])
        raw = envelope_bytes([json.dumps(event) + "\n" for event in self.events])
        self.assertEqual(self.args.events.read_bytes(), raw)
        result = self.collect()
        self.assertTrue(result["assignments"]["inspect"]["native_collected"])
        self.assertEqual(self.collect(), result)
        self.assertEqual(self.args.events.read_bytes(), raw)

    def assert_telemetry_failure(self, telemetry, position):
        self.events.insert(position, telemetry)
        self.process = FakeProcess(self.events)
        receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIsNone(receipt["report"])
        self.assertIsNone(receipt["candidate"])
        before = (self.path.read_bytes(), self.args.receipt.read_bytes(), self.args.events.read_bytes())
        with self.assertRaises(factory.FactoryError):
            self.collect()
        self.assertEqual((self.path.read_bytes(), self.args.receipt.read_bytes(), self.args.events.read_bytes()), before)
        self.assertFalse(factory.load_task(self.path)["assignments"]["inspect"]["native_collected"])

    def test_malformed_thinking_never_collects(self):
        telemetry = thinking_events(self.args.native_session)[0]
        telemetry["estimated_tokens_delta"] = True
        self.assert_telemetry_failure(telemetry, 1)

    def test_foreign_thinking_never_collects(self):
        self.assert_telemetry_failure(thinking_events("foreign")[0], 1)

    def test_late_thinking_never_collects(self):
        self.assert_telemetry_failure(thinking_events(self.args.native_session)[0], len(self.events))

    def test_prior_failed_receipt_stays_failed_even_when_stream_now_decodes(self):
        self.events[1:1] = thinking_events(self.args.native_session)
        self.process = FakeProcess(self.events)
        # Model the original strict decoder's refusal, without changing its evidence.
        with patch.object(native_claude, "parse_execution", side_effect=native_claude.ClaudeProbeError("Unknown subtype")):
            self.assertEqual(self.execute()["state"], "failed")
        native = native_claude.parse_execution(claude_worker.read_events(self.args.events),
            session_id=self.args.native_session, cwd=str(self.repo), model=MODEL, max_turns=3)
        self.assertEqual(native["report"]["outcome"], "approved")
        before = (self.path.read_bytes(), self.args.receipt.read_bytes(), self.args.events.read_bytes())
        with self.assertRaises(factory.FactoryError):
            self.collect()
        self.assertEqual((self.path.read_bytes(), self.args.receipt.read_bytes(), self.args.events.read_bytes()), before)
        self.assertFalse(factory.load_task(self.path)["assignments"]["inspect"]["native_collected"])

    def test_deep_native_event_saves_controlled_failed_receipt(self):
        self.process = FakeProcess([])
        line = RECURSIVE_JSON + "\n"
        self.process.lines = iter([(json.dumps(self.events[0]) + "\n").encode(), line.encode()])
        with recursion_on(line):
            receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("Malformed Claude event", receipt["error"])
        self.assertIsNone(receipt["report"])
        self.assertEqual(json.loads(self.args.receipt.read_text()), receipt)
        retained = self.args.events.read_bytes()
        self.assertEqual(json.loads(retained.splitlines()[-1])["data"], line)
        self.assertTrue(self.process.signals)
        with self.assertRaises(factory.FactoryError):
            self.collect()
        self.assertEqual(self.args.events.read_bytes(), retained)

    def test_cumulative_stream_exact_limit_and_collection_refusal(self):
        self.events[1:1] = [self.events[3]] * 100
        self.process = FakeProcess(self.events)
        raw = envelope_bytes([json.dumps(event) + "\n" for event in self.events])
        with patch.object(claude_worker, "RETAINED_STREAM_LIMIT", len(raw)):
            self.assertEqual(self.execute()["state"], "completed")
            self.assertEqual(self.args.events.read_bytes(), raw)
        before = (self.path.read_bytes(), self.args.receipt.read_bytes(), raw)
        with patch.object(claude_worker, "RETAINED_STREAM_LIMIT", len(raw) - 1):
            with self.assertRaises(factory.FactoryError):
                self.collect()
        self.assertEqual((self.path.read_bytes(), self.args.receipt.read_bytes(), self.args.events.read_bytes()), before)
        with patch.object(claude_worker, "RETAINED_STREAM_LIMIT", len(raw)):
            self.assertTrue(self.collect()["assignments"]["inspect"]["native_collected"])

    def test_cumulative_stream_over_limit_stops_before_write(self):
        self.events[1:1] = [self.events[3]] * 100
        self.process = FakeProcess(self.events)
        raw = envelope_bytes([json.dumps(event) + "\n" for event in self.events])
        prefix = envelope_bytes([json.dumps(event) + "\n" for event in self.events[:-1]])
        with patch.object(claude_worker, "RETAINED_STREAM_LIMIT", len(raw) - 1):
            receipt = self.execute()
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("retained-stream byte limit", receipt["error"])
        self.assertIsNone(receipt["report"])
        self.assertTrue(self.process.signals)
        self.assertEqual(self.args.events.read_bytes(), prefix)
        self.assertEqual(json.loads(self.args.receipt.read_text()), receipt)
        before = (self.path.read_bytes(), self.args.receipt.read_bytes(), prefix)
        with self.assertRaises(factory.FactoryError):
            self.collect()
        self.assertEqual((self.path.read_bytes(), self.args.receipt.read_bytes(), self.args.events.read_bytes()), before)

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
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(native_claude.OVERRIDE_PREFIXES)}
        cleanup = patch.dict(os.environ, environment, clear=True)
        cleanup.start()
        self.addCleanup(cleanup.stop)
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

    def run_claude_writer(self, session, *, exit_code=0):
        f = self.fixture
        with patch.object(factory.uuid, "uuid4", return_value=uuid.UUID(session)):
            command = factory.claude_command(f.path, task="deliver", assignment_id="claude-writer", claude=sys.executable,
                model="claude-opus-5-5", effort="high", cwd=str(f.repo), role="backend", write=True,
                commit_message="Add fixture notes", sandbox=sys.executable)
        record, assignment = factory.launch(f.path, task="deliver", assignment_id="claude-writer", role="backend",
            goal="Implement the fixture change", cwd=str(f.repo), command=command, purpose="work",
            binary=sys.executable, server="unix:///fixture.sock")
        replacements = {"{repository}": str(f.repo), "{cwd}": str(f.repo), "{brief}": assignment["brief"],
            "{brief_sha256}": assignment["request"]["instructions_sha256"], "{candidate}": record["inputs"]["candidate"],
            "{requirements}": record["inputs"]["requirements"],
            "{repository_identity}": json.dumps(record["admission"]["repository_identity"], sort_keys=True)}
        args = claude_worker.parser().parse_args([replacements.get(part, part) for part in command[4:]])
        async def spawn(*command, **options):
            return FakeProcess(writer_events(str(f.repo), session, model="claude-opus-5-5"), exit_code=exit_code)
        def probe(binary, cwd):
            return dict(schema_version=1, kind="claude-native-auth", harness="claude", binary=str(binary), cwd=str(cwd),
                cli_version="2.1.319 (Claude Code)", started_at=1000, observed_at=1000, probe_seconds=0,
                auth=dict(loggedIn=True, authMethod="claude.ai", apiProvider="firstParty"))
        with redirect_stdout(StringIO()):
            receipt = asyncio.run(claude_worker.execute(args, spawn=spawn, probe=probe, clock=lambda: 1000, timer=lambda: 10,
                git_scope=lambda *a, **k: nullcontext(), scan=lambda *a, **k: None))
        return record, assignment, receipt

    def test_claude_writer_contributes_without_breaking_integration_approvals(self):
        writer_session = "00000000-0000-0000-0000-000000000002"
        record, assignment, receipt = self.run_claude_writer(writer_session)
        self.assertEqual((receipt["state"], receipt["commit"]["state"]), ("completed", "unchanged"), receipt["error"])
        with patch.object(factory, "status", return_value=dict(state="exited", exit=dict(exit_code=0))):
            record = factory.collect_claude(self.fixture.path, "claude-writer", None, record, assignment)
            self.assertEqual(integration.contributor_context("claude-writer", record, record["assignments"]["claude-writer"]),
                             writer_session)
            selected = integration.approvals(record, "release", ["claude-qa"])
        self.assertEqual(selected["claude-qa"]["context"], SESSION)

    def test_failed_claude_writer_stays_a_contributor_under_its_frozen_session(self):
        writer_session = "00000000-0000-0000-0000-000000000003"
        record, assignment, receipt = self.run_claude_writer(writer_session, exit_code=1)
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(integration.contributor_context("claude-writer", record, assignment), writer_session)

    def test_claude_writer_cannot_approve_its_own_work(self):
        record, assignment, _ = self.run_claude_writer(SESSION)
        with patch.object(factory, "status", return_value=dict(state="exited", exit=dict(exit_code=0))):
            record = factory.collect_claude(self.fixture.path, "claude-writer", None, record, assignment)
            with self.assertRaisesRegex(factory.FactoryError, "independent of contributors"):
                integration.approvals(record, "release", ["claude-qa"])
