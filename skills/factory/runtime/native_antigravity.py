"""Observe native Antigravity weekly quota groups without a model turn."""
import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True  # Installed copies stay free of generated files.
import time
from typing import NoReturn

from codex_worker import native_environment
from factory import decode_json, RESULT_OUTCOMES

API_ENVIRONMENT = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "AGY_ADC_AUTH")
EFFORTS = ("low", "medium", "high", "xhigh", "max")  # As `agy --effort` documents them.


class UsageError(Exception):
    pass


class ExecutionError(Exception):
    pass


CLOSED_STEPS = ("DONE", "ERROR")  # States that end a step; ERROR only ends a tool step.


def parse_execution(events, *, expected_model, expected_cwd, expected_schema):
    """Decode original 1.5.3 JSON lines only; callers retain provenance and exit.

    No I/O, execution admission, process success or acceptance is established.
    The init model is an echo, not independently observed inference identity.
    """
    def refuse(reason) -> NoReturn:
        raise ExecutionError(f"Antigravity {reason}; inspect the original stream and frozen inputs before collection")

    if (not isinstance(expected_model, str) or not expected_model.strip()
            or not isinstance(expected_cwd, str) or "\0" in expected_cwd
            or not Path(expected_cwd).is_absolute()):
        refuse("requires an explicit model and absolute cwd")
    # This dedicated report shape supports the historical outcome subset too.
    outcomes = (expected_schema.get("properties", {}).get("outcome", {}).get("enum")
                if isinstance(expected_schema, dict)
                and isinstance(expected_schema.get("properties"), dict)
                and isinstance(expected_schema["properties"].get("outcome"), dict) else None)
    if (not isinstance(outcomes, list) or not outcomes
            or any(not isinstance(value, str) or value not in RESULT_OUTCOMES for value in outcomes)
            or len(set(outcomes)) != len(outcomes)):
        refuse("requires a supported frozen report schema")
    schema = dict(type="object", additionalProperties=False,
                  properties=dict(outcome=dict(type="string", enum=outcomes),
                                  artifacts=dict(type="array", items=dict(type="string"))),
                  required=["outcome", "artifacts"])
    try:
        if json.dumps(expected_schema, sort_keys=True, allow_nan=False) != json.dumps(schema, sort_keys=True):
            refuse("requires the supported frozen report schema")
    except (TypeError, ValueError):
        refuse("has a malformed frozen report schema")

    def observations(payload, where):
        usage, duration = payload.get("usage"), payload.get("duration_seconds")
        if usage is not None and (not isinstance(usage, dict)
                or any(value is not None and (type(value) is not int or not 0 <= value <= sys.float_info.max)
                       for value in usage.values())):
            refuse(f"has malformed {where} token usage")
        if duration is not None and (type(duration) not in (int, float)
                or not 0 <= duration <= sys.float_info.max or not math.isfinite(duration)):
            refuse(f"has malformed {where} duration_seconds")

    def tool_error(payload):
        info = payload.get("tool_info")
        error = info.get("error") if isinstance(info, dict) else None
        return (payload.get("step_type") == "tool" and isinstance(error, dict)
                and all(isinstance(error.get(key), str) and error[key].strip() for key in ("type", "message")))

    conversation_id, final = None, None
    step_index, step_state, step_type, user_turns = -1, None, None, 0
    for line_number, line in enumerate(events, 1):
        if final is not None:
            refuse(f"has trailing data after result at line {line_number}")
        if not isinstance(line, (str, bytes)):
            refuse(f"requires original JSON lines at line {line_number}")
        try:
            event = decode_json(line)
        except (ValueError, UnicodeError):
            refuse(f"has malformed JSON at line {line_number}")
        if not isinstance(event, dict) or event.get("event") not in ("init", "step_update", "result"):
            refuse(f"has an unsupported event at line {line_number}")
        kind = event["event"]
        payload = event.get(kind)
        if (not isinstance(payload, dict)
                or any(key in event for key in {"init", "step_update", "result"} - {kind})):
            refuse(f"has a malformed {kind} envelope at line {line_number}")
        if conversation_id is None:
            if kind != "init" or line_number != 1:
                refuse("requires exactly one first init")
            conversation_id = event.get("conversation_id")
            if not isinstance(conversation_id, str) or not conversation_id.strip():
                refuse("init has no conversation identity")
            if (payload.get("model") != expected_model or payload.get("cwd") != expected_cwd
                    or payload.get("json_schema") != schema):
                refuse("init model/cwd/schema differs from frozen inputs")
        elif kind == "init":
            refuse("has a duplicate or out-of-order init")
        for source in (event, payload):
            if "conversation_id" in source and source["conversation_id"] != conversation_id:
                refuse(f"has a mismatched conversation identity at line {line_number}")
        if kind == "init":
            # JSON comparison also distinguishes false from numeric zero.
            if json.dumps(payload["json_schema"], sort_keys=True) != json.dumps(schema, sort_keys=True):
                refuse("init schema differs from frozen inputs")
            continue
        if payload.get("conversation_id") != conversation_id:
            refuse(f"{kind} has no matching conversation identity at line {line_number}")
        observations(payload, kind)
        if kind == "step_update":
            index, state, category = payload.get("step_index"), payload.get("state"), payload.get("step_type")
            # A tool step can end in ERROR (the agent saw the failure and carried on), and agy
            # 1.5.3 records empty system_message and error_message steps. None carries a report
            # or authority; the result event alone supplies the report.
            if (type(index) is not int or index < 0 or state not in ("ACTIVE", "DONE", "ERROR")
                    or category not in ("user_input", "agent_response", "tool", "finish", "system_message", "error_message")
                    or state == "ERROR" and not tool_error(payload)):
                refuse(f"has malformed step_update at line {line_number}")
            if index == step_index:
                # agy streams a response as repeated ACTIVE updates of one step, each with a text_delta.
                streaming = state == "ACTIVE" and category == step_type == "agent_response"
                if (step_state != "ACTIVE" or state not in CLOSED_STEPS and not streaming
                        or category != step_type and (step_type, category) != ("tool", "finish")):
                    refuse(f"has duplicate or out-of-order step_update at line {line_number}")
            elif index != step_index + 1 or step_index >= 0 and step_state not in CLOSED_STEPS or step_type == "finish":
                refuse(f"has out-of-order step_update at line {line_number}")
            if category == "user_input":
                # The factory supplies one turn. agy adds one empty turn of its own when the model
                # answered in prose, asking again for the structured report (#86).
                reprompt = (user_turns == 1 and step_type == "agent_response" and step_state == "DONE"
                            and not any(key in payload for key in ("text_delta", "content", "message")))
                if state != "DONE" or not (index == 0 and not user_turns or reprompt):
                    refuse("requires exactly one first DONE user_input step")
                user_turns += 1
            elif not user_turns:
                refuse("has telemetry before the supplied user turn")
            step_index, step_state, step_type = index, state, category
        else:
            if (payload.get("status") != "SUCCESS" or type(payload.get("num_turns")) is not int
                    or not user_turns or payload["num_turns"] != user_turns
                    or step_state != "DONE" or step_type != "finish"):
                refuse("result requires SUCCESS, one completed supplied user turn and DONE finish")
            if (payload.get("json_schema") != schema
                    or json.dumps(payload["json_schema"], sort_keys=True) != json.dumps(schema, sort_keys=True)):
                refuse("terminal schema differs from frozen inputs")
            report = payload.get("structured_output")
            if (not isinstance(report, dict) or set(report) != {"outcome", "artifacts"}
                    or not isinstance(report["outcome"], str) or report["outcome"] not in outcomes
                    or not isinstance(report["artifacts"], list)
                    or any(not isinstance(value, str) or not value.strip() for value in report["artifacts"])):
                refuse("result has no supported structured report")
            if "response" in payload:
                if not isinstance(payload["response"], str):
                    refuse("result response is not JSON text")
                try:
                    response = trailing_object(payload["response"])
                except ValueError:
                    refuse("result response is malformed JSON")
                if response != report:
                    refuse("result response disagrees with structured_output")
            final = payload
    if final is None:
        refuse("requires exactly one first init and one last result")
    return dict(report=final["structured_output"], conversation_id=conversation_id,
                init_model=expected_model, cwd=expected_cwd, turns=final["num_turns"],
                usage=final.get("usage"), duration_seconds=final.get("duration_seconds"))


