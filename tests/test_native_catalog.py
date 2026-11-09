"""Recorded catalog text and fake subprocesses; no live providers or agents."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import subprocess
import sys
import unittest
from unittest.mock import Mock

import native_catalog as catalog

GROK = """You are logged in with grok.com.

Default model: fixture-grok

Available models:
  * fixture-grok (default)
  - fixture-fast
"""
ANTIGRAVITY = "fixture-gemini\tGemini fixture\nfixture-claude\tClaude fixture\n"


class NativeCatalog(unittest.TestCase):
    def test_grok_catalog_keeps_native_model_ids_and_order(self):
        self.assertEqual(catalog.parse_catalog("grok", GROK),
                         [dict(id="fixture-grok", name="fixture-grok"),
                          dict(id="fixture-fast", name="fixture-fast")])

    def test_antigravity_catalog_keeps_native_names_without_inventing_effort(self):
        self.assertEqual(catalog.parse_catalog("antigravity", ANTIGRAVITY),
                         [dict(id="fixture-gemini", name="Gemini fixture"),
                          dict(id="fixture-claude", name="Claude fixture")])
        self.assertEqual(catalog.parse_catalog("grok", "Available models:\n"), [])
        self.assertEqual(catalog.parse_catalog("antigravity", ""), [])

    def test_changed_or_ambiguous_catalogs_refuse_candidate_discovery(self):
        for harness, output in (("grok", "Authentication required"),
                                ("grok", "Available models:\nunsupported format"),
                                ("grok", GROK + "- fixture-grok\n"),
                                ("antigravity", "Authentication required"),
                                ("antigravity", ANTIGRAVITY + "fixture-gemini\tDuplicate\n")):
            with self.subTest(harness=harness, output=output), self.assertRaises(catalog.CatalogError):
                catalog.parse_catalog(harness, output)

    def test_probe_only_runs_version_and_models_and_preserves_unknown_capacity(self):
        outputs = iter([subprocess.CompletedProcess([], 0, "fixture CLI 1.0\n", ""),
                        subprocess.CompletedProcess([], 0, ANTIGRAVITY, "Fetching available models...\n")])
        run = Mock(side_effect=lambda *args, **kwargs: next(outputs))
        times = iter([10, 12])
        observed = catalog.probe("antigravity", sys.executable, run=run,
                                 clock=lambda: 1000, timer=lambda: next(times))
        self.assertEqual([call.args[0][1:] for call in run.call_args_list], [["--version"], ["models"]])
        self.assertTrue(all(call.kwargs["stdin"] == subprocess.DEVNULL for call in run.call_args_list))
        self.assertEqual(observed["schema_version"], 1)
        self.assertEqual(observed["observed_at"], 1000)
        self.assertEqual(observed["probe_seconds"], 2)
        self.assertEqual(observed["readiness"], "unverified")
        self.assertIsNone(observed["quota_observation"])
        self.assertEqual(observed["native_output"]["models"], ANTIGRAVITY)

    def test_process_failures_are_actionable_without_echoing_native_diagnostics(self):
        for error in (FileNotFoundError("private fixture"), subprocess.TimeoutExpired("private fixture", 20),
                      UnicodeDecodeError("utf-8", b'\xff', 0, 1, "private fixture")):
            run = Mock(side_effect=error)
            with self.subTest(error=type(error)), self.assertRaisesRegex(catalog.CatalogError, "inspect") as caught:
                catalog.probe("grok", sys.executable, run=run)
            self.assertNotIn("private fixture", str(caught.exception))
            self.assertEqual(run.call_count, 1)
        run = Mock(return_value=subprocess.CompletedProcess([], 1, "", "private fixture"))
        with self.assertRaisesRegex(catalog.CatalogError, "inspect native sign-in/configuration") as caught:
            catalog.probe("grok", sys.executable, run=run)
        self.assertNotIn("private fixture", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
