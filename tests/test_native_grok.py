"""Native Grok protocol fixtures; no provider calls or live Rex sessions."""
import asyncio
import copy
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import native_grok


async def plain_configuration(binary, cwd):
    return dict(schema_version=2, kind="grok-subscription-configuration", cwd=str(Path(cwd).resolve()),
                grok_home=str(Path(cwd).resolve()), cli_version="fixture", layers=[],
                sandbox="strict", tools=["read_file"], no_executable_customizations=True,
                route="cached-session-without-routing-overrides")


def replies():
    return {"initialize": dict(result=dict(protocolVersion=1, agentCapabilities={"loadSession": True},
        authMethods=[{"id": "cached_token"}, {"id": "grok.com"}], _meta=dict(agentVersion="fixture-native/1",
        modelState=dict(currentModelId="fixture-model", availableModels=[dict(modelId="fixture-model", name="Fixture",
            _meta=dict(reasoningEfforts=[{"value": "high"}, {"value": "low"}]))])))),
        "authenticate": dict(result={"_meta": dict(auth_mode="Oidc", subscription_tier="Fixture subscriber",
            gate=None, backend_billed=False, email="private@example.invalid", team_id="private-team", token="private-token")})}


class Process:
    def __init__(self, responses):
        self.responses, self.requests = copy.deepcopy(responses), []
        self.stdin, self.stdout = self, asyncio.StreamReader()
        self.returncode, self.terminated = None, False

    def write(self, data):
        request = json.loads(data)
        self.requests.append(request)
        response = dict(jsonrpc="2.0", id=request["id"], **self.responses[request["method"]])
        self.stdout.feed_data((json.dumps(response) + "\n").encode())

    async def drain(self):
        pass

    def terminate(self):
        self.terminated, self.returncode = True, 0

    async def wait(self):
        return self.returncode


