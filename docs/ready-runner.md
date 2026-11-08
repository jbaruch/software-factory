# Explicit ready-work runner

`ready_runner.py` executes an operator-selected Codex plan. It observes existing
Rex jobs, collects successful native receipts, then launches ready assignments.
It uses the existing task store and its cumulative attempt limits. It does not
select models, interpret findings, accept tasks or release changes. The separate
[integration command](integration.md) owns mechanical delivery; the runner only
observes its existing pending/terminal assignment and never starts or retries it.

Admit tasks and select their exact candidate/requirements through
[the task owner](task-state.md). Keep one state directory per factory instance;
the runner counts every recorded assignment in that directory, including jobs
outside the supplied plan. Other instances and native CLI use outside this
factory are not part of its work limits.

```sh
python3 ready_runner.py --state-dir '/absolute/path/to/state' \
  --plan '/absolute/path/to/plan.json' \
  --codex '/absolute/path/to/codex' \
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

Every job supplies all fields shown here. Replace the candidate and model with
the actual selected commit and discovered eligible model ID:

```json
{
  "schema_version": 1,
  "limits": {
    "workers": 2,
    "tasks": 4,
    "reserve_workers": 1,
    "minimum_remaining_percent": 15
  },
  "jobs": [
    {
      "task": "example",
      "id": "qa",
      "role": "qa",
      "goal": "Verify the selected candidate against the admitted acceptance references",
      "cwd": "/absolute/path/to/clean/worktree",
      "candidate": "<full-commit-sha>",
      "requirements": "requirements-v1",
      "model": "<discovered-eligible-model-id>",
      "effort": "high",
      "seconds": 180,
      "purpose": "work",
      "write": false,
      "commit_message": null,
      "after": []
    }
  ]
}
```

Set `write` to true and supply an explicit `commit_message` to authorize the
factory-owned local commit after a successful writer report. This uses the
[approved writer contract](native-providers.md#explicit-codex-assignments),
including configured Git helpers. Read-only jobs use `commit_message: null`.
Changing the message changes the immutable assignment request.
After a writer creates a commit, explicitly select that produced candidate and
submit a verification phase, or use the opt-in handoff below. The original writer
plan then reports stale.

Jobs have distinct `(task, id)` pairs. `after` names earlier jobs in this plan or
existing assignments in the same task. A dependency needs a successful current
report and an observed successful process exit. Findings, failures, stale reports
and changed requests do not advance it. One plan uses one candidate/requirements
pair per task. A later phase can depend on an existing implementation's produced
commit after explicit input selection, without relabeling the original request.

## Opt-in writer candidate handoff

For an explicitly authorized independent verification phase, add
`--from-writer ASSIGNMENT_ID` to the command above. Keep the verification plan's
candidate/requirements at the writer's **original frozen input pair**; no manual
copy of the output SHA is needed. Ready-plan schema 1 and the task schema stay
unchanged.

The plan must contain jobs for exactly one admitted task. Every job must be
read-only `qa`, `code-review` or `test-automation`, use an ID distinct from the
writer, and name that existing writer explicitly in `after`. Additional explicit
dependencies retain their normal gates. The source must already have a collected
`completed` native Codex result and an explicit factory commit. The runner
revalidates its original receipt/events, commit and repository/worktree binding,
observes its successful terminal Rex process, and compares the saved result with
that evidence. A manually recorded completion alone is insufficient. Collect the
writer through the existing runner or `factory.py codex-collect` first.

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

## Limits and reservations

Worker slots and active task slots are reserved from observed processes, including
unknown and unconfirmed launches. A terminal process releases its slot even if
its report is unusable; that report still needs action. A process `lost` to a
Rex server restart is terminal: the runner collects a native Codex result from
its completed receipt, and otherwise reports `blocked` and blocks dependents. New tasks share initial
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

The runner holds the owner's `.execution.lock` during its invocation. Other
controllers and manual launch commands refuse while it owns execution. Status,
capture, result collection, admission, input selection and pause/resume remain
available under their task locks. After the controller exits, manual launches
retain their explicit-launch behavior; a later runner counts them.

Capacity is a separate admission gate. A ready launch needs a fresh native
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
