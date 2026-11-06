"""Local operator view generated from factory-owned records and optional Rex observations."""
import argparse
import base64
import binascii
from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

import factory

ASSETS = Path(__file__).resolve().parent / "web"


def snapshot(state_dir, *, live=False, connect=factory.connect, clock=time.time, timer=time.monotonic) -> dict:
    started = timer()
    try:
        paths = sorted(path for path in state_dir.iterdir() if path.suffix == ".json")
    except FileNotFoundError:
        paths = []
    except NotADirectoryError:
        raise factory.FactoryError("State path is not a directory; supply the factory's --state-dir") from None
    except OSError:
        raise factory.FactoryError("Cannot list task records; restore access to the factory state directory") from None
    tasks, errors = [], []
    for path in paths:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", path.stem):
            continue  # Receipts and schemas are evidence, not task records.
        try:
            with factory.task_lock(state_dir, path.stem):
                record = factory.load_task(path)
                view = factory.task_view(record)
            rex, observation_error = None, None
            if live and record["rex"] is not None:
                try:
                    rex = connect(record)
                except (factory.FactoryError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
                    observation_error = "Inspect the pinned Rex host/session: " + str(exc)
            assignments = []
            for identity, assignment in view["assignments"].items():
                process = dict(state="not_checked")
                if live:
                    if observation_error:
                        process = dict(state="unavailable", error=observation_error)
                    elif assignment["state"] == "launching":
                        process = dict(state="unreconciled", error="Use factory status to reconcile the existing launch")
                    elif rex is not None:
                        try:
                            process = factory.status(rex, record, assignment)
                        except (factory.FactoryError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
                            process = dict(state="unavailable", error="Inspect the original Rex pane: " + str(exc))
                assignments.append(dict(id=identity, role=assignment["request"]["role"],
                    goal=assignment["request"]["goal"], launch_state=assignment["state"],
                    inputs=assignment["request"].get("inputs"), process=process,
                    result=assignment["result"], result_current=assignment["result_current"]))
            tasks.append(dict(id=record["task"], goal=(record["admission"] or {}).get("goal"),
                              hold=record["hold"], inputs=record["inputs"],
                              control_events=record["control_events"],
                              allowances=view["allowances"], assignments=assignments))
        except (factory.FactoryError, OSError) as exc:
            errors.append(dict(task=path.stem, error=str(exc)))
    catalog = json.loads((factory.ROOT / "config/providers.json").read_text())
    if (not isinstance(catalog, dict) or type(catalog.get("schema_version")) is not int
            or catalog["schema_version"] != 1 or not isinstance(catalog.get("routes"), list)):
        raise factory.FactoryError("Unsupported provider catalog; update the dashboard before reading this format")
    if any(not isinstance(route, dict) or "quota_observation" not in route or any(
            not isinstance(route.get(key), str) or not route[key].strip()
            for key in ("id", "harness", "readiness")) for route in catalog["routes"]):
        raise factory.FactoryError("Incomplete provider catalog; restore its route identities and observations")
    return dict(schema_version=2, observed_at=clock(), collection_seconds=timer() - started,
                live=live, tasks=tasks, errors=errors, routes=catalog["routes"])


def text(value):
    return escape(str(value).encode("utf-8", errors="backslashreplace").decode("utf-8"), quote=True)


def render(view, *, csrf_token=None):
    tasks = []
    for task in view["tasks"]:
        rows = []
        for assignment in task["assignments"]:
            process = assignment["process"]
            state = process["state"]
            label = {"not_checked": "Not checked", "unreconciled": "Needs reconciliation",
                     "exited": "Exited successfully"}.get(state, state.capitalize())
            observed = f'<span class="process {text(state)}">{text(label)}</span>'
            if process.get("error"):
                observed += f'<p class="detail">{text(process["error"])}</p>'
            report = assignment["result"]
            result = '<span class="muted">No report</span>'
            if report is not None:
                current = assignment["result_current"]
                result = (f'<span class="report {text(report["outcome"])} {"current" if current else "stale"}">'
                          f'{text(report["outcome"].capitalize())} ({"current" if current else "stale"})</span>')
                evidence = "".join(f'<li><code>{text(ref)}</code></li>' for ref in report["artifacts"])
                result += f'<details><summary>Evidence</summary><ul>{evidence}</ul></details>'
            rows.append(f'<tr><th scope="row">{text(assignment["role"])}<small>{text(assignment["id"])}</small></th>'
                        f'<td>{text(assignment["goal"])}</td><td>{observed}</td><td>{result}</td></tr>')
        table = ('<div class="table-scroll"><table><thead><tr><th scope="col">Specialty</th>'
                 '<th scope="col">Assignment</th><th scope="col">Process observation</th>'
                 '<th scope="col">Specialist report</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'
                 if rows else '<p class="empty">No assignments recorded.</p>')
        hold = (f'<p class="hold">Paused: {text(task["hold"])}</p>' if task["hold"]
                else '<p class="hold-status">Not paused</p>')
        controls = ""
        if csrf_token is not None:
            action = "resume" if task["hold"] is not None else "pause"
            identity = text(task["id"])
            reason = (f'<label for="reason-{identity}">Pause reason</label>'
                      f'<input id="reason-{identity}" name="reason" maxlength="512" required>'
                      if action == "pause" else '<input type="hidden" name="reason" value="">')
            controls = (f'<div class="task-controls"><form method="post" action="/tasks/{identity}/hold">'
                        f'<input type="hidden" name="csrf" value="{text(csrf_token)}">'
                        f'<input type="hidden" name="request_id" value="{secrets.token_hex(16)}">'
                        f'<input type="hidden" name="expected_revision" value="{len(task["control_events"])}">'
                        f'<input type="hidden" name="action" value="{action}">'
                        f'{reason}<button>{action.capitalize()} new assignments</button></form>'
                        '<p class="muted">Pausing blocks new assignments. Running workers continue.</p></div>')
        history = []
        for event in reversed(task["control_events"][-5:]):
            label = "Resumed" if event["hold"] is None else "Paused: " + event["hold"]
            when = datetime.fromtimestamp(event["at"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            history.append(f'<li>{text(label)} <time>{text(when)}</time><small>Request {text(event["id"])}</small></li>')
        trace = ('<details class="control-history"><summary>Recent control requests</summary><ol>'
                 + "".join(history) + '</ol></details>' if history else "")
        limits = "Allowances unknown (task not admitted)"
        if task["allowances"] is not None:
            limits = "; ".join(f'{name.capitalize()}: {values["remaining"]} remaining of {values["limit"]}'
                               for name, values in task["allowances"].items())
        inputs = task["inputs"]
        revisions = (f'<details class="revisions"><summary>Selected inputs</summary><dl><dt>Candidate</dt>'
                     f'<dd><code>{text(inputs["candidate"])}</code></dd><dt>Requirements</dt>'
                     f'<dd><code>{text(inputs["requirements"])}</code></dd></dl></details>' if inputs else "")
        tasks.append(f'<section class="task" aria-labelledby="task-{text(task["id"])}">'
                     f'<div class="task-heading"><div><h2 id="task-{text(task["id"])}">{text(task["id"])}</h2>'
                     f'<p class="goal">{text(task["goal"] or "Scope not admitted")}</p></div>{revisions}</div>'
                     f'{hold}{controls}{trace}{table}<p class="allowances">{text(limits)}</p></section>')
    errors = "".join(f'<li><strong>{text(item["task"])}</strong>: {text(item["error"])}</li>' for item in view["errors"])
    unavailable = f'<aside class="errors"><h2>Records needing attention</h2><ul>{errors}</ul></aside>' if errors else ""
    routes = "".join(f'<tr><th scope="row">{text(route["id"])}</th><td>{text(route["harness"])}</td>'
                     f'<td>{text(route["readiness"])}</td><td>'
                     f'{"Unknown" if route["quota_observation"] is None else "Recorded; not refreshed"}</td></tr>'
                     for route in view["routes"])
    timestamp = datetime.fromtimestamp(view["observed_at"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    mode = "Live processes checked" if view["live"] else "Stored records only"
    facts = "/api/state?live=1" if view["live"] else "/api/state"
    content = "".join(tasks) or '<section class="task empty"><h2>No task records</h2><p>Admit a task in the factory CLI, then refresh this view.</p></section>'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>Software Factory</title><link rel="stylesheet" href="/dashboard.css"></head><body>'
            f'<header><div class="brand">Software Factory</div><div class="observation">{text(mode)}<br><time>{text(timestamp)}</time></div></header>'
            '<main><div class="toolbar"><div><h1>Work in progress</h1><p>Process observations and specialist reports stay separate.</p></div>'
            '<form method="get"><a class="button secondary" href="/">Refresh records</a><button name="live" value="1">Check live processes</button></form></div>'
            f'{unavailable}{content}<section class="routes"><h2>Subscription routes</h2><p>Configured readiness; capacity has not been refreshed here.</p>'
            '<div class="table-scroll"><table><thead><tr><th scope="col">Route</th><th scope="col">Harness</th><th scope="col">Catalog status</th><th scope="col">Capacity observation</th></tr></thead>'
            f'<tbody>{routes}</tbody></table></div></section><footer>Reports do not accept a task or satisfy release gates. '
            f'This view collected records in {view["collection_seconds"]:.3f}s. <a href="{facts}">View source facts</a></footer></main></body></html>')


def authorized(header, access_key):
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, password = base64.b64decode(encoded.strip(), validate=True).decode("utf-8").split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    return user == "operator" and secrets.compare_digest(password.encode(), access_key.encode())


@contextmanager
def access_file(access_key, url):
    """Only the launching user can read this process's temporary capability."""
    with tempfile.TemporaryDirectory(prefix="software-factory-view-") as directory:
        path = Path(directory) / "access.json"
        factory.save(path, dict(schema_version=1, username="operator", password=access_key, url=url))
        yield path


def make_server(state_dir, port=8090, *, access_key):
    if not isinstance(access_key, str) or not access_key:
        raise ValueError("Supply a nonempty private per-process access capability")
    csrf_token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def check_access(self):
            allowed = {f"127.0.0.1:{active_port}", f"localhost:{active_port}"}
            if active_port == 80:
                allowed.update(("127.0.0.1", "localhost"))
            if self.headers.get("Host", "").lower() not in allowed:
                self.send_error(403, "Use this dashboard's loopback address")
                return False
            if not authorized(self.headers.get("Authorization", ""), access_key):
                body = b'{"error":"Use operator credentials from the private access file announced at startup"}'
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Software Factory", charset="UTF-8"')
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return False
            return True

        def do_GET(self):
            if not self.check_access():
                return
            target = urlsplit(self.path)
            query = parse_qs(target.query, keep_blank_values=True)
            if target.netloc or query not in ({}, {"live": ["1"]}):
                self.send_error(400, "Use only the live=1 observation option")
                return
            if target.path not in ("/", "/api/state", "/dashboard.css"):
                self.send_error(404, "Use / or /api/state")
                return
            status, content_type = 200, "text/html; charset=utf-8"
            try:
                if target.path == "/dashboard.css":
                    body, content_type = (ASSETS / "dashboard.css").read_text(), "text/css; charset=utf-8"
                else:
                    view = snapshot(state_dir, live=bool(query))
                    if target.path == "/api/state":
                        body, content_type = json.dumps(view, allow_nan=False), "application/json"
                    else:
                        body = render(view, csrf_token=csrf_token)
            except (factory.FactoryError, OSError, ValueError) as exc:
                status = 503
                if target.path == "/api/state":
                    body, content_type = json.dumps(dict(schema_version=2, error=str(exc))), "application/json"
                else:
                    body = f'<h1>Factory records unavailable</h1><p>{text(exc)}</p><p>Restore the source, then refresh.</p>'
                print("dashboard: Restore the source, then refresh. " + str(exc), file=sys.stderr)
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if not self.check_access():
                return
            target = urlsplit(self.path)
            match = re.fullmatch(r"/tasks/([a-z0-9][a-z0-9-]{0,63})/hold", target.path)
            if target.netloc or target.query or target.fragment or match is None:
                self.send_error(404, "Use a task's pause/resume form")
                return
            origin = self.headers.get("Origin")
            if origin is not None and origin != "http://" + self.headers.get("Host", ""):
                self.send_error(403, "Submit controls from this dashboard's own page")
                return
            try:
                length = self.headers.get("Content-Length", "")
                if (self.headers.get("Transfer-Encoding") is not None or not re.fullmatch(r"[0-9]+", length)
                        or not 0 < int(length) <= 8192
                        or self.headers.get("Content-Type", "").split(";", 1)[0] != "application/x-www-form-urlencoded"):
                    raise ValueError("Use a bounded form body")
                fields = parse_qs(self.rfile.read(int(length)).decode("utf-8"), keep_blank_values=True,
                                  strict_parsing=True, errors="strict")
                if (set(fields) != {"csrf", "request_id", "expected_revision", "action", "reason"}
                        or any(len(values) != 1 for values in fields.values())):
                    raise ValueError("Use the complete task form without duplicate fields")
                values = {key: items[0] for key, items in fields.items()}
                if not secrets.compare_digest(values["csrf"].encode(), csrf_token.encode()):
                    self.send_error(403, "Refresh the dashboard before submitting controls")
                    return
                if (values["action"] not in {"pause", "resume"}
                        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", values["request_id"])
                        or not re.fullmatch(r"[0-9]+", values["expected_revision"])
                        or len(values["reason"]) > 512
                        or (values["action"] == "pause" and not values["reason"].strip())
                        or (values["action"] == "resume" and values["reason"] != "")):
                    raise ValueError("Supply a valid action, request ID, revision and pause reason")
                revision = int(values["expected_revision"])
            except (ValueError, UnicodeDecodeError):
                self.send_error(400, "Invalid control request; refresh the form and supply a pause reason of at most 512 characters")
                return
            try:
                with factory.task_lock(state_dir, match[1]) as path:
                    factory.hold_task(path, values["reason"] if values["action"] == "pause" else None,
                                      request_id=values["request_id"], expected_revision=revision)
            except factory.ControlConflict as exc:
                self.send_error(409, str(exc))
                return
            except (factory.FactoryError, OSError) as exc:
                self.send_error(503, "Task controls unavailable; inspect/restore the original state. " + str(exc))
                return
            self.send_response(303)
            self.send_header("Location", "/#task-" + match[1])
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, fmt, *args):
            # Keep private request URLs out of access logs; source failures go to stderr above.
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    active_port = server.server_port
    return server


def port_number(value):
    number = int(value)
    if not 0 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be 0–65535; choose 0 for a free local port")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=factory.ROOT / ".factory-state")
    parser.add_argument("--port", type=port_number, default=8090)
    args = parser.parse_args(argv)
    try:
        access_key = secrets.token_urlsafe(32)
        with make_server(args.state_dir.resolve(), args.port, access_key=access_key) as server:
            url = f"http://127.0.0.1:{server.server_port}"
            with access_file(access_key, url) as path:
                print(json.dumps(dict(schema_version=1, url=url, username="operator", access_file=str(path),
                                      state_dir=str(args.state_dir.resolve()))), flush=True)
                server.serve_forever()
    except (OSError, ValueError) as exc:
        print("dashboard: Cannot start local view; choose an accessible state directory and free --port. " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