class NativeGrok(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cwd = Path(directory.name)
        environment = {k: v for k, v in os.environ.items() if k not in native_grok.API_KEY_ENVIRONMENT}
        cleanup = patch.dict(os.environ, environment, clear=True)
        cleanup.start()
        self.addCleanup(cleanup.stop)

    async def run_probe(self, responses=None, process=None):
        process = process or Process(replies() if responses is None else responses)
        async def spawn(*args, **options):
            self.assertEqual(args[1:8], ("--no-auto-update", "--no-subagents", "--disable-web-search", "--permission-mode", "dontAsk",
                "--sandbox", "strict"))
            self.assertEqual(args[-3:], ("agent", "--no-leader", "stdio"))
            self.assertEqual(args[args.index("--tools") + 1], "read_file")
            for denied in ("Write", "Edit", "Bash", "MCPTool(*)", "Task", "Workflow"):
                self.assertIn(denied, args)
            self.assertEqual(options["env"]["GROK_WORKTREE_AUTO_GC"], "0")
            self.assertEqual(options["cwd"], str(self.cwd.resolve()))
            return process
        times = iter((10, 12))
        result = await native_grok.probe(sys.executable, self.cwd, spawn=spawn, configuration=plain_configuration,
                                         clock=lambda: 100, timer=lambda: next(times))
        return result, process

    async def test_cached_auth_models_and_efforts_request_no_session_or_model_turn(self):
        result, process = await self.run_probe()
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual((result["observed_at"], result["probe_seconds"]), (100, 2))
        self.assertEqual([r["method"] for r in process.requests], ["initialize", "authenticate"])
        self.assertEqual(process.requests[1]["params"], dict(methodId="cached_token", _meta=dict(headless=True)))
        self.assertEqual(result["models"][0]["modelId"], "fixture-model")
        self.assertEqual(result["models"][0]["_meta"]["reasoningEfforts"], [{"value": "high"}, {"value": "low"}])
        self.assertEqual(result["authentication"]["auth_mode"], "Oidc")
        self.assertIsNone(result["quota_observation"])
        self.assertEqual(result["readiness"], "unverified")
        for hidden in ("private@example.invalid", "private-team", "private-token"):
            self.assertNotIn(hidden, json.dumps(result))
        self.assertTrue(process.terminated)

    async def test_metadata_child_preserves_selected_profile_without_host_credentials(self):
        process = Process(replies())
        selected = str(self.cwd / "selected-profile")
        async def spawn(*args, **options):
            self.assertEqual(options["env"]["GROK_HOME"], selected)
            self.assertNotIn("GH_TOKEN", options["env"])
            return process
        with patch.dict(os.environ, {"GROK_HOME": selected, "GH_TOKEN": "fixture-private-token"}):
            await native_grok.probe(sys.executable, self.cwd, spawn=spawn, configuration=plain_configuration)

    async def test_empty_catalog_and_access_gate_preserve_unavailable_facts(self):
        response = replies()
        response["initialize"]["result"]["_meta"]["modelState"]["availableModels"] = []
        response["authenticate"]["result"]["_meta"]["gate"] = dict(
            message="Fixture access hold", label="Inspect plan", url="https://example.invalid/?token=private")
        result, _ = await self.run_probe(response)
        self.assertEqual(result["models"], [])
        self.assertEqual(result["authentication"]["gate"], dict(message="Fixture access hold", label="Inspect plan"))
        for hidden in ("private@example.invalid", "private-team", "private-token", "token=private"):
            self.assertNotIn(hidden, json.dumps(result))
        self.assertEqual(result["readiness"], "unverified")

    async def test_api_key_environment_refuses_even_empty_overrides_before_spawn(self):
        for name in native_grok.API_KEY_ENVIRONMENT:
            with self.subTest(name=name), patch.dict(os.environ, {name: ""}):
                spawn = AsyncMock()
                with self.assertRaisesRegex(native_grok.GrokProbeError, "Unset XAI_API_KEY and GROK_CODE_XAI_API_KEY"):
                    await native_grok.probe(sys.executable, self.cwd, spawn=spawn)
                spawn.assert_not_called()

    async def test_missing_cached_auth_and_api_key_mode_never_fall_back(self):
        for kind in ("missing", "duplicate", "api-key"):
            response = replies()
            if kind == "missing":
                response["initialize"]["result"]["authMethods"] = [{"id": "grok.com"}]
            elif kind == "duplicate":
                response["initialize"]["result"]["authMethods"] = [{"id": "cached_token"}, {"id": "cached_token"}]
            else:
                response["authenticate"]["result"]["_meta"]["auth_mode"] = "ApiKey"
            process = Process(response)
            with self.subTest(kind=kind), self.assertRaises(native_grok.GrokProbeError):
                await self.run_probe(process=process)
            self.assertFalse(any(r["method"] not in {"initialize", "authenticate"} for r in process.requests))
            self.assertTrue(process.terminated)

    async def test_malformed_or_missing_native_fields_refuse_metadata(self):
        base = replies()
        cases = []
        for field, value in (("protocolVersion", True), ("protocolVersion", 2), ("_meta", None), ("agentCapabilities", [])):
            response = copy.deepcopy(base)
            response["initialize"]["result"][field] = value
            cases.append(response)
        for field in ("auth_mode", "subscription_tier", "gate", "backend_billed"):
            response = copy.deepcopy(base)
            del response["authenticate"]["result"]["_meta"][field]
            cases.append(response)
        for catalog in (None, {}, dict(currentModelId=" ", availableModels=[]),
                        dict(currentModelId="fixture", availableModels=[None]),
                        dict(currentModelId="fixture", availableModels=[{"modelId": ""}]),
                        dict(currentModelId="fixture", availableModels=[{"modelId": "same"}, {"modelId": "same"}]),
                        dict(currentModelId="fixture", availableModels=[dict(modelId="fixture", _meta=dict(reasoningEfforts=[{"value": ""}]))]),
                        dict(currentModelId="fixture", availableModels=[dict(modelId="fixture", _meta=dict(reasoningEfforts=[{"value": "high"}, {"value": "high"}]))])):
            response = copy.deepcopy(base)
            response["initialize"]["result"]["_meta"]["modelState"] = catalog
            cases.append(response)
        for index, response in enumerate(cases):
            process = Process(response)
            with self.subTest(index=index), self.assertRaises(native_grok.GrokProbeError):
                await self.run_probe(process=process)
            self.assertTrue(process.terminated)

    async def test_protocol_rejects_ambiguous_json_identity_and_client_execution(self):
        for line in (b'{"jsonrpc":"2.0","id":0,"result":{},"result":{}}\n',
                     b'{"jsonrpc":"2.0","id":0,"result":{"unused":[NaN]}}\n',
                     b'{"jsonrpc":"2.0","id":0,"result":{"unused":1e999}}\n',
                     b'{"jsonrpc":"2.0","id":false,"result":{}}\n',
                     b'{"jsonrpc":"2.0","id":0,"method":"terminal/create"}\n',
                     b'[]\n', b'\xff\n', b''):
            process = Process(replies())
            with self.subTest(line=line), patch.object(process.stdout, "readline", return_value=line):
                with self.assertRaises(native_grok.GrokProbeError):
                    await self.run_probe(process=process)
            self.assertTrue(process.terminated)

    async def test_rpc_error_does_not_echo_private_provider_messages(self):
        response = replies()
        response["authenticate"] = dict(error=dict(code=401, message="private provider text"))
        with self.assertRaisesRegex(native_grok.GrokProbeError, "rejected authenticate; inspect native sign-in") as caught:
            await self.run_probe(response)
        self.assertNotIn("private", str(caught.exception))

    async def test_pipe_and_timeout_failures_name_recovery_and_stop_the_child(self):
        for target, method, error in (("stdin", "write", BrokenPipeError("private")),
                                     ("stdin", "drain", ConnectionResetError("private")),
                                     ("stdout", "readline", ConnectionResetError("private")),
                                     ("stdout", "readline", TimeoutError("private"))):
            process = Process(replies())
            with self.subTest(method=method), patch.object(getattr(process, target), method, side_effect=error):
                with self.assertRaises(native_grok.GrokProbeError) as caught:
                    await self.run_probe(process=process)
            self.assertNotIn("private", str(caught.exception))
            self.assertTrue(process.terminated)

    async def test_cleanup_exit_races_still_observe_the_owned_child_exit(self):
        class RaceProcess(Process):
            waits = 0
            def terminate(self):
                raise ProcessLookupError("fixture exit")
            def kill(self):
                raise ProcessLookupError("fixture exit")
            async def wait(self):
                self.waits += 1
                if self.waits == 1:
                    raise TimeoutError("fixture deadline")
                self.returncode = 0
                return 0
        process = RaceProcess(replies())
        result, _ = await self.run_probe(process=process)
        self.assertEqual(result["readiness"], "unverified")
        self.assertEqual(process.waits, 2)
        self.assertEqual(process.returncode, 0)

    async def test_bad_paths_and_spawn_failures_name_recovery(self):
        for binary, cwd in (("relative", self.cwd), (self.cwd / "missing", self.cwd),
                            (self.cwd, self.cwd), (sys.executable, self.cwd / "missing")):
            spawn = AsyncMock()
            with self.subTest(binary=str(binary)), self.assertRaises(native_grok.GrokProbeError):
                await native_grok.probe(binary, cwd, spawn=spawn)
            spawn.assert_not_called()
        with self.assertRaisesRegex(native_grok.GrokProbeError, "restore executable access and local process resources"):
            await native_grok.probe(sys.executable, self.cwd, spawn=AsyncMock(side_effect=OSError("private")),
                                    configuration=plain_configuration)


class NativeGrokConfiguration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cwd = Path(temporary.name).resolve()
        self.config = self.cwd / "config.toml"
        self.config.write_text('[models]\ndefault = "grok-4.7"\n')
        self.report = dict(grokVersion="1.0.77", cwd=str(self.cwd), projectTrusted=True, experiments=[],
                           hooks=[], plugins=[], mcpServers=[], lspServers=[],
                           configSources=dict(layers=[dict(role="user", path=str(self.config))]))
        environment = {key: value for key, value in os.environ.items()
                       if key not in native_grok.API_KEY_ENVIRONMENT and not key.startswith("GROK_")}
        clean = patch.dict(os.environ, environment, clear=True)
        clean.start()
        self.addCleanup(clean.stop)

    def test_plain_native_configuration_retains_source_identity_without_contents(self):
        result = native_grok.parse_configuration(self.report, self.cwd)
        self.assertEqual(result["route"], "cached-session-without-routing-overrides")
        self.assertEqual(result["layers"][0]["path"], str(self.config))
        self.assertEqual(len(result["layers"][0]["sha256"]), 64)
        self.assertNotIn('default =', json.dumps(result))

    def test_builtin_model_key_env_key_endpoint_and_auth_overrides_refuse(self):
        for config in ('[model."grok-4.7"]\napi_key="private-fixture"\n',
                       '[model."grok-4.7"]\nenv_key="CUSTOM_NATIVE_KEY"\n',
                       '[model."grok-4.7"]\nbase_url="https://example.invalid"\n',
                       '[endpoints]\nmodels_base_url="https://example.invalid"\n',
                       '[auth]\nauth_provider_command="fixture-helper"\n',
                       '[grok_com_config]\nauth_provider_command="fixture-helper"\n',
                       '[[experiments]]\nname="fixture"\n'):
            self.config.write_text(config)
            with self.subTest(config=config), self.assertRaisesRegex(native_grok.GrokProbeError, "overrides are unsupported") as caught:
                native_grok.parse_configuration(self.report, self.cwd)
            self.assertNotIn("private-fixture", str(caught.exception))

    def test_opaque_malformed_and_unreadable_layers_cannot_prove_routing(self):
        for layer in (None, {}, dict(path="relative.toml", role="user"),
                      dict(path=str(self.cwd / "missing.toml"), role="user")):
            report = {**self.report, "configSources": dict(layers=[layer])}
            with self.subTest(layer=layer), self.assertRaises(native_grok.GrokProbeError):
                native_grok.parse_configuration(report, self.cwd)
        for report in ({**self.report, "experiments": [dict(name="fixture")]},
                       {**self.report, "cwd": str(self.cwd / "other")}, {**self.report, "configSources": {}},
                       {**self.report, "projectTrusted": False}):
            with self.assertRaises(native_grok.GrokProbeError):
                native_grok.parse_configuration(report, self.cwd)
        self.config.write_bytes(b'\xff')
        with self.assertRaisesRegex(native_grok.GrokProbeError, "readable TOML"):
            native_grok.parse_configuration(self.report, self.cwd)

    async def test_native_inspection_preserves_profile_and_filters_host_credentials(self):
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(json.dumps(self.report).encode(), b"")))
        spawn = AsyncMock(return_value=process)
        with patch.dict(os.environ, {"GROK_HOME": str(self.cwd), "GH_TOKEN": "fixture-private-token"}):
            result = await native_grok.subscription_configuration(sys.executable, self.cwd, spawn=spawn)
        self.assertEqual(spawn.call_args.args[-2:], ("inspect", "--json"))
        self.assertEqual(spawn.call_args.args[spawn.call_args.args.index("--tools") + 1], "read_file")
        self.assertIn("MCPTool(*)", spawn.call_args.args)
        self.assertEqual(spawn.call_args.kwargs["env"]["GROK_HOME"], str(self.cwd))
        self.assertNotIn("GH_TOKEN", spawn.call_args.kwargs["env"])
        self.assertEqual(result["grok_home"], str(self.cwd))
        self.assertGreaterEqual(result["probe_seconds"], 0)

    def test_read_options_keep_profile_credentials_outside_model_file_access(self):
        selected = self.cwd / "selected-profile"
        with patch.dict(os.environ, {"GROK_HOME": str(selected)}):
            options = native_grok.read_options(self.cwd)
        self.assertIn("Read(~/.grok/**)", options)
        self.assertIn(f"Read({selected}/**)", options)
        self.assertIn("Read(./**)", options)
        with patch.dict(os.environ, {"GROK_HOME": str(self.cwd / "profile[ambiguous]")}), self.assertRaises(native_grok.GrokProbeError):
            native_grok.read_options(self.cwd)

    async def test_routing_environment_overrides_refuse_before_inspection(self):
        for key in ("GROK_CONFIG", "GROK_CONFIG_PATH", "GROK_AUTH", "GROK_MODELS_BASE_URL",
                    "GROK_MODELS_LIST_URL", "GROK_AUTH_PROVIDER_COMMAND", "GROK_CLI_CHAT_PROXY_BASE_URL",
                    "GROK_OIDC_ISSUER", "GROK_OIDC_CLIENT_ID", "GROK_UNKNOWN_ROUTE_OVERRIDE"):
            spawn = AsyncMock()
            with self.subTest(key=key), patch.dict(os.environ, {key: ""}), self.assertRaisesRegex(native_grok.GrokProbeError, "Unset"):
                await native_grok.subscription_configuration(sys.executable, self.cwd, spawn=spawn)
            spawn.assert_not_called()

    def test_enabled_executable_surfaces_and_unknown_discovery_refuse(self):
        for key, value in (("hooks", [dict(event="session_start")]),
                           ("mcpServers", [dict(name="fixture")]),
                           ("lspServers", [dict(name="fixture")]),
                           ("plugins", [dict(enabled=True)]), ("plugins", [dict(name="unknown")]),
                           ("mcpServers", None)):
            with self.subTest(key=key), self.assertRaisesRegex(native_grok.GrokProbeError, "executable"):
                native_grok.parse_configuration({**self.report, key: value}, self.cwd)

    async def test_committed_project_mcp_and_hook_commands_never_start_metadata_agent(self):
        for name, content in ((".grok/config.toml", '[mcp_servers.fixture]\ncommand="touch"\nargs=["candidate-command-ran"]\n'),
                              (".mcp.json", '{"mcpServers":{"fixture":{"command":"touch","args":["candidate-command-ran"]}}}\n'),
                              (".claude/settings.json", '{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"touch candidate-command-ran"}]}]}}\n')):
            with self.subTest(name=name):
                directory = self.cwd / name.replace("/", "-")
                directory.mkdir()
                source = directory / name
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(content)
                with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}):
                    for command in (("init", "-q", "--initial-branch=main"), ("add", "--all"),
                                    ("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                     "-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false", "commit", "-qm", "Fixture")):
                        subprocess.run(["git", "-C", str(directory), *command], check=True, capture_output=True)
                report = {**self.report, "cwd": str(directory)}
                origin = dict(type="project", path=str(source))
                if name.endswith("settings.json"):
                    report["hooks"] = [dict(event="session_start", hookType="command", source=origin)]
                else:
                    report["mcpServers"] = [dict(name="fixture", transport="stdio", source=origin)]
                process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(json.dumps(report).encode(), b"")))
                inspect_spawn, metadata_spawn = AsyncMock(return_value=process), AsyncMock()
                async def inspect(binary, cwd):
                    return await native_grok.subscription_configuration(binary, cwd, spawn=inspect_spawn)
                with self.assertRaisesRegex(native_grok.GrokProbeError, "executable"):
                    await native_grok.probe(sys.executable, directory, spawn=metadata_spawn, configuration=inspect)
                metadata_spawn.assert_not_called()
                self.assertFalse((directory / "candidate-command-ran").exists())

    async def test_inspection_failure_or_timeout_never_exposes_native_output(self):
        spawn = AsyncMock(side_effect=OSError("private-fixture"))
        with self.assertRaisesRegex(native_grok.GrokProbeError, "inspect its native configuration") as caught:
            await native_grok.subscription_configuration(sys.executable, self.cwd, spawn=spawn)
        self.assertNotIn("private-fixture", str(caught.exception))
        process = SimpleNamespace(returncode=None, communicate=AsyncMock(side_effect=TimeoutError()),
                                  wait=AsyncMock(return_value=130), kill=lambda: None)
        def interrupt(_):
            process.returncode = 130
        process.send_signal = interrupt
        with self.assertRaises(native_grok.GrokProbeError):
            await native_grok.subscription_configuration(sys.executable, self.cwd, spawn=AsyncMock(return_value=process))
        process.wait.assert_awaited_once()


