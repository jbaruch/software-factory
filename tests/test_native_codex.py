"""Native metadata transport fixtures; never start Codex or contact an account."""
import asyncio
import json
import sys
import unittest
from unittest.mock import patch

import native_codex


class Writer:
    def __init__(self, process):
        self.process = process

    def write(self, data):
        request = json.loads(data)
        self.process.requests.append(request)
        if "id" in request:
            result = self.process.replies[request["method"]]
            reply = result.pop(0) if isinstance(result, list) else result
            self.process.stdout.feed_data((json.dumps({"id": request["id"], **reply}) + "\n").encode())

    async def drain(self):
        return None


class Process:
    def __init__(self, replies):
        self.stdout = asyncio.StreamReader()
        self.stdin = Writer(self)
        self.requests, self.replies = [], replies
        self.returncode = None
        self.terminated = False

    def terminate(self):
        self.terminated, self.returncode = True, 0

    async def wait(self):
        return self.returncode


class NativeMetadata(unittest.IsolatedAsyncioTestCase):
    def replies(self):
        return {"initialize": {"result": {"userAgent": "fixture-native/1"}},
                "account/read": {"result": {"account": {"type": "chatgpt", "planType": "fixture"}}},
                "account/rateLimits/read": {"result": {"rateLimits": {"primary": None, "secondary": None}}},
                "model/list": [{"result": {"data": [{"model": "fixture-model"}], "nextCursor": "next"}},
                               {"result": {"data": [{"model": "fixture-other"}], "nextCursor": None}}]}

    async def run_probe(self, replies):
        process = Process(replies)

        async def spawn(*args, **kwargs):
            self.assertEqual(args[1:], ("app-server", "--listen", "stdio://"))
            return process

        times = iter((100, 102))
        result = await native_codex.probe(sys.executable, spawn=spawn, clock=lambda: 102,
                                           timer=lambda: next(times))
        return result, process

    async def test_probe_preserves_unknown_capacity_paginates_and_runs_no_model_turn(self):
        result, process = await self.run_probe(self.replies())
        self.assertEqual(result["readiness"], "unverified")
        self.assertEqual(result["observed_at"], 102)
        self.assertEqual(result["probe_seconds"], 2)
        self.assertEqual(result["models"], [{"model": "fixture-model"}, {"model": "fixture-other"}])
        self.assertIsNone(result["rate_limits"]["rateLimits"]["primary"])
        self.assertEqual([request["method"] for request in process.requests],
                         ["initialize", "initialized", "account/read", "account/rateLimits/read",
                          "model/list", "model/list"])
        self.assertEqual(process.requests[2]["params"], {"refreshToken": False})
        self.assertEqual(process.requests[-1]["params"]["cursor"], "next")
        self.assertTrue(process.terminated)

    async def test_rpc_failure_stops_and_cleans_up_only_the_owned_process(self):
        replies = self.replies()
        replies["account/rateLimits/read"] = {"error": {"code": 401, "message": "private fixture data"}}
        process = Process(replies)

        async def spawn(*args, **kwargs):
            return process

        with self.assertRaises(native_codex.CodexProbeError) as caught:
            await native_codex.probe(sys.executable, spawn=spawn, clock=lambda: 100)
        self.assertNotIn("private fixture data", str(caught.exception))
        self.assertEqual(process.requests[-1]["method"], "account/rateLimits/read")
        self.assertTrue(process.terminated)

    async def test_missing_catalog_is_not_an_empty_successful_inventory(self):
        replies = self.replies()
        replies["model/list"] = [{"result": {"nextCursor": None}}]
        with self.assertRaises(native_codex.CodexProbeError):
            await self.run_probe(replies)

    async def test_timeout_identifies_the_failed_observation_and_recovery(self):
        process = Process(self.replies())
        with patch.object(process.stdout, "readline", side_effect=TimeoutError):
            with self.assertRaisesRegex(native_codex.CodexProbeError, "initialize timed out; inspect"):
                await native_codex.exchange(process, 0, "initialize", {})

    async def test_malformed_protocol_identifies_recovery_and_cleans_up_owned_child(self):
        for line in (b'{"truncated":\n', b'\xff\n'):
            process = Process(self.replies())
            async def spawn(*args, **kwargs):
                return process
            with patch.object(process.stdout, "readline", return_value=line):
                with self.subTest(line=line), self.assertRaisesRegex(native_codex.CodexProbeError,
                        "Malformed native Codex protocol; inspect"):
                    await native_codex.probe(sys.executable, spawn=spawn, clock=lambda: 100,
                                             timer=lambda: 10)
            self.assertTrue(process.terminated)

    async def test_missing_binary_identifies_recovery_before_starting_a_child(self):
        with patch.object(native_codex.Path, "resolve", side_effect=FileNotFoundError), patch.object(
                native_codex.asyncio, "create_subprocess_exec") as spawn:
            with self.assertRaisesRegex(native_codex.CodexProbeError, "supply an existing executable"):
                await native_codex.probe("/missing/native-codex", spawn=spawn)
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
