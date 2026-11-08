# Local operator view

Run the view from the application checkout with Python 3.11+:

```sh
python3 dashboard.py --state-dir '/absolute/path/to/factory-state' \
  --decision-store '/absolute/path/to/private/decisions' --port 8090
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
The exact canonical regular worker file must belong to this application root or
a currently registered worktree of its actual Git repository. A read-only Git
inventory binds original-owner commands when the view runs from another application
checkout; arbitrary project repositories, unrelated clones, retired worktrees and
missing/linked worker paths confer no ownership. Rendering executes no other
checkout's code and neither reads nor initializes repository identity markers.
This metadata establishes neither runtime readiness nor subscription eligibility.
Observed model remains unknown; process reads do not establish it. No provider
metadata is refreshed or written by this view.

Saved Rex host/session/window/block IDs remain visible independently of process
state. **Read owned Rex pane** opens an authenticated relative dashboard URL,
`/tasks/<task-slug>/assignments/<assignment-slug>/pane`, with no query parameters.
It loads and validates one saved record through the owner loader/lock, releases
the lock before any Rex call, validates that snapshot's pinned host, session
marker and saved window/block pair through `factory.connect`, then
reads only that block's process and original terminal capture through the existing
Rex adapter. Concurrent controls and same-task launchers can acquire the task lock
while Rex responds. Saved-state changes cannot retarget this observation; it uses
the same loaded assignment/IDs throughout and never saves that snapshot back.
Each Rex call retains its existing 20-second deadline. There is no
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
[`extend-allowance` CLI command](task-state.md). Task detail shows original admission
limits/references and the full original allowance-decision history. **Approve one
additional correction/investigation attempt** requires an explicit human approval
reason (nonempty, at most 512 characters). It adds exactly one attempt through
`factory.extend_allowance` under the existing task lock. Original admission,
consumed attempts, selected inputs, reports, holds and past events stay intact.
Nothing starts work or grants acceptance. This control is not permission for an
agent/classifier to grant itself budget.

The **Decision inbox** groups operator, standby-reasoner, investigator and judge
proposals. Task conditions come directly from `task_view`: holds, current
blocked/findings reports with original artifacts, and exhausted admitted
correction/investigation allowances. Zero allowance with no attempts is an
authorization condition, not a failed loop. Correction exhaustion proposes
investigation only with attempted correction work relevant to current inputs and
current blocked/findings evidence. Existing completed investigation evidence goes
to reassessment; pending work is not duplicated. Blocked investigations or no
remaining investigation allowance require an operator decision. Explicit holds
remain binding. These diagnostics dispatch nothing. Unknown finding
recipients/options remain operator planning. Stale
reports stay visible in Team and do not become current inbox findings.

`--decision-store` is optional. Without it, Jev evidence is unknown/not configured.
A selected missing directory means no retained decisions according to its owner's
first-use contract; no directory/evaluation is created. Read failures, damaged or
unsupported records remain unavailable, never an empty/all-resolved queue. Owner
readers reconcile collected original native answers offline. Their errors are
sanitized; no state-supplied path is used to locate a task. Only explicit
investigator/native task IDs under `--state-dir` can bind tasks. Generic decisions
can be unbound and their evidence freshness remains unknown.

Bounded before/after identity and content checks surround owner collection of
selected Jev/task records. New, changed, removed or unreadable sources make the
inbox unavailable and suppress mixed items/current native resolutions until a
fresh read. These transient checks create no snapshot store or transaction and
do not establish freshness after collection.

Each item separates original shadow owner/condition, current condition, action
availability, used/remaining attempts when bound, original evidence revision and
references, original question ID/version/options, and the exact decision needed.
Question instruction contents and raw classifier state/payloads/native argv are
omitted. Judges additionally show the original bounded dispute question/options.
Stale, conflicting, blocked, uncollected, engine-unavailable and unbound cases
remain explicit operator items. Missing/blocked/stale or changed investigation
evidence blocks judging, including for an already recorded native ruling; an
original blocked investigation cannot be upgraded by a later result. Recorded
current answers appear separately and establish no task acceptance. All text is
escaped; source references are text, with relative task/source-fact links only.

Forms submit to `POST /tasks/<task-slug>/hold` using the authenticated process's
CSRF token, a stable request ID and the observed control revision. Foreign origins,
bad tokens, duplicate fields, unknown actions and oversized bodies refuse before
task access. The runtime applies the change under its existing task lock and
atomically saves the hold and request history together. Stale/conflicting requests
return 409; busy/damaged records return 503 without resetting state. A successful
submission redirects to the updated task. Refreshing that page repeats no action;
replaying the original request never overwrites a subsequent operator decision.

Allowance forms use `POST /tasks/<task-slug>/allowance` with exactly `csrf`,
`request_id`, `purpose` (`correction` or `investigation`), `expected_limit` and
`reason`. The server fixes the addition at one. Authentication, Host, an exact
same-origin Origin, valid CSRF, strict path/field validation and an 8,192-byte body
bound precede task access. It accepts no external targets or arbitrary additions.
An identical replay after a lost reply or later decisions adds nothing; conflicting
IDs/stale limits return 409, busy/damaged/unadmitted tasks return 503. Success
redirects to task detail. Page loads/refreshes/scripts grant no budget. Static
render callers without CSRF have no mutation forms.

The state directory's top-level `<task-slug>.json` names are task records.
Keep other JSON exports in subdirectories or use additional dots in their names,
as native receipts do. Damaged/busy tasks appear with actionable errors while
other readable tasks remain visible. Missing directories represent no prior
tasks; inaccessible paths refuse a successful empty view. The runtime loader may
upgrade accepted legacy formats and create its normal advisory lock files, as
`factory.py show` does. The dashboard creates no second task store.

`GET /api/state` returns transient schema-version-4 source facts. `?live=1` adds
the same explicit Rex observations as the page. `dashboard.py` owns the response
shape: collection time/duration, observation mode, task summaries, per-assignment
process/result facts, task control events, optional task provisioning facts,
per-task read errors and configured routes. Commands and
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
is a saved target, not proof the pane is currently available.
Version 4 includes each assignment's original `purpose` (null for legacy unknown)
and adds per-task allowlisted `admission` (`goal`, `initial_candidate`,
`acceptance`, original `limits`) and original `allowance_events`, plus
`decision_inbox` with `status` (`not_configured`, `available`, `unavailable`),
sanitized nullable `error`, `conditions`, `items` and `resolutions`. Task conditions
carry owner, condition, task/assignment and artifact references, allowance facts,
availability/prerequisites where applicable and the decision needed.
Jev items/resolutions carry original `proposal_owner`/
`proposal_reason`, current `owner`/`condition`, nullable bound task, original
decision/question/version/options/dispute question and evidence revision/reference,
explicit freshness/availability/decision needed, allowlisted native `attempts`
and nullable `recorded_answer`. Pending/failed evaluations also expose
`evaluation_questions` with original IDs/versions/options (no instructions).
Native answers expose only choice/ruling,
consequences and missing input, never the original receipt/event objects. No stored snapshots
are migrated; the [task owner](task-state.md#schema-and-ownership) defines stored task schemas and legacy support.
Older static render callers can omit these additions and remain read-only when
no CSRF token is supplied.

Tests use fixed records, fake Rex observations, socket-free handler exercises and
a temporary loopback server on a host that permits sockets. Host full-suite,
browser and owned live read-only conformance remain separate acceptance gates.
See the [implementation inventory](implementation.md) for observed integration
evidence and the remaining dashboard requirements.