class NativeGrokExecution(unittest.TestCase):
    def final(self) -> dict:
        # Sanitized final-event shape from the owned Grok 1.0.77 fixture.
        return dict(type="end", stopReason="end_turn", sessionId="fixture-session",
            requestId="fixture-request", cwd="/fixture", num_turns=2,
            usage=dict(input_tokens=11876, cache_read_input_tokens=29184, output_tokens=428,
                       reasoning_tokens=265, total_tokens=41488),
            modelUsage={"grok-4.7-build": dict(inputTokens=11876, outputTokens=428,
                cacheReadInputTokens=29184, modelCalls=2, costUSD=0.01391008)},
            structuredOutput=dict(outcome="approved", artifacts=["README.md"]))

    def parse(self, events):
        return native_grok.parse_execution(events, session_id="fixture-session", cwd="/fixture", max_turns=3)

    def test_only_final_structured_output_supplies_the_specialist_report(self):
        events = [dict(type="text", data='{"outcome":"blocked","artifacts":[]}'),
                  dict(type="tool_call", toolCallId="fixture-read", toolName="read_file"),
                  dict(type="tool_call_update", toolCallId="fixture-read", status="completed"), self.final()]
        result = self.parse(events)
        self.assertEqual(result["report"], dict(outcome="approved", artifacts=["README.md"]))
        self.assertEqual(result["session_id"], "fixture-session")
        self.assertEqual(result["request_id"], "fixture-request")
        self.assertEqual(result["turns"], 2)
        self.assertEqual(result["usage"], events[-1]["usage"])
        self.assertEqual(result["model_usage"], events[-1]["modelUsage"])

    def test_missing_ambiguous_failed_or_unbound_completion_refuses(self):
        final = self.final()
        cases = [[], [dict(type="text", data=json.dumps(final["structuredOutput"]))],
                 [final, final], [final, dict(type="text", data="late")],
                 [dict(type="error", message="private provider detail"), final], [None]]
        for key, value in (("sessionId", "other-session"), ("cwd", "/other"), ("requestId", " "),
                           ("stopReason", "max_turns"), ("num_turns", True), ("num_turns", 4),
                           ("structuredOutput", None), ("structuredOutput", {"outcome": "approved"})):
            damaged = copy.deepcopy(final)
            damaged[key] = value
            cases.append([damaged])
        for index, events in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(native_grok.GrokProbeError) as raised:
                self.parse(events)
            self.assertNotIn("private provider detail", str(raised.exception))
        with self.assertRaises(native_grok.GrokProbeError):
            self.parse([{**final, "cwd": None}])
        with self.assertRaises(native_grok.GrokProbeError):
            native_grok.parse_execution([{**final, "cwd": None}], session_id="fixture-session", cwd=None, max_turns=3)

    def test_absent_usage_stays_unknown_and_report_outcomes_stay_distinct(self):
        for outcome in ("completed", "approved", "findings", "blocked"):
            final = self.final()
            del final["usage"], final["modelUsage"]
            final["structuredOutput"] = dict(outcome=outcome, artifacts=[])
            result = self.parse([final])
            self.assertEqual(result["report"]["outcome"], outcome)
            self.assertIsNone(result["usage"])
            self.assertIsNone(result["model_usage"])

    def test_malformed_usage_and_final_reports_refuse(self):
        final = self.final()
        cases = []
        for key, value in (("usage", []), ("usage", {"input_tokens": True}),
                           ("usage", {"input_tokens": -1}), ("usage", {"input_tokens": 10 ** 400}),
                           ("modelUsage", []), ("modelUsage", {"fixture": {"costUSD": float("nan")}}),
                           ("modelUsage", {"fixture": {"modelCalls": True}}),
                           ("modelUsage", {"fixture": {"modelCalls": 10 ** 400}}),
                           ("structuredOutput", dict(outcome="accepted", artifacts=[])),
                           ("structuredOutput", dict(outcome="approved", artifacts=[" "])),
                           ("structuredOutput", dict(outcome="approved", artifacts=[], permission="merge"))):
            damaged = copy.deepcopy(final)
            damaged[key] = value
            cases.append(damaged)
        for index, event in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(native_grok.GrokProbeError):
                self.parse([event])


class NativeGrokCli(unittest.TestCase):
    def test_cli_failure_is_actionable_without_partial_json(self):
        output, error = StringIO(), StringIO()
        with patch.object(native_grok, "probe", new=AsyncMock(side_effect=native_grok.GrokProbeError("Inspect native sign-in"))), redirect_stdout(output), redirect_stderr(error):
            code = native_grok.main(["--grok", sys.executable, "--cwd", str(Path.cwd())])
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("native-grok: Inspect native sign-in", error.getvalue())


if __name__ == "__main__":
    unittest.main()
