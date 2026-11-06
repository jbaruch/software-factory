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
GET requests require HTTP Basic authentication before any state is read. This
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

The state directory's top-level `<task-slug>.json` names are task records.
Keep other JSON exports in subdirectories or use additional dots in their names,
as native receipts do. Damaged/busy tasks appear with actionable errors while
other readable tasks remain visible. Missing directories represent no prior
tasks; inaccessible paths refuse a successful empty view. The runtime loader may
upgrade accepted legacy formats and create its normal advisory lock files, as
`factory.py show` does. The dashboard creates no second task store.

`GET /api/state` returns transient schema-version-1 source facts. `?live=1` adds
the same explicit Rex observations as the page. `dashboard.py` owns the response
shape: collection time/duration, observation mode, task summaries, per-assignment
process/result facts, per-task read errors and configured routes. Commands and
instruction contents are omitted. Responses are not cached. The endpoint is a
view, not an authorization or acceptance API; it has no mutation endpoints.

Tests use fixed records, fake Rex observations and a temporary loopback server.
See the [implementation inventory](implementation.md) for observed integration
evidence and the remaining dashboard requirements.
