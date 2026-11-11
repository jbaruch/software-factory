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

    def recovered(self, step_type="system_message", error=None):
        """Observed 1.5.3 (#51): a read_file step errors, the agent carries on, and an empty
        system_message step follows; later steps shift by one."""
        def shifted(line, by):
            event = json.loads(line)
            event["step_update"]["step_index"] += by
            return json.dumps(event) + "\n"
        errored = json.loads(LINES[4])
        errored["step_update"].update(state="ERROR", tool_info=dict(name="read_file", parameters=dict(Path="README.md"),
            error={**dict(type="TOOL_ERROR", message="README.md must be an absolute path"), **(error or {})}))
        # Observed (canary qa-1 on writer-4): agy also records a bare error_message step and carries on.
        notice = dict(event="step_update", step_update=dict(conversation_id="fixture-conversation", step_index=3,
                                                            state="DONE", step_type=step_type, duration_seconds=0.0002))
        return (LINES[:4] + [json.dumps(errored) + "\n", json.dumps(notice) + "\n"]
                + [shifted(line, 1) for line in LINES[5:8]] + LINES[8:])

    def test_recovered_tool_errors_and_system_messages_complete(self):
        for step_type in ("system_message", "error_message"):
            with self.subTest(step_type=step_type):
                self.assertEqual(self.parse(self.recovered(step_type))["report"]["outcome"], "approved")

    def test_errors_outside_tools_or_without_an_error_record_refuse(self):
        lines = self.recovered()
        for index, changes in ((4, dict(tool_info=dict(name="read_file"))), (4, dict(state="CANCELLED")),
                               (2, dict(state="ERROR")), (5, dict(state="ERROR")), (5, dict(step_type="system_notice"))):
            altered = lines.copy()
            event = json.loads(altered[index])
            event["step_update"].update(changes)
            altered[index] = json.dumps(event) + "\n"
            with self.subTest(index=index, changes=changes):
                self.rejects(altered, "malformed step_update|out-of-order")
        for changes in (dict(message=""), dict(type=None)):
            with self.subTest(error=changes):
                self.rejects(self.recovered(error=changes), "malformed step_update")
        closed_twice = lines[:5] + [lines[4].replace('"ERROR"', '"DONE"')] + lines[5:]
        self.rejects(closed_twice, "duplicate or out-of-order")

    def test_a_response_streamed_as_repeated_active_updates_decodes(self):
        # Observed (#86): agy streams one agent_response step as several ACTIVE text_delta updates.
        def update(state, text=None, **extra):
            step = dict(conversation_id="fixture-conversation", step_index=3, state=state, step_type="agent_response", **extra)
            if text is not None:
                step["text_delta"] = text
            return json.dumps(dict(event="step_update", step_update=step)) + "\n"
        streamed = LINES[:5] + [update("ACTIVE", "Now"), update("ACTIVE", " let me check"), update("ACTIVE", " for")] + LINES[5:]
        self.assertEqual(self.parse(streamed)["report"]["outcome"], "approved")
        # Only a response streams: a tool step still opens once, and nothing reopens after DONE.
        tool = json.loads(LINES[3])["step_update"]
        twice = LINES[:4] + [json.dumps(dict(event="step_update", step_update=tool)) + "\n"] + LINES[4:]
        self.rejects(twice, "duplicate or out-of-order")
        self.rejects(LINES[:6] + [update("ACTIVE", "late")] + LINES[6:], "out-of-order")

    def test_agys_reprompt_after_a_prose_answer_decodes_with_two_turns(self):
        # Observed (#86): the model answered in prose, agy added one empty user turn asking
        # for the structured report, and the result response is the prose then the report.
        def step(index, state, kind, **extra):
            return json.dumps(dict(event="step_update", step_update=dict(
                conversation_id="fixture-conversation", step_index=index, state=state, step_type=kind, **extra))) + "\n"
        result = json.loads(LINES[-1])
        report = result["result"]["structured_output"]
        result["result"].update(num_turns=2, response="Prose findings table\n" + json.dumps(report) + "\n")
        lines = (LINES[:3] + [step(2, "DONE", "user_input"), step(3, "DONE", "agent_response", text_delta=json.dumps(report)),
                 step(4, "DONE", "finish"), json.dumps(result) + "\n"])
        self.assertEqual(self.parse(lines)["report"], report)
        self.assertEqual(self.parse(lines)["turns"], 2)
        # A re-prompt that carries text, a third turn, or a turn count that disagrees refuses.
        with_text = lines[:3] + [step(2, "DONE", "user_input", text_delta="do more")] + lines[4:]
        self.rejects(with_text, "user_input")
        third = lines[:5] + [step(4, "DONE", "user_input"), step(5, "DONE", "finish")] + lines[6:]
        self.rejects(third, "user_input|out-of-order")
        miscounted = copy.deepcopy(result)
        miscounted["result"]["num_turns"] = 1
        self.rejects(lines[:-1] + [json.dumps(miscounted) + "\n"], "one completed supplied user turn")

    def test_prose_glued_to_the_report_in_a_one_turn_response_decodes(self):
        # Observed: a one-turn Sonnet review's response was 2204 characters of prose with the report glued on.
        result = json.loads(LINES[-1])
        report = result["result"]["structured_output"]
        for response in ("UNTESTED by this review." + json.dumps(report) + "\n", json.dumps(report),
                         "A {braced} aside.\n" + json.dumps(report)):
            result["result"]["response"] = response
            with self.subTest(response=response[:30]):
                self.assertEqual(self.parse(LINES[:-1] + [json.dumps(result) + "\n"])["report"], report)
        for response in (json.dumps(report) + " trailing prose", "prose only", json.dumps(dict(report, outcome="blocked"))):
            result["result"]["response"] = response
            with self.subTest(response=response[:30]):
                self.rejects(LINES[:-1] + [json.dumps(result) + "\n"], "malformed JSON|disagrees")

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
