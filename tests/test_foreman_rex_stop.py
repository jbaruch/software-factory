import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "foreman-rex-stop.py"
SESSION = "session:00000000-0000-0000-0000-000000000001"
FOREMAN = "block:00000000-0000-0000-0000-000000000001"
WORKER = "block:00000000-0000-0000-0000-000000000002"
HELPER = "block:00000000-0000-0000-0000-000000000003"
SERVER = "unix:///fixture/Rex%20Server/rex.sock"
PREFIX = ["--autostart=false", "-S", SERVER]
INSPECT = [*PREFIX, "session", "inspect", SESSION, "--json"]
EVENT = '{"stop_hook_active":false}'
WARNING = "foreman-rex-stop: check the Stop input and Rex connection\n"
REMINDER = {
    "decision": "block",
    "reason": "A finished pane remains in this Rex session. Check helpers you own using "
              "the Rex helpers procedure in the factory command reference.",
}


def process(block):
    return [*PREFIX, "block", "call", "-s", SESSION, "-b", block, "process"]


def inventory(*blocks):
    return {"session_id": SESSION, "windows": [{"layers": [{"blocks": [
        {"block_id": block, "label": "private pane label"} for block in blocks
    ]}]}]}


class ForemanRexStopTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        fake = self.root / "rex"
        fake.write_text(f"#!{sys.executable}\n" + """\
import json, os, sys, time
from pathlib import Path

root = Path(os.environ["REX_BIN_DIR"])
with (root / "calls").open("a") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\\n")
response = json.loads((root / "replies").read_text())[json.dumps(sys.argv[1:])]
if response == "unavailable":
    sys.exit("private CLI diagnostic")
if response == "stall":
    sys.stdout.close()
    os.close(1)
    time.sleep(10)
if response == "oversized":
    print("private" * (64 * 1024))
elif response == "malformed":
    print("private malformed reply")
else:
    print(json.dumps(response))
""")
        fake.chmod(0o700)
        self.env = dict(os.environ, REX_SESSION=SESSION, REX_BLOCK=FOREMAN,
                        REX_SERVER=SERVER, REX_BIN_DIR=str(self.root),
                        SOFTWARE_FACTORY_FOREMAN_BLOCK=FOREMAN)

    def run_hook(self, *replies, event=EVENT, reminder=False, warning=False):
        calls = self.root / "calls"
        calls.unlink(missing_ok=True)
        (self.root / "replies").write_text(json.dumps({json.dumps(args): reply for args, reply in replies}))
        result = subprocess.run([sys.executable, str(HOOK)], input=event, env=self.env,
                                capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual(result.stderr, WARNING if warning else "")
        self.assertEqual(result.stdout, json.dumps(REMINDER) + "\n" if reminder else "")
        actual = calls.read_text().splitlines() if calls.exists() else []
        self.assertEqual([json.loads(line) for line in actual], [args for args, _ in replies])

    def test_only_exact_opt_in_for_local_rex_pane_can_probe(self):
        for key, value in (("SOFTWARE_FACTORY_FOREMAN_BLOCK", None), ("REX_BLOCK", WORKER),
                           ("REX_BLOCK", "block:short"), ("REX_SESSION", "line-label"),
                           ("REX_BIN_DIR", "."), ("REX_SERVER", None),
                           ("REX_SERVER", "tcp://other.example:8000"),
                           ("REX_SERVER", "unix://other.example/rex.sock")):
            with self.subTest(key=key, value=value):
                original = self.env.pop(key)
                if value is not None:
                    self.env[key] = value
                try:
                    self.run_hook(event="invalid Stop input")
                finally:
                    self.env[key] = original

    def test_later_retained_exit_reminds_opted_in_worker_then_followup_is_quiet(self):
        self.env.update(REX_BLOCK=WORKER, SOFTWARE_FACTORY_FOREMAN_BLOCK=WORKER,
                        SOFTWARE_FACTORY_MANAGED_CHILD="1")
        replies = ((INSPECT, inventory(FOREMAN, WORKER, HELPER)),
                   (process(FOREMAN), {"foreground": {"pid": 1}, "last_exit": {"exit_code": 0}}),
                   (process(HELPER), {"last_exit": {"exit_code": 7, "command": "private argv"}}))
        self.run_hook(*replies, reminder=True)
        self.run_hook(event='{"stop_hook_active":true}')
        self.run_hook(*replies, reminder=True)

    def test_scope_and_active_siblings_allow_stop(self):
        self.run_hook((INSPECT, {**inventory(FOREMAN, WORKER), "session_id": "other"}))
        self.run_hook((INSPECT, inventory(WORKER)))
        self.run_hook((INSPECT, {"session_id": SESSION}))
        self.run_hook((INSPECT, inventory(FOREMAN, WORKER, HELPER)),
                      (process(WORKER), {"child": {"pid": 1}, "last_exit": {"exit_code": 0}}),
                      (process(HELPER), {"last_exit": None}))

    def test_failures_warn_once_without_private_data(self):
        for event in ("private malformed input", '{"stop_hook_active":0}', EVENT.ljust(1024 * 1024 + 1)):
            with self.subTest(input_length=len(event)):
                self.run_hook(event=event, warning=True)
        for reply in ("unavailable", "malformed", "oversized", "stall", {"last_exit": {"exit_code": False}}):
            with self.subTest(reply=reply):
                self.run_hook((INSPECT, inventory(FOREMAN, HELPER)), (process(HELPER), reply), warning=True)
        self.run_hook((INSPECT, inventory(FOREMAN, "block:short")), warning=True)
        (self.root / "rex").unlink()
        self.run_hook(warning=True)


if __name__ == "__main__":
    unittest.main()
