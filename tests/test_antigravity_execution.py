"""Fixed sanitized 1.5.3 shapes, independently specified; no archive/provider I/O."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import copy
import json
import unittest
from unittest.mock import patch

import native_antigravity as native


# Deliberately independent of worker schema builders and decoder output.
SCHEMA = {"type": "object", "additionalProperties": False,
          "properties": {"outcome": {"type": "string", "enum": ["approved", "findings", "blocked"]},
                         "artifacts": {"type": "array", "items": {"type": "string"}}},
          "required": ["outcome", "artifacts"]}
LINES = str('''{"event":"init","conversation_id":"fixture-conversation","init":{"model":"fixture-gemini-low","cwd":"/workspace/fixture","tools":["read_file","write_file"],"permission_mode":"request-review","json_schema":{"type":"object","additionalProperties":false,"properties":{"outcome":{"type":"string","enum":["approved","findings","blocked"]},"artifacts":{"type":"array","items":{"type":"string"}}},"required":["outcome","artifacts"]},"expanded_commands":[{"name":"plan","type":"system"}]}}
{"event":"step_update","step_update":{"conversation_id":"fixture-conversation","step_index":0,"state":"DONE","step_type":"user_input"}}
{"event":"step_update","step_update":{"conversation_id":"fixture-conversation","step_index":1,"state":"DONE","step_type":"agent_response","duration_seconds":2.129632,"usage":{"input_tokens":8990,"output_tokens":67,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":9057}}}
{"event":"step_update","step_update":{"conversation_id":"fixture-conversation","step_index":2,"state":"ACTIVE","step_type":"tool","tool_name":"read_file","tool_info":{"name":"read_file","parameters":{"Path":"/workspace/fixture/README.md"}}}}
{"event":"step_update","step_update":{"conversation_id":"fixture-conversation","step_index":2,"state":"DONE","step_type":"tool","tool_name":"read_file","duration_seconds":0.084236,"tool_info":{"name":"read_file","parameters":{"Path":"/workspace/fixture/README.md"},"output":"1 lines, 60 bytes"}}}
{"event":"step_update","step_update":{"conversation_id":"fixture-conversation","step_index":3,"state":"DONE","step_type":"agent_response","text_delta":"{\\"outcome\\":\\"blocked\\",\\"artifacts\\":[]}\\n","duration_seconds":2.43659,"usage":{"input_tokens":9150,"output_tokens":78,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":9228}}}
{"event":"step_update","step_update":{"conversation_id":"fixture-conversation","step_index":4,"state":"ACTIVE","step_type":"tool","tool_name":"finish","tool_info":{"name":"finish","parameters":{"artifacts":["Fixture contents.","/workspace/fixture"],"outcome":"approved"}}}}
{"event":"step_update","step_update":{"conversation_id":"fixture-conversation","step_index":4,"state":"DONE","step_type":"finish","duration_seconds":0.04303}}
{"event":"result","result":{"conversation_id":"fixture-conversation","status":"SUCCESS","response":"{\\"artifacts\\":[\\"Fixture contents.\\",\\"/workspace/fixture\\"],\\"outcome\\":\\"approved\\"}\\n","duration_seconds":4.834046,"num_turns":1,"structured_output":{"artifacts":["Fixture contents.","/workspace/fixture"],"outcome":"approved"},"json_schema":{"type":"object","additionalProperties":false,"properties":{"outcome":{"type":"string","enum":["approved","findings","blocked"]},"artifacts":{"type":"array","items":{"type":"string"}}},"required":["outcome","artifacts"]},"usage":{"input_tokens":18140,"output_tokens":145,"thinking_tokens":0,"cache_read_tokens":0,"total_tokens":18285}}}
''').splitlines(keepends=True)


class AntigravityExecution(unittest.TestCase):
    def parse(self, lines=None, **options):
        return native.parse_execution(LINES if lines is None else lines,
            **{**dict(expected_model="fixture-gemini-low", expected_cwd="/workspace/fixture",
                      expected_schema=SCHEMA), **options})

    def altered(self, index, key, value):
        lines = LINES.copy()
        event = json.loads(lines[index])
        event[event["event"]][key] = value
        lines[index] = json.dumps(event)
        return lines

    def rejects(self, lines, diagnostic):
        original = copy.deepcopy(lines)
        with self.assertRaisesRegex(native.ExecutionError, diagnostic) as caught:
            self.parse(lines)
        self.assertIn("inspect the original stream", str(caught.exception))
        self.assertEqual(lines, original)  # Never repair or rewrite retained inputs.

    def test_fixed_plan_shape_and_terminal_observations_without_execution(self):
        original = LINES.copy()
        with patch.object(native.subprocess, "run", side_effect=AssertionError("no process")):
            result = self.parse()
        self.assertEqual(result, dict(report={"outcome": "approved", "artifacts": ["Fixture contents.", "/workspace/fixture"]},
            conversation_id="fixture-conversation", init_model="fixture-gemini-low", cwd="/workspace/fixture",
            turns=1, duration_seconds=4.834046,
            usage={"input_tokens": 18140, "output_tokens": 145, "thinking_tokens": 0,
                   "cache_read_tokens": 0, "total_tokens": 18285}))
        self.assertEqual(LINES, original)
        self.assertEqual(self.parse([line.encode() for line in LINES]), result)

    def test_first_stream_finish_shape_and_tools_are_only_telemetry(self):
        # Initial stream used DONE finish directly, without ACTIVE finish tool.
        lines = LINES[:6] + LINES[7:]
        result = self.parse(lines)
        self.assertEqual(result["report"]["outcome"], "approved")
        self.assertNotIn("permission_mode", result)
        self.assertNotIn("actual_model", result)
        for field in ("quota", "dollars", "accepted", "exit_code"):
            self.assertNotIn(field, result)

    def test_missing_observations_remain_unknown_no_step_sum_or_response_fallback(self):
        event = json.loads(LINES[-1])
        for key in ("usage", "duration_seconds", "response"):
            del event["result"][key]
        result = self.parse(LINES[:-1] + [json.dumps(event)])
        self.assertIsNone(result["usage"])
        self.assertIsNone(result["duration_seconds"])
        self.assertIsNone(self.parse(self.altered(-1, "usage", None))["usage"])
        self.assertEqual(self.parse(self.altered(-1, "usage", {"input_tokens": None}))["usage"], {"input_tokens": None})
        self.assertEqual(self.parse(self.altered(-1, "duration_seconds", 0))["duration_seconds"], 0)
        self.rejects(self.altered(-1, "structured_output", None), "structured report")

    def test_frozen_inputs_and_supported_schema(self):
        for options in (dict(expected_model="other"), dict(expected_cwd="/workspace/other"),
                        dict(expected_model=" "), dict(expected_cwd="relative"), dict(expected_cwd="/bad\0path"),
                        dict(expected_schema=None), dict(expected_schema={}),
                        dict(expected_schema={**SCHEMA, "additionalProperties": 0}),
                        dict(expected_schema={**SCHEMA, "required": []})):
            with self.subTest(options=options), self.assertRaises(native.ExecutionError):
                self.parse(**options)
        for key in ("model", "cwd", "json_schema"):
            self.rejects(self.altered(0, key, None), "frozen inputs")
        for index in (0, -1):
            self.rejects(self.altered(index, "json_schema", {**SCHEMA, "additionalProperties": 0}), "schema")
        schema = copy.deepcopy(SCHEMA)
        schema["properties"]["outcome"]["enum"] = ["completed", "approved", "findings", "blocked"]
        lines = self.altered(0, "json_schema", schema)
        terminal = json.loads(lines[-1])
        terminal["result"]["json_schema"] = schema
        lines[-1] = json.dumps(terminal)
        for outcome in ("approved", "findings", "blocked", "completed"):
            terminal["result"]["structured_output"] = {"outcome": outcome, "artifacts": []}
            terminal["result"]["response"] = json.dumps({"outcome": outcome, "artifacts": []})
            lines[-1] = json.dumps(terminal)
            with self.subTest(outcome=outcome):
                self.assertEqual(self.parse(lines, expected_schema=schema)["report"],
                                 {"outcome": outcome, "artifacts": []})

    def test_missing_duplicate_out_of_order_and_trailing_lifecycle(self):
        for lines, diagnostic in (([], "first init"), (LINES[1:], "first init"), (LINES[:-1], "last result"),
                (LINES[:1] + LINES, "duplicate.*init"), (LINES + [LINES[-1]], "trailing"),
                (LINES + ["\n"], "trailing"),
                (["warning: --mode plan has no effect while slash command expansion is disabled.\n"] + LINES, "malformed JSON"),
                (LINES[:3] + [LINES[2]] + LINES[3:], "duplicate"),
                (LINES[:3] + [LINES[4], LINES[3]] + LINES[5:], "out-of-order"),
                (LINES[:4] + LINES[5:], "out-of-order"), (LINES[:3] + [LINES[-1]], "structured report|SUCCESS"),
                (LINES[:1] + LINES[2:], "out-of-order|user turn"),
                (LINES[:-1] + [LINES[2], LINES[-1]], "out-of-order")):
            with self.subTest(lines=lines):
                self.rejects(lines, diagnostic)

    def test_every_identity_bearing_event_must_agree(self):
        for index in range(len(LINES)):
            for envelope in (True, False):
                event = json.loads(LINES[index])
                source = event if envelope else event[event["event"]]
                source["conversation_id"] = "other-conversation"
                with self.subTest(index=index):
                    self.rejects(LINES[:index] + [json.dumps(event)] + LINES[index + 1:], "identity")
        for index in (0, 1, -1):
            event = json.loads(LINES[index])
            source = event if index == 0 else event[event["event"]]
            del source["conversation_id"]
            lines = LINES.copy()
            lines[index] = json.dumps(event)
            self.rejects(lines, "identity")

    def test_malformed_failed_or_extra_turns_cannot_complete(self):
        for index, key, value, message in ((-1, "status", "FAILED", "SUCCESS"),
                (-1, "num_turns", True, "user turn"), (-1, "num_turns", 2, "user turn"),
                (-1, "num_turns", None, "user turn"), (-1, "json_schema", None, "schema"),
                (1, "step_index", True, "malformed"), (1, "state", "ACTIVE", "DONE user_input"),
                (2, "state", "FAILED", "malformed"), (2, "step_type", "unknown", "malformed"),
                (2, "step_type", "user_input", "one first")):
            self.rejects(self.altered(index, key, value), message)
        for line in ('{"event":"error"}', '{"event":"init","init":[]}', '[]', '{}', 'null', b'\xff'):
            self.rejects([line], "unsupported|malformed")
        self.rejects([json.loads(LINES[0])], "original JSON lines")

    def test_report_and_response_are_strict_and_must_agree(self):
        for report in ({}, [], {"outcome": "accepted", "artifacts": []},
                       {"outcome": "completed", "artifacts": []},  # Not in this frozen subset.
                       {"outcome": "approved", "artifacts": [" "]},
                       {"outcome": "approved", "artifacts": [1]},
                       {"outcome": "approved", "artifacts": "path"},
                       {"outcome": "approved", "artifacts": [], "accepted": True}):
            self.rejects(self.altered(-1, "structured_output", report), "structured report")
        for response in (None, "not JSON", '{"outcome":"blocked","artifacts":[]}',
                         '{"outcome":"approved","outcome":"approved","artifacts":[]}',
                         '{"outcome":"approved","artifacts":[],"extra":NaN}'):
            self.rejects(self.altered(-1, "response", response), "response")

    def test_numeric_observations_and_all_json_are_strict(self):
        for index in (2, -1):
            for value in (True, -1, 1.5, "12", float("nan"), float("inf"), 10**309):
                self.rejects(self.altered(index, "usage", {"input_tokens": value}), "usage|malformed JSON")
            for value in (True, -0.1, "1", float("nan"), float("inf"), 10**309):
                self.rejects(self.altered(index, "duration_seconds", value), "duration|malformed JSON")
            for value in ([], 1):
                self.rejects(self.altered(index, "usage", value), "usage")
        for line in (LINES[0].replace('"event":"init"', '"event":"init","event":"init"'),
                     LINES[0].replace('"model":', '"unused":1e999,"model":')):
            self.rejects([line] + LINES[1:], "malformed JSON")
        self.rejects(LINES[:-1] + [LINES[-1].replace('"status":"SUCCESS"',
                     '"status":"FAILED","status":"SUCCESS"')], "malformed JSON")


if __name__ == "__main__":
    unittest.main()
