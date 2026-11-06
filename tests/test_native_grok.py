"""Native Grok metadata fixtures only; no provider calls or live Rex sessions."""
import asyncio
import copy
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import native_grok


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
            self.assertEqual(args[1:], ("--no-auto-update", "--no-subagents", "--disable-web-search", "--permission-mode", "dontAsk",
                "--sandbox", "read-only", "agent", "--no-leader", "stdio"))
            self.assertEqual(options["env"]["GROK_WORKTREE_AUTO_GC"], "0")
            self.assertEqual(options["cwd"], str(self.cwd.resolve()))
            return process
        times = iter((10, 12))
        result = await native_grok.probe(sys.executable, self.cwd, spawn=spawn, clock=lambda: 100, timer=lambda: next(times))
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
            await native_grok.probe(sys.executable, self.cwd, spawn=AsyncMock(side_effect=OSError("private")))


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
