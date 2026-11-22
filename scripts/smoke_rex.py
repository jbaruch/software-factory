"""Explicit live check of controller logs and helper/worker lifecycles in an owned Rex session."""
import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def check_controller_run(process, log, expected):
    if process.returncode != expected:
        raise RuntimeError(f"Controller process exited {process.returncode}, expected {expected}. "
                           f"Inspect launcher stderr and {log}.")
    try:
        summary = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid launcher JSON. Inspect launcher stdout and {log}.") from exc
    if (not isinstance(summary, dict) or type(summary.get("controller_exit")) is not int
            or summary != dict(log=str(log), controller_exit=expected)):
        raise RuntimeError(f"Launcher JSON did not report exit {expected} and log {log}. Inspect launcher stdout.")

    contents = log.read_text()
    exits = list(re.finditer(r"(?m)^controller_exit=([0-9]+)\n", contents))
    if len(exits) != 1 or exits[0][1] != str(expected):
        raise RuntimeError(f"Expected one controller_exit={expected} record. Inspect {log}.")
    output = contents[:exits[0].start()]
    # The runner prints an indented result. Decode one object at a time so trailing notices are allowed.
    results = []
    decoder = json.JSONDecoder()
    for start in re.finditer(r'\{\n  "schema_version":', output):
        try:
            result, _ = decoder.raw_decode(output, start.start())
        except json.JSONDecodeError:
            continue
        if (type(result.get("schema_version")) is int and result["schema_version"] == 1
                and isinstance(result.get("result"), str) and isinstance(result.get("jobs"), list)):
            results.append(result)
    if expected:
        if results:
            raise RuntimeError(f"Unexpected runner result after shell startup failure. Inspect {log}.")
        if "EARLY_STDOUT" not in output or "EARLY_STDERR" not in output:
            raise RuntimeError(f"Shell startup diagnostics from both streams are required. Inspect {log}.")
    else:
        if len(results) != 1:
            raise RuntimeError(f"Expected one structured runner result. Inspect {log}.")
        if results[0]["result"] != "complete" or results[0]["jobs"] != []:
            raise RuntimeError(f"Expected the empty controller plan to complete with zero jobs. Inspect {log}.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rex", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--state-dir", type=Path, help="Private artifacts directory; use a fresh path for each check")
    args = parser.parse_args()
    state = (args.state_dir or Path(tempfile.mkdtemp(prefix="factory-rex-smoke-"))).resolve()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = state.stat()
    if info.st_uid != os.geteuid() or info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        parser.error("state directory must be private and owned by the current user; choose a fresh path")
    source = state / "source"
    # Admission requires a clean candidate, even when testing uncommitted documentation.
    subprocess.run(["git", "clone", "--shared", "--quiet", str(ROOT), str(source)], check=True)
    base = [sys.executable, "-B", str(ROOT / "skills/factory/runtime/factory.py"), "--state-dir", str(state)]
    task = "native-smoke"

    def call(*arguments):
        return subprocess.check_output([*base, *arguments], text=True)

    def rex(*arguments):
        command = list(arguments)
        # block call already returns JSON and accepts no --json flag.
        if command[:2] != ["block", "call"]:
            command.insert(command.index("--") if "--" in command else len(command), "--json")
        return json.loads(subprocess.check_output(
            [args.rex, "--autostart=false", "-S", args.server, *command], text=True))

    # Admission gives these workers a unique line; never join the operator's default session.
    line = "rex-smoke-" + uuid.uuid4().hex[:12]
    created = rex("new", line, "--window", "operator-sentinel")
    session = created["session_id"]
    print("Rex smoke session: " + session + " (" + line + ")", file=sys.stderr)
    call("admit", "--task", task, "--line", line, "--repo", str(source),
         "--goal", "Verify disposable Rex conformance only.", "--acceptance", "docs/native-rex.md#verification")

    def inventory():
        return sorted(window["window_id"] for window in rex("window", "ls", "-s", session)["windows"])

    sentinel = inventory()
    monitor_log = state / "monitor.log"
    monitor_log.write_text("CONTROLLER_MONITOR_READY\n")
    monitor = rex("run", "-s", session, "--label", "live-monitor", "--shell=none", "--focus=false",
                  "--", "/usr/bin/tail", "-F", str(monitor_log))
    baseline = inventory()
    if baseline != sorted([*sentinel, monitor["window_id"]]):
        raise RuntimeError("Monitor changed an unrelated tab: " + json.dumps(baseline))
    helper = rex("run", "-s", session, "--label", "short-helper", "--shell=none", "--focus=false",
                 "--", sys.executable, "-c", "import time; print('HELPER_OK', flush=True); time.sleep(2)")
    for _ in range(50):
        if inventory() == baseline:
            break
        time.sleep(0.1)
    if inventory() != baseline:
        raise RuntimeError("Short helper did not auto-close: " + helper["window_id"])

    retained_helper = rex("run", "-s", session, "--label", "retained-helper", "--shell=none", "--keep-open",
                          "--focus=false", "--", sys.executable, "-c", "print('RETAINED_HELPER_OK')")
    for _ in range(50):
        process = rex("block", "call", "-s", session, "-b", retained_helper["block_ids"][0], "process")
        if process.get("last_exit") is not None:
            break
        time.sleep(0.1)
    if process.get("last_exit") is None:
        raise RuntimeError("Retained helper is still running; inspect " + retained_helper["window_id"])
    captured = subprocess.check_output([args.rex, "--autostart=false", "-S", args.server, "capture",
        "-s", session, "-b", retained_helper["block_ids"][0], "--trim"], text=True)
    (state / "helper.log").write_text(captured)
    if "RETAINED_HELPER_OK" not in captured:
        raise RuntimeError("Retained helper lost its output")
    subprocess.run([args.rex, "--autostart=false", "-S", args.server, "block", "close",
                    "-s", session, retained_helper["block_ids"][0]], check=True, stdout=subprocess.DEVNULL)
    if inventory() != baseline:
        raise RuntimeError("Retained helper cleanup changed the tab inventory")

    release = state / "release-worker"
    live_program = ("import sys,time; from pathlib import Path; print('LIVE_WORKER_READY', flush=True)\n"
                    "while not Path(sys.argv[1]).exists(): time.sleep(0.1)\n"
                    "print('LIVE_WORKER_DONE', flush=True)")
    live_worker = json.loads(call("--rex", args.rex, "--server", args.server, "start", "--task", task,
        "--id", "live-worker", "--role", "qa", "--purpose", "work", "--goal", "Wait for this check's release file only.",
        "--cwd", str(source), "--", sys.executable, "-c", live_program, str(release)))
    baseline = sorted([*baseline, live_worker["window_id"]])
    if inventory() != baseline:
        raise RuntimeError("Live worker changed an unrelated tab")

    controller = ROOT / "skills/factory/runtime/ready_controller.py"
    controller_state = state / "controllers"
    plan = state / "empty-plan.json"
    plan.write_text(json.dumps(dict(schema_version=1, limits=dict(workers=1, tasks=1,
        reserve_workers=0, minimum_remaining_percent=15), jobs=[])))
    startup = state / "startup-failure.sh"
    startup.write_text("""printf '%s\\n' '{"result":"startup diagnostics ready"}'
printf 'EARLY_STDOUT\\n'
printf 'EARLY_STDERR\\n' >&2
exit 37
""")
    controller_runs = []
    for label, expected in (("success", 0), ("startup-failure", 37), ("retry", 0)):
        log = state / (label + ".log")
        env = os.environ.copy()
        if label == "startup-failure":
            env["BASH_ENV"] = str(startup)
        result = subprocess.run([sys.executable, "-B", str(controller), str(log), str(ROOT), sys.executable,
            "--", "--state-dir", str(controller_state), "--plan", str(plan),
            "--codex", str(state / "offline-codex"), "--rex", args.rex, "--server", args.server, "--once"],
            env=env, capture_output=True, text=True)
        check_controller_run(result, log, expected)
        after = inventory()
        if after != baseline:
            raise RuntimeError("Controller changed the tab inventory: " + json.dumps(after))
        if json.loads(call("status", "--task", task, "--id", "live-worker"))["state"] != "running":
            raise RuntimeError("Controller disturbed the running worker")
        controller_runs.append(dict(run=label, exit=expected, log=str(log), windows=after))

    program = ("from pathlib import Path; import os,sys; "
               "text=Path(sys.argv[1]).read_text(); "
               "assert '# Assignment: '+sys.argv[2] in text; "
               "print('NATIVE_REX_OK role='+sys.argv[2]); "
               "print('cwd='+os.getcwd())")
    results = []
    for role in ("qa", "security"):
        launch = ["--rex", args.rex, "--server", args.server, "start", "--task", task, "--purpose", "work",
                  "--id", role, "--role", role, "--goal", "Verify role brief and working directory only.",
                  "--cwd", str(source), "--", sys.executable, "-c", program, "{brief}", role]
        first = json.loads(call(*launch))
        for _ in range(50):
            current = json.loads(call("status", "--task", task, "--id", role))
            if current["state"] in {"exited", "failed"}:
                break
            time.sleep(0.1)
        if current["state"] != "exited":
            raise RuntimeError("Smoke command did not exit successfully: " + json.dumps(current))
        captured = json.loads(call("capture", "--task", task, "--id", role))["text"]
        if "NATIVE_REX_OK role=" + role not in captured or "cwd=" + str(source) not in captured:
            raise RuntimeError("Smoke output did not confirm role and cwd: " + captured)
        repeated = json.loads(call(*launch))
        if repeated["block_id"] != first["block_id"]:
            raise RuntimeError("Repeated launch created another block")
        results.append(current)
    if any(row["session_id"] != session for row in results) or results[0]["block_id"] == results[1]["block_id"]:
        raise RuntimeError("Expected one task session with distinct specialist blocks")
    tidied = json.loads(call("tidy", "--task", task))[task]
    if "error" in tidied or tidied["running"] != ["live-worker"]:
        raise RuntimeError("Tidy did not preserve only the running worker: " + json.dumps(tidied))
    if inventory() != baseline:
        raise RuntimeError("Worker tidy changed the sentinel or live monitor")
    for role, original in zip(("qa", "security"), results):
        retained = json.loads(call("status", "--task", task, "--id", role))
        if retained.get("closed") is not True or retained["state"] != "exited" or retained["block_id"] != original["block_id"]:
            raise RuntimeError("Tidied tab lost its final status: " + json.dumps(retained))
        if "NATIVE_REX_OK role=" + role not in json.loads(call("capture", "--task", task, "--id", role))["text"]:
            raise RuntimeError("Tidied tab lost its terminal output: " + role)
    release.touch()
    for _ in range(50):
        current = json.loads(call("status", "--task", task, "--id", "live-worker"))
        if current["state"] == "exited":
            break
        time.sleep(0.1)
    if current["state"] != "exited":
        raise RuntimeError("Live worker did not finish after release")
    final_tidy = json.loads(call("tidy", "--task", task))[task]
    if "error" in final_tidy or final_tidy["running"] or inventory() != sorted([*sentinel, monitor["window_id"]]):
        raise RuntimeError("Finished live worker was not closed without disturbing operator tabs")
    if "LIVE_WORKER_DONE" not in json.loads(call("capture", "--task", task, "--id", "live-worker"))["text"]:
        raise RuntimeError("Live worker output was not saved before close")
    captured = subprocess.check_output([args.rex, "--autostart=false", "-S", args.server, "capture",
        "-s", session, "-b", monitor["block_ids"][0], "--trim"], text=True)
    if "CONTROLLER_MONITOR_READY" not in captured:
        raise RuntimeError("Live monitor lost its output")
    process = rex("block", "call", "-s", session, "-b", monitor["block_ids"][0], "process")
    if process.get("last_exit") is not None or not (process.get("child") or process.get("foreground")):
        raise RuntimeError("Live monitor stopped before disposable-session teardown")
    # Monitoring is done: stop only this check's monitor by its saved block ID.
    subprocess.run([args.rex, "--autostart=false", "-S", args.server, "send-key",
                    "-s", session, "-b", monitor["block_ids"][0], "ctrl+c"], check=True,
                   stdout=subprocess.DEVNULL)
    for _ in range(50):
        if inventory() == sentinel:
            break
        time.sleep(0.1)
    if inventory() != sentinel:
        raise RuntimeError("Owned monitor did not close after monitoring ended")
    # Only this check's session is disposable, including its synthetic operator sentinel.
    subprocess.run([args.rex, "--autostart=false", "-S", args.server, "kill", session], check=True,
                   stdout=subprocess.DEVNULL)
    if any(row["session_id"] == session for row in rex("ls").get("sessions", [])):
        raise RuntimeError("Disposable smoke session remained open: " + session)
    print(json.dumps(dict(result="passed", state_dir=str(state), session=session, baseline=baseline,
        helper=helper["window_id"], monitor=monitor, controllers=controller_runs, assignments=results, tidy=tidied), indent=2))


if __name__ == "__main__":
    main()
