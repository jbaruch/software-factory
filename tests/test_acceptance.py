"""Replay externally stated expected behavior against the small reference decisions."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import json
import unittest
from pathlib import Path

from contracts import (efficiency, eligible_routes, integration_ready,
                       next_owner, select_route)

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = json.loads((ROOT / "evals/scenarios.json").read_text())


class AcceptanceScenarios(unittest.TestCase):
    def test_recorded_decisions(self):
        functions = {"next_owner": next_owner, "efficiency": efficiency,
                     "integration_ready": integration_ready}
        for scenario in SCENARIOS["decisions"]:
            with self.subTest(scenario=scenario["id"]):
                actual = functions[scenario["operation"]](**scenario["input"])
                self.assertEqual(actual, scenario["expected"])

    def test_routing_reacts_to_observed_capacity_and_learned_ranking(self):
        for scenario in SCENARIOS["route_selection"]:
            with self.subTest(scenario=scenario["id"]):
                eligible = eligible_routes(scenario["routes"], capability="code", now=100)
                self.assertEqual(eligible, scenario["expected_eligible"])
                self.assertEqual(select_route(eligible, scenario["ranking"]),
                                 scenario["expected_selection"])

    def test_invalid_durations_are_rejected(self):
        with self.assertRaises(ValueError):
            efficiency(coordination=-1)

    def test_unsupported_judgment_is_not_inferred(self):
        with self.assertRaises(ValueError):
            next_owner(stage="post-investigation", answer="probably fine", investigated=True)


if __name__ == "__main__":
    unittest.main()
