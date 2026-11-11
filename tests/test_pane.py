"""Readable assignment panes: one line per event worth watching, from each harness's real event shapes."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import json
import unittest

import factory


class Pane(unittest.TestCase):
    def test_claude_tool_calls_text_errors_and_result(self):
        assistant = dict(type="assistant", message=dict(content=[
            dict(type="thinking", thinking="private"), dict(type="text", text="Checking the\nrule"),
            dict(type="tool_use", name="Read", input=dict(file_path="/repo/rules/a.md"))]))
        self.assertEqual(factory.pane_line("claude", assistant), "▸ Checking the rule\n→ Read /repo/rules/a.md")
        failed = dict(type="user", message=dict(content=[dict(type="tool_result", is_error=True, content="No such file")]))
        self.assertEqual(factory.pane_line("claude", failed), "✗ No such file")
        result = dict(type="result", subtype="success", num_turns=36, structured_output=dict(outcome="completed", artifacts=["a", "b"]))
        self.assertEqual(factory.pane_line("claude", result), "= success: completed (2 artifacts), 36 turns")
        for skipped in (dict(type="system", subtype="thinking_tokens"), dict(type="rate_limit_event"),
                        dict(type="user", message=dict(content=[dict(type="tool_result", content="ok")]))):
            self.assertIsNone(factory.pane_line("claude", skipped))

    def test_grok_shows_calls_and_failures_but_not_streamed_fragments(self):
        call = dict(type="tool_call", toolName="read_file", rawInput=dict(target_file="/repo/README.md", limit=40))
        self.assertEqual(factory.pane_line("grok", call), "→ read_file /repo/README.md")
        self.assertEqual(factory.pane_line("grok", dict(type="tool_call_update", status="failed",
            content="Denied by permission policy")), "✗ Denied by permission policy")
        for fragment in (dict(type="text", data=" by"), dict(type="thought", data="I will"), dict(type="usage")):
            self.assertIsNone(factory.pane_line("grok", fragment))
        end = dict(type="end", stopReason="end_turn", num_turns=20, structuredOutput=dict(outcome="approved", artifacts=["x"]))
        self.assertEqual(factory.pane_line("grok", end), "= end_turn: approved (1 artifacts), 20 turns")

    def test_antigravity_reads_raw_lines_and_shows_malformed_ones_as_they_came(self):
        step = dict(event="step_update", step_update=dict(step_type="tool", state="ACTIVE", tool_name="run_command",
            tool_info=dict(parameters=dict(CommandLine="git diff --stat"))))
        self.assertEqual(factory.pane_line("antigravity", json.dumps(step) + "\n"), "→ run_command git diff --stat")
        delta = dict(event="step_update", step_update=dict(step_type="agent_response", state="ACTIVE", text_delta="Now"))
        self.assertIsNone(factory.pane_line("antigravity", json.dumps(delta)))
        self.assertEqual(factory.pane_line("antigravity", "not json\n"), "not json")

    def test_codex_commands_failures_messages_and_turns(self):
        started = dict(type="item.started", item=dict(type="command_execution", command="pytest -q"))
        self.assertEqual(factory.pane_line("codex", started), "→ $ pytest -q")
        failed = dict(type="item.completed", item=dict(type="command_execution", command="pytest -q", exit_code=1))
        self.assertEqual(factory.pane_line("codex", failed), "✗ exit 1: pytest -q")
        self.assertIsNone(factory.pane_line("codex", dict(type="item.completed",
            item=dict(type="command_execution", command="ls", exit_code=0))))
        self.assertEqual(factory.pane_line("codex", dict(type="turn.completed")), "= completed")

    def test_long_text_is_cut_to_one_line(self):
        line = factory.pane_line("codex", dict(type="item.completed", item=dict(type="agent_message", text="x" * 500)))
        self.assertEqual(len(line), 2 + factory.PANE_WIDTH)
        self.assertNotIn("\n", line)


if __name__ == "__main__":
    unittest.main()
