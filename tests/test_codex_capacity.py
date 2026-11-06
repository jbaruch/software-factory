"""Offline native capacity fixtures; no provider or Rex access."""
import asyncio
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

import codex_capacity as capacity
import native_codex


def recorded() -> dict:
    pool = dict(limitId="codex", planType="plus", primary=dict(
        usedPercent=11, windowDurationMins=10080, resetsAt=1794000000), secondary=None)
    return dict(schema_version=1, observed_at=1793990000, binary="/fixture/codex", route="codex-native",
                initialization={"token": "PRIVATE_INITIALIZATION"},
                account={"account": {"type": "chatgpt", "email": "PRIVATE_EMAIL", "id": "PRIVATE_ID"}},
                rate_limits=dict(ordinaryUsageAllowed=True, rateLimits=pool,
                                 rateLimitsByLimitId={"codex": copy.deepcopy(pool)}),
                models=[dict(model="actual-native-model", id="picker-id", defaultReasoningEffort="high",
                             supportedReasoningEfforts=[{"reasoningEffort": "high", "description": "PRIVATE_DESCRIPTION"}])])


class Curation(unittest.TestCase):
    def test_real_shape_is_deduplicated_safe_and_does_not_mutate_input(self):
        raw = recorded()
        before = copy.deepcopy(raw)
        result = capacity.curate(raw)
        self.assertEqual(raw, before)
        self.assertEqual(result["observed_at"], raw["observed_at"])
        self.assertEqual((result["binary"], result["route"]), ("/fixture/codex", "codex-native"))
        self.assertEqual(result["account"], {"type": "chatgpt", "plan": "plus"})
        self.assertIs(result["ordinaryUsageAllowed"], True)
        self.assertEqual(result["pools"], [dict(limit_id="codex", primary=dict(
            used_percent=11, remaining_percent=89, duration_minutes=10080, resets_at=1794000000), secondary=None,
            rate_limit_reached_type=None, spend_control_reached=None)])
        self.assertEqual(result["models"], [dict(model="actual-native-model", id="picker-id",
            effort_ids=["high"], default_effort_id="high")])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("readiness", result)

    def test_overlapping_windows_and_distinct_pools_are_separate(self):
        raw = recorded()
        raw["rate_limits"]["rateLimits"] = None
        pools = raw["rate_limits"]["rateLimitsByLimitId"]
        pools["codex"]["secondary"] = dict(usedPercent=20.5, windowDurationMins=300, resetsAt=1793991000)
        pools["other"] = dict(limitId="other", primary={})
        result = capacity.curate(raw)["pools"]
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["primary"]["remaining_percent"], 89)
        self.assertEqual(result[0]["secondary"]["remaining_percent"], 79.5)
        self.assertIsNone(result[1]["primary"]["remaining_percent"])

    def test_unknown_empty_and_explicit_false_are_distinct(self):
        result = capacity.curate({"schema_version": 1})
        for key in ("observed_at", "binary", "route", "ordinaryUsageAllowed", "pools", "models"):
            self.assertIsNone(result[key])
        self.assertEqual(result["account"], dict(type=None, plan=None))
        raw: dict = dict(schema_version=1, account=None, models=[], rate_limits=dict(ordinaryUsageAllowed=False,
            rateLimits=dict(limitId=None, primary=dict(usedPercent=None), secondary=None)))
        result = capacity.curate(raw)
        self.assertIs(result["ordinaryUsageAllowed"], False)
        self.assertIsNone(result["pools"][0]["limit_id"])
        self.assertIsNone(result["pools"][0]["primary"]["remaining_percent"])
        self.assertEqual(result["models"], [])
        self.assertIsNone(result["pools"][0]["secondary"])

    def test_account_plan_sources_and_unknown_model_efforts(self):
        for location in ("account", "nested", "limits"):
            raw: dict = dict(schema_version=1, account={"account": {"type": "chatgpt"}}, models=[dict(model="native")])
            target = {"account": raw["account"], "nested": raw["account"]["account"],
                      "limits": raw.setdefault("rate_limits", {})}[location]
            target["planType"] = "observed-plan"
            with self.subTest(location=location):
                result = capacity.curate(raw)
                self.assertEqual(result["account"]["plan"], "observed-plan")
                self.assertIsNone(result["models"][0]["effort_ids"])

    def test_conflicting_duplicate_snapshots_refuse_instead_of_merging(self):
        for key, value in (("primary", None), ("secondary", {}), ("planType", "other")):
            raw = recorded()
            raw["rate_limits"]["rateLimitsByLimitId"]["codex"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(capacity.CapacityError, "conflicting.*inspect native metadata"):
                capacity.curate(raw)

    def test_missing_or_unsupported_observation_formats_refuse_capacity(self):
        for version in (None, True, 1.0, "1", 0, 2):
            raw = recorded()
            if version is None:
                del raw["schema_version"]
            else:
                raw["schema_version"] = version
            with self.subTest(version=version), self.assertRaisesRegex(capacity.CapacityError, "schema_version"):
                capacity.curate(raw)

    def test_matching_pool_facts_deduplicate_despite_omitted_or_private_fields(self):
        raw = recorded()
        alternate = raw["rate_limits"]["rateLimitsByLimitId"]["codex"]
        del alternate["limitId"]  # The native map key still supplies this identity.
        del alternate["secondary"]  # Missing and null both preserve an unknown window.
        alternate["credits"] = {"balance": "PRIVATE_BALANCE"}
        result = capacity.curate(raw)
        self.assertEqual(len(result["pools"]), 1)
        self.assertEqual(result["pools"][0]["limit_id"], "codex")
        self.assertEqual(result["pools"][0]["primary"]["remaining_percent"], 89)
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_malformed_known_fields_refuse_without_echoing_private_values(self):
        cases = [(["observed_at"], True), (["observed_at"], float("nan")),
                 (["observed_at"], 10 ** 400), (["binary"], "relative"),
                 (["account"], []), (["account", "account", "type"], 3),
                 (["rate_limits", "ordinaryUsageAllowed"], "PRIVATE_VALUE"),
                 (["rate_limits", "rateLimitsByLimitId"], []),
                 (["rate_limits", "rateLimits", "limitId"], 3),
                 (["rate_limits", "rateLimits", "primary"], []),
                 (["models"], {}), (["models", 0, "model"], ""),
                 (["models", 0, "supportedReasoningEfforts"], [None]),
                 (["models", 0, "defaultReasoningEffort"], "unsupported")]
        for key in ("usedPercent", "windowDurationMins", "resetsAt"):
            for value in (True, -1, "PRIVATE_VALUE", float("inf")):
                cases.append((["rate_limits", "rateLimits", "primary", key], value))
        cases.extend([(["rate_limits", "rateLimits", "primary", "usedPercent"], 101),
                      (["rate_limits", "rateLimits", "primary", "resetsAt"], 1.5)])
        for path, value in cases:
            raw = recorded()
            target = raw
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
            with self.subTest(path=path, value=value), self.assertRaises(capacity.CapacityError) as caught:
                capacity.curate(raw)
            self.assertNotIn("PRIVATE_VALUE", str(caught.exception))

    def test_unidentified_default_cannot_supply_an_extra_or_conflicting_pool(self):
        for used in (11, 99):
            for identity in (None, "missing"):
                raw = recorded()
                default = raw["rate_limits"]["rateLimits"]
                if identity == "missing":
                    del default["limitId"]
                else:
                    default["limitId"] = None
                default["primary"]["usedPercent"] = used
                with self.subTest(used=used, identity=identity), self.assertRaisesRegex(
                        capacity.CapacityError, "unidentified rateLimits.*inspect native metadata"):
                    capacity.curate(raw)

    def test_pool_blocking_status_survives_positive_ordinary_and_window_capacity(self):
        for reached in (None, "workspace_member_usage_limit_reached"):
            for spend in (None, False, True):
                raw = recorded()
                for pool in (raw["rate_limits"]["rateLimits"], raw["rate_limits"]["rateLimitsByLimitId"]["codex"]):
                    pool.update(rateLimitReachedType=reached, spendControlReached=spend)
                result = capacity.curate(raw)
                self.assertIs(result["ordinaryUsageAllowed"], True)
                self.assertEqual(result["pools"][0]["primary"]["remaining_percent"], 89)
                self.assertEqual(result["pools"][0]["rate_limit_reached_type"], reached)
                self.assertIs(result["pools"][0]["spend_control_reached"], spend)

    def test_pool_blocking_conflicts_and_unsupported_spending_constraints_refuse(self):
        for key, value in (("rateLimitReachedType", "workspace_member_usage_limit_reached"),
                           ("spendControlReached", True)):
            raw = recorded()
            raw["rate_limits"]["rateLimitsByLimitId"]["codex"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(capacity.CapacityError, "duplicate"):
                capacity.curate(raw)
        for key, value in (("rateLimitReachedType", 3), ("spendControlReached", 1),
                           ("spendControlReached", "PRIVATE_VALUE"),
                           ("individualLimit", {"remainingPercent": 0, "limit": "PRIVATE_VALUE", "used": "PRIVATE_VALUE"})):
            raw = recorded()
            raw["rate_limits"]["rateLimits"][key] = value
            with self.subTest(key=key), self.assertRaises(capacity.CapacityError) as caught:
                capacity.curate(raw)
            self.assertNotIn("PRIVATE_VALUE", str(caught.exception))

    def test_identity_plan_and_model_ambiguity_refuse(self):
        for change in (lambda r: r["rate_limits"]["rateLimitsByLimitId"]["codex"].update(limitId="other"),
                       lambda r: r["account"]["account"].update(planType="other"),
                       lambda r: r["models"].append(copy.deepcopy(r["models"][0])),
                       lambda r: r["models"][0]["supportedReasoningEfforts"].append({"reasoningEffort": "high"})):
            raw = recorded()
            change(raw)
            with self.assertRaises(capacity.CapacityError):
                capacity.curate(raw)

    def test_credits_and_extra_private_metadata_are_never_capacity(self):
        raw = recorded()
        for pool in (raw["rate_limits"]["rateLimits"], raw["rate_limits"]["rateLimitsByLimitId"]["codex"]):
            pool.update(credits={"balance": "100", "unlimited": True}, resetCredits=50)
        result = capacity.curate(raw)
        serialized = json.dumps(result)
        for forbidden in ("credits", "resetCredits", "unlimited", "dollars", "initialization", "email", "token"):
            self.assertNotIn(forbidden, serialized)


class Integration(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_calls_probe_exactly_once_and_propagates_failure(self):
        with patch.object(native_codex, "probe", new_callable=AsyncMock, return_value=recorded()) as probe:
            self.assertEqual(await capacity.snapshot(Path("/fixture/codex")), capacity.curate(recorded()))
            probe.assert_awaited_once_with(Path("/fixture/codex"))
        with patch.object(native_codex, "probe", new_callable=AsyncMock,
                          side_effect=native_codex.CodexProbeError("fixture failure")) as probe:
            with self.assertRaisesRegex(native_codex.CodexProbeError, "fixture failure"):
                await capacity.snapshot("/fixture/codex")
            self.assertEqual(probe.await_count, 1)
        with patch.object(native_codex, "probe", new_callable=AsyncMock) as probe:
            with self.assertRaisesRegex(capacity.CapacityError, "absolute"):
                await capacity.snapshot("relative")
            probe.assert_not_called()

    async def test_native_transport_to_curated_snapshot_offline(self):
        # Use the native probe's RPC/process fixtures, never a native executable.
        from test_native_codex import Process
        raw = recorded()
        process = Process({"initialize": {"result": raw["initialization"]},
            "account/read": {"result": raw["account"]},
            "account/rateLimits/read": {"result": raw["rate_limits"]},
            "model/list": {"result": {"data": raw["models"]}}})
        spawn = AsyncMock(return_value=process)
        original = native_codex.probe

        async def fake_transport(binary):
            return await original(binary, spawn=spawn, clock=lambda: raw["observed_at"])

        with patch.object(native_codex, "probe", side_effect=fake_transport) as probe:
            result = await capacity.snapshot(sys.executable)
        self.assertEqual(len(result["pools"]), 1)
        self.assertEqual(result["pools"][0]["primary"]["remaining_percent"], 89)
        self.assertTrue(process.terminated)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual([r["method"] for r in process.requests],
            ["initialize", "initialized", "account/read", "account/rateLimits/read", "model/list"])


class CLI(unittest.TestCase):
    def test_json_success_and_actionable_failure_have_no_partial_stdout(self):
        with patch.object(native_codex, "probe", new_callable=AsyncMock, return_value=recorded()) as probe:
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                self.assertEqual(capacity.main(["--codex", "/fixture/codex"]), 0)
            self.assertEqual(json.loads(out.getvalue()), capacity.curate(recorded()))
            self.assertEqual(probe.await_count, 1)
        for failure in (native_codex.CodexProbeError("inspect native sign-in"), capacity.CapacityError("inspect metadata")):
            with patch.object(capacity, "snapshot", new_callable=AsyncMock, side_effect=failure) as snapshot:
                with patch("sys.stdout", new_callable=io.StringIO) as out, patch("sys.stderr", new_callable=io.StringIO) as err:
                    self.assertEqual(capacity.main(["--codex", "/fixture/codex"]), 1)
                self.assertEqual(out.getvalue(), "")
                self.assertIn("inspect", err.getvalue())
                self.assertEqual(snapshot.await_count, 1)


if __name__ == "__main__":
    unittest.main()
