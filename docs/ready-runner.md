# Explicit ready-work runner

`ready_runner.py` executes an operator-selected plan of native Codex, Claude, Grok and Antigravity jobs. It observes existing
Rex jobs, collects successful native receipts, then launches ready assignments.
It uses the existing task store and its cumulative attempt limits. It does not
select models, interpret findings, accept tasks or release changes;
[delivery](integration.md) belongs to the release skill and `delivered`.

Admit tasks and select their exact candidate/requirements through
[the task owner](task-state.md). Keep one state directory per factory instance;
the runner counts every recorded assignment in that directory, including jobs
outside the supplied plan. Other instances and native CLI use outside this
factory are not part of its work limits.

```sh
python3 -B skills/factory/runtime/ready_runner.py --state-dir '/absolute/path/to/state' \
  --plan '/absolute/path/to/plan.json' \
  --codex '/absolute/path/to/codex' --claude '/absolute/path/to/claude' --grok '/absolute/path/to/grok' \
  --antigravity '/absolute/path/to/agy' \
  --rex '/absolute/path/to/rex' --server 'unix:///explicit/socket' \
  --seconds 600
```

`--once` performs one observation/dispatch cycle. Otherwise the runner polls
every five seconds until all planned reports succeed, a settled plan needs
action, or the orchestration budget expires. A budget return leaves workers
running; invoke the same plan again to observe/reconcile their existing IDs.
Individual Rex/Git calls retain their own bounded deadlines. The loop budget is
checked between cycles and does not interrupt a call or worker.

## Input and output contracts

`ready_runner.py` owns ready-plan schema 1 and generated result schema 1. Plans
are declarative operator input, not mutable queue history. No migration or
rewriting occurs. Unknown versions, unknown fields, duplicate JSON keys,
non-finite values and invalid job graphs refuse before execution. Keep plans
private when goals or paths are sensitive; never include credentials.

The job fields, an example plan and the per-harness rules ship with the skill in
[the ready-plan reference](../skills/factory/references/ready-plan.md), so an installed
coordinator can read them. A writer plan reports `complete` once the writer's commit
is collected; the commit is not yet the task's selection. Explicitly select that
produced candidate and submit a verification phase, or use the opt-in handoff below.
Run again, the original writer plan reports the writer by its result, since its commit
is now the selection; any other job in that plan, read-only jobs included, reports stale.

## Opt-in writer candidate handoff

For an explicitly authorized independent verification phase, add
`--from-writer ASSIGNMENT_ID` to the command above. Keep the verification plan's
candidate/requirements at the writer's **original frozen input pair**; no manual
copy of the output SHA is needed. Ready-plan schema 1 and the task schema stay
unchanged.

The plan may contain jobs for several admitted tasks; in each, the writer is the
assignment with that ID. Every job must be a read-only verifier (`qa`, `code-review`, `test-automation`, `security`, `ui-design`,
`accessibility`, `data-engineering`, `sre` or `performance`), use an ID distinct from the writer, and name
that existing writer explicitly in `after`. Every task's writer is checked before
any task's inputs move, so one refused task leaves all of them unchanged. Additional explicit
dependencies retain their normal gates. The source must already have a collected
`completed` native result, on any harness, and an explicit factory commit that created a
commit: a writer that committed nothing (`unchanged`, which runtimes before 0.39.5 collected
as completed) refuses, because its candidate is the one already verified. The runner
revalidates its original receipt/events, commit and repository/worktree binding,
observes its successful terminal Rex process, and compares the saved result with
that evidence. A manually recorded completion alone is insufficient. Collect the
writer through the existing runner or its harness's `factory.py` collect command first.

Under the instance execution lock and task lock, the runner selects the exact
produced commit with the original requirements revision through the task owner.
It retargets only its in-memory verification plan. Initial selection refuses
holds, selected inputs outside the original/produced pair, changed requirements
and any other active or unknown assignment in the task. Actual Rex processes
must be observed; a saved result never establishes terminal status. Unknown
launches need reconciliation before selection can proceed.

Restart with the same original verification plan and writer ID. An already
selected target is reused without rewriting task inputs or resetting attempts;
existing verification IDs remain observable, including during holds. There is no
continuity ledger, and supplied plans, original requests/results, holds and
limits are preserved. Capacity, checkout, dependency and allowance checks still
gate every new launch. This selects a candidate for verification only; it never
accepts a task, pushes, merges or waives review. Offline tests exercise these
mechanics with fake native/Rex transports and real temporary Git commits; they
do not establish live handoff integration or specialist quality.

## Execution and recovery

If a takeover reports a Python interpreter mismatch, confirm the installation
recorded for that task and assignment against the original launcher. Invoke that
exact absolute Python with `-B` and the same installed `ready_runner.py`. Its
`sys.executable` must match the reported path exactly. Retry with the original
arguments, including `--plan`, `--from-writer` if used, the same state directory
or default environment, provider paths, `--rex` and `--server`. If Rex normally
resolves Python through PATH, pin the confirmed absolute interpreter in the
trusted Rex launcher. Treat the reported path as escaped data. If the original
installation is missing, its identity is uncertain, or assignments report
different interpreters, stop and reconcile the original launcher provenance.
Keep the plan, assignment IDs, state and evidence for the retry.

The owner store retains launch intent before Rex effects. A lost reply reconciles
the original pane; failed or missing/unknown effects never acquire a new attempt
automatically. A preflight failure with no intent is blocked for the invocation;
repair it before explicitly restarting. Holds and limits remain owner-controlled.
Collection continues during holds. Plans never reset budgets or mutate holds.

