"""Explicit live check: creates one Rex session and two harmless specialist tabs."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rex", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--state-dir", type=Path, help="Resume an existing smoke check")
    args = parser.parse_args()
    state = (args.state_dir or Path(tempfile.mkdtemp(prefix="factory-rex-smoke-"))).resolve()
    base = [sys.executable, str(ROOT / "factory.py"), "--state-dir", str(state)]
    task = "native-smoke"

    def call(*arguments):
        return subprocess.check_output([*base, *arguments], text=True)

    program = ("from pathlib import Path; import os,sys; "
               "text=Path(sys.argv[1]).read_text(); "
               "assert '# Assignment: '+sys.argv[2] in text; "
               "print('NATIVE_REX_OK role='+sys.argv[2]); "
               "print('cwd='+os.getcwd())")
    results = []
    for role in ("qa", "security"):
        launch = ["--rex", args.rex, "--server", args.server, "start", "--task", task,
                  "--id", role, "--role", role, "--goal", "Verify role brief and working directory only.",
                  "--cwd", str(state), "--", sys.executable, "-c", program, "{brief}", role]
        first = json.loads(call(*launch))
        for _ in range(50):
            current = json.loads(call("status", "--task", task, "--id", role))
            if current["state"] in {"exited", "failed"}:
                break
            time.sleep(0.1)
        if current["state"] != "exited":
            raise RuntimeError("Smoke command did not exit successfully: " + json.dumps(current))
        captured = call("capture", "--task", task, "--id", role)
        if "NATIVE_REX_OK role=" + role not in captured or "cwd=" + str(state) not in captured:
            raise RuntimeError("Smoke output did not confirm role and cwd: " + captured)
        repeated = json.loads(call(*launch))
        if repeated["block_id"] != first["block_id"]:
            raise RuntimeError("Repeated launch created another block")
        results.append(current)
    if results[0]["session_id"] != results[1]["session_id"] or results[0]["block_id"] == results[1]["block_id"]:
        raise RuntimeError("Expected one task session with distinct specialist blocks")
    print(json.dumps(dict(result="passed", state_dir=str(state), assignments=results), indent=2))


if __name__ == "__main__":
    main()
