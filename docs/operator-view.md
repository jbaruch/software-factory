# Local operator view

Run the view from the application checkout with Python 3.11+:

```sh
python3 dashboard.py --state-dir '/absolute/path/to/factory-state' --port 8090
```

Open the printed loopback URL. The browser asks for username `operator` and the
password from the private `access_file` path announced at startup. Read that file
locally; do not paste its contents into logs or messages. The random capability
changes on each start. The server binds only `127.0.0.1`. It has no
external assets, client JavaScript, build step or additional dependency. Stop
it with Control-C. `--port 0` chooses a free local port.

`dashboard.py` owns the temporary access file: schema version 1, `username`,
`password` and `url`. Its directory is mode 0700 and the file is mode 0600.
It exists only while this server runs and has no migration or restart reader.
Startup output contains its path, never its capability. All HTML, CSS and API
requests require HTTP Basic authentication before any state is read. This
keeps another local OS user from reading task records through the loopback port.

The first page reads stored records. **Refresh records** reads them again.
**Check live processes** additionally connects to each task's pinned Rex host,
verifies the saved session identity and reads the recorded blocks' process
states. It does not launch, interrupt or reconcile work. Pending identities need
the existing `factory.py status` recovery command. An observation failure stays
unavailable; it never becomes idle, exited or accepted.

Task goals, holds, selected inputs, remaining allowances, current/stale reports
and original evidence references come from the factory's existing owner loader
and generated task view. Process observations occupy a separate column. A
successful process exit does not approve a specialist report. A current report
means its revisions match selected task inputs, not that the task is accepted.
The route table reads the configured provider catalog; it does not probe quota
or infer subscription access, pool independence or unlimited capacity.

The **Team** table shows each task, specialty and assignment with its original
source inputs and their freshness against selected task inputs. Native adapter,
harness, requested model/effort and requested subscription route are derived only
from supported factory-owned Codex/Grok worker envelopes using the owner's argument
parsers and original whole-argument substitutions. Generic commands, unsupported
harnesses, damaged arguments and mismatched assignment bindings remain unknown.
This metadata establishes neither runtime readiness nor subscription eligibility.
Observed model remains unknown; process reads do not establish it. No provider
metadata is refreshed or written by this view.

Saved Rex host/session/window/block IDs remain visible independently of process
state. **Read owned Rex pane** opens an authenticated relative dashboard URL,
`/tasks/<task-slug>/assignments/<assignment-slug>/pane`, with no query parameters.
It reloads the saved record through the owner loader/lock, validates the pinned
host, session marker and saved window/block pair through `factory.connect`, then
reads only that block's process and original terminal capture through the existing
Rex adapter. Each Rex call retains its existing 20-second deadline. There is no
native URL scheme, arbitrary Rex target, focus, input, interrupt, launch or recovery
effect. Refresh repeats only reads. Missing, damaged, launching, foreign or missing
live identities and unavailable transport produce an actionable unavailable page
(HTTP 503); no pane identity is guessed or repaired.

Terminal text is escaped task data. At most the last 65,536 characters returned by
capture are visible; larger captures carry an explicit **Partial view** marker
alongside the original pane identity. Even an unabridged capture is bounded by
Rex's own retained screen/scrollback and is not a complete execution log.
Process observations show their own actual read timestamp, including unknown
process state. Stored-only, unreconciled and failed process reads have no process
timestamp; the header's collection time does not substitute for one. A successful
process read remains timestamped if the later output capture fails. Observations
are transient and are never saved back to task records.

**Pause new assignments** requires a reason of at most 512 characters.
**Resume new assignments** clears the task's hold. Running workers continue in
both cases. These actions have the same authority as the launching user's factory
CLI; protect the access file as an operator credential. The task shows its current
hold and five most recent control requests, with original IDs and timestamps.
Full history remains in the task record/source facts.

Allowance values include explicit additions recorded by the operator's
[`extend-allowance` CLI command](task-state.md). Full allowance-decision history
is available in `factory.py show`; this page's forms change only pause/resume.

Forms submit to `POST /tasks/<task-slug>/hold` using the authenticated process's
CSRF token, a stable request ID and the observed control revision. Foreign origins,
bad tokens, duplicate fields, unknown actions and oversized bodies refuse before
task access. The runtime applies the change under its existing task lock and
atomically saves the hold and request history together. Stale/conflicting requests
return 409; busy/damaged records return 503 without resetting state. A successful
submission redirects to the updated task. Refreshing that page repeats no action;
replaying the original request never overwrites a subsequent operator decision.

The state directory's top-level `<task-slug>.json` names are task records.
Keep other JSON exports in subdirectories or use additional dots in their names,
as native receipts do. Damaged/busy tasks appear with actionable errors while
other readable tasks remain visible. Missing directories represent no prior
tasks; inaccessible paths refuse a successful empty view. The runtime loader may
upgrade accepted legacy formats and create its normal advisory lock files, as
`factory.py show` does. The dashboard creates no second task store.

`GET /api/state` returns transient schema-version-3 source facts. `?live=1` adds
the same explicit Rex observations as the page. `dashboard.py` owns the response
shape: collection time/duration, observation mode, task summaries, per-assignment
process/result facts, task control events, per-task read errors and configured routes. Commands and
instruction contents are omitted. Responses are not cached. The endpoint is a
view, not an authorization or acceptance API. The HTML forms use the separate
hold endpoint above. Version 1 omitted control events; version 2 added those events.
Version 3 adds per-assignment `requested_native` (`adapter`, `harness`, `route`,
`model`, `effort`, each null when unknown), `observed_model` (currently null),
`inputs_current` (boolean or null), `pane` (saved `host_id`, `session_id`, `window_id`,
`block_id`) and `pane_href` (relative detail link or null for pending launches).
`process` now contains `state`, `observed_at` (Unix seconds or null), optional
sanitized recovery `error`, and `exit_code` after a successful process read. It
omits native process/exit objects and their potentially private fields. A link
is a saved target, not proof the pane is currently available. No stored snapshots
are migrated; task schema remains 7 with the owner's existing legacy support.
Older static render callers can omit these additions and remain read-only when
no CSRF token is supplied.

Tests use fixed records, fake Rex observations, socket-free handler exercises and
a temporary loopback server on a host that permits sockets. Host full-suite,
browser and owned live read-only conformance remain separate acceptance gates.
See the [implementation inventory](implementation.md) for observed integration
evidence and the remaining dashboard requirements.