Generated results contain `result`, per-job `state`/optional `reason`, current
`active_workers`, state `issues`, safe `capacity`/`capacity_error`, measured
`elapsed_seconds` and `metrics` (polls, quota probes, launches and collections).
`complete` means only that every planned assignment has a current successful
report and successful process observation. It is not task acceptance. Other
results are `needs_action`, `pending` for a one-cycle observation, and
`pending_at_budget`. The CLI returns zero only for `complete`.

`--seconds` sets the dispatch budget. The CLI observes native quotas after
releasing the line execution lock. Native observation commands and PTY
exchanges have their own timeouts, and PTY cleanup has a separate bounded
grace and reap. Reporting can extend wall-clock time beyond `--seconds`.
`elapsed_seconds` includes reporting.

The CLI result carries `harnesses`, one entry per supplied binary, for planning
new jobs. Each entry is `{available, reason, windows}`. `true` means the
Codex pool and catalog gates pass. `false` gives an observation error or a
capacity restriction. `null` means measured headroom is available for planning
and model eligibility or model-to-pool binding still needs evidence. Each
window contains `{pool, remaining_percent, resets_at}`. Claude and Grok keep
their native reset labels. Reobserve after a reset before using the route.

Claude opens its interactive `/usage` tab in safe mode with screen-reader output
from resolved `/tmp` by default. If Claude asks for folder
trust, select an absolute, existing, accessible directory with
`--claude-usage-cwd`. Use a dedicated private folder outside repositories, free
of project configuration, imports, and relevant inherited configuration. The
owner must approve the folder in Claude Code's own UI under the intended native
profile. The runner withholds input at the trust prompt. Its all-model and
session windows, when reported, constrain the route. Optional model-specific
windows are reported under their native names. The quota observation requires
usage credits to be off. Grok reads the included weekly limit with `/usage show`
from the dashboard before opening an agent session. Headroom comes from the
included limit. [Grok usage limits](https://docs.x.ai/grok/faq)

Both observers require a recognized interactive
prompt and built-in command entry, use the normal native profile, and keep
terminal output private. Sign in to the intended native subscriptions before
refreshing. Antigravity reports its weekly groups and is unavailable
when all observed groups are at or below reserve.

The [route selection steps](../skills/factory/references/ready-plan.md#selecting-a-route)
use the report for planning. Explicit plans retain their native worker and
admission checks. The Codex launch gate uses its fresh capacity snapshot and
the plan's reserve.

## Limits and reservations

Worker slots and active task slots are reserved from observed processes, including
unknown and unconfirmed launches. A terminal process releases its slot even if
its report is unusable; that report still needs action. A process `lost` to a
Rex server restart is terminal: the runner collects a native Codex result from
its completed receipt, and otherwise reports `blocked` and blocks dependents.
An operator-[abandoned](native-rex.md#recovery-and-status-meanings) launch frees
its slot and also blocks dependents; replan it under a new ID.

After collection, each tick also [tidies](native-rex.md#tab-tidying) finished
tabs in every task of its state directory, keeping their final status and output
in the task record. `metrics.tabs_closed` counts closed tabs. A tidy failure is
listed under `tidy_issues`, never blocks work, and is retried on the next tick. New tasks share initial
starts by existing assignment count, with plan order breaking ties. This is a
simple finite-plan fairness rule, not a learned priority model.

Correction/investigation attempts and read-only QA, code-review, investigator,
judge and standby-reasoner roles can use the reserved worker slots. Ordinary work
cannot consume that reserve. Active readers and writers never share a checkout
with a new writer; unknown command adapters are conservatively treated as writers.
Checkout exclusion compares Git worktree identities, including subdirectory
assignments. Verified regular files with shared hard links are materialized as
independent copies before execution and after native work; source bytes and
modes still have to match the candidate. Other aliases remain unchanged.
Writers still require isolated worktrees. Worktree creation remains explicit;
candidate handoff can use the opt-in source validation above.

A runner serves one factory line, the line of its plan's tasks; a plan that mixes
lines refuses. It holds that line's execution lock (`.execution.lock` for `default`,
`.execution.<line>.lock` otherwise) for each tick, never while it polls, and it observes, collects,
counts against `workers`/`tasks` and tidies only that line's tasks; another line's
records and running agents never block it. An unreadable record names no line, so it
is reported in `issues` without blocking. Other controllers and manual launch
commands of the same line refuse only while a tick owns execution, so many plans can
share one line and its commands run between ticks. They exit 75 (`EX_TEMPFAIL`) for that refusal, so a
caller retries instead of treating it as a failure. A runner's first tick refuses a busy
line the same way; a later tick another holder has is skipped, so the budget holds. Status,
capture, result collection, admission, input selection and pause/resume remain
available under their task locks. After the controller exits, manual launches
retain their explicit-launch behavior; a later runner counts them.

Codex capacity is a separate admission gate. A Codex launch needs a fresh native
subscription observation, ordinary usage permission, the selected model/effort,
known pool identity/primary headroom, clear spend-control status and headroom
above the configured minimum in every reported window. Null secondary windows
remain unknown, and are never converted to additional allowance. With no verified
per-model pool mapping, every observed pool must clear the gate. Metadata alone
is not an eligibility test; the operator still selects the eligible route/model.

The safe snapshot is reused for at most 60 seconds and refreshed only when a new
launch could fit. Each probe is bounded to 30 seconds. Transient probe failures
use the coordination policy's `max_operational_retries`, spaced by the freshness
interval; exhaustion retains pending work until an explicit restart after repair.
Malformed capacity refuses further refresh in that invocation. Percent headroom
and worker slots are configured reserves, not token reservations or a guarantee
about unknown quotas or consumption outside this instance.

Ordinary tests use fake Rex/native processes and fixed observations. Live
conformance is invoked separately. The [implementation inventory](implementation.md)
owns current verification status and the remaining full delivery work.