def trailing_object(text):
    """The JSON object a response ends with. agy puts any prose the model wrote before it,
    after a newline when it re-prompted for the report and glued on when it did not (#86)."""
    text, decoder = text.rstrip(), json.JSONDecoder()
    start = text.rfind("{")
    while start >= 0:
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            value, end = None, -1
        if end == len(text) and isinstance(value, dict):
            return value
        start = text.rfind("{", 0, start)
    raise ValueError("Response does not end with a JSON object")


def parse_usage(output):
    """Read the observed four-column `agy --print /usage` format."""
    groups, seen = [], set()
    for line in output.splitlines():
        fields = line.split("\t")
        if (len(fields) != 4 or not fields[0].strip() or fields[0] in seen
                or fields[1] != "Weekly Limit Remaining"
                or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?%", fields[2])):
            raise UsageError("Unsupported Antigravity usage format; inspect the native /usage report and update its reader before scheduling")
        group, _, percentage, reset = fields
        remaining = float(percentage[:-1])
        try:
            parsed = datetime.fromisoformat(reset.replace("Z", "+00:00"))
        except ValueError:
            raise UsageError("Invalid Antigravity quota reset; inspect the original usage report before scheduling") from None
        if not 0 <= remaining <= 100 or parsed.utcoffset() is None:
            raise UsageError("Invalid Antigravity quota value/reset; inspect the original usage report before scheduling")
        seen.add(group)
        groups.append(dict(group=group, window="weekly", percent_remaining=remaining, resets_at=reset))
    if not groups:
        raise UsageError("Antigravity returned no quota groups; inspect native sign-in and /usage before scheduling")
    return groups


def probe(binary, cwd, *, run=subprocess.run, clock=time.time, timer=time.monotonic):
    if any(name in os.environ for name in API_ENVIRONMENT):
        raise UsageError("Unset Gemini API/ADC environment overrides before observing the native subscription route")
    if not Path(binary).is_absolute():
        raise UsageError("Supply an absolute native Antigravity executable before observing quota")
    try:
        binary, cwd = Path(binary).resolve(strict=True), Path(cwd).resolve(strict=True)
    except OSError:
        raise UsageError("Restore the selected native Antigravity executable and working directory before refreshing quota") from None
    if not binary.is_file() or not os.access(binary, os.X_OK) or not cwd.is_dir():
        raise UsageError("Supply an executable native Antigravity file and existing working directory")
    started, outputs = timer(), {}
    for command in (("--version",), ("--print", "/usage")):
        try:
            result = run([str(binary), *command], cwd=str(cwd), env=native_environment(),
                         stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=20, check=False)
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            raise UsageError("Cannot observe native Antigravity quota; inspect its installation, sign-in and connection before refreshing") from None
        if result.returncode or result.stderr:
            raise UsageError("Native Antigravity quota observation failed; inspect native sign-in/configuration and /usage before refreshing")
        outputs[command[0]] = result.stdout
    version = outputs["--version"].strip()
    if not version:
        raise UsageError("Antigravity returned no CLI version; restore its installation before refreshing quota")
    return dict(schema_version=1, kind="antigravity-native-usage", binary=str(binary), cwd=str(cwd),
                cli_version=version, observed_at=clock(), probe_seconds=timer() - started,
                groups=parse_usage(outputs["--print"]), native_output=outputs["--print"],
                readiness="unverified",
                note="Quota groups only; account eligibility, model-to-group binding, reservations and worker readiness require separate evidence.")


def validate_usage(value, binary):
    """Validate a retained usage observation without rereading the native CLI."""
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("kind") != "antigravity-native-usage" or value.get("binary") != str(Path(binary).resolve())
            or not isinstance(value.get("cli_version"), str) or not value["cli_version"].strip()
            or not isinstance(value.get("groups"), list) or not value["groups"]
            or not isinstance(value.get("cwd"), str) or not Path(value["cwd"]).is_absolute()):
        raise UsageError("Antigravity usage observation differs from its native binary; refresh it before execution")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(probe(args.binary, args.cwd), indent=2, allow_nan=False))
    except UsageError as exc:
        print("native-antigravity: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
