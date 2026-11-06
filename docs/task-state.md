# Task admission and state

`factory.py` owns one JSON record per task, protected by the existing task lock.
Admission and inspection need no Rex connection or provider account:

```sh
python3 factory.py admit --task example --goal 'Implement the agreed behavior' \
  --repo '/absolute/path/to/checkout' --acceptance 'docs/requirements.md#acceptance'
python3 factory.py show --task example
python3 factory.py pause --task example --reason 'Waiting for a scope decision'
python3 factory.py resume --task example
```

Repeat `--acceptance` for multiple original evidence references. Admission captures
the supplied repository's current Git commit as `initial_candidate`. This is an
admission snapshot, not review evidence or a live observation of the current head.
The repository must have a commit. References are operator-supplied; their content
and engineering adequacy are not assessed by this command.

Limits default to `config/coordination.json`; `--corrections` and `--investigations`
override them at admission. Zero is valid. Repeating identical admission inputs
returns the existing record, preserving the initial candidate if Git HEAD advances.
Changed inputs are refused; admission never resets usage. An existing task with
unclassified launches cannot acquire guessed budgets.
Use a new task ID to admit explicitly scoped work.

## Launches and holds

For an admitted task, add `--purpose work`, `--purpose correction` or
`--purpose investigation` to the [start command](native-rex.md). The investigator
role requires the investigation purpose, and that purpose requires the investigator.
The operator declares the purpose; the launcher does not classify whether work is
a correction. Ordinary work has no attempt count limit in this slice.

Usage is derived from the recorded assignments. A new correction or investigation
reserves one attempt when its launch intent is saved, before the external command.
Failed or uncertain launches retain that reservation. Reconnection to the same
assignment consumes nothing further. New assignment IDs, worker changes, restarts
and pause/resume do not restore attempts. Correction exhaustion names the
investigator; investigation exhaustion names the operator. These are diagnostics,
not automatic dispatch or a substitute for investigation -> Jev -> judge.

Pause blocks new launches for that task. It does not interrupt existing processes.
Inspection, capture and reconciliation remain available while paused, including
repeating `start` for an existing assignment. Resume removes only the hold.

`show` generates a snapshot and allowance arithmetic from the task record. It does
not query live processes, quota, results or acceptance. Without admission, allowances
are `null`, not unlimited or zero. Task state can contain command arguments and
briefs; keep credentials out of those inputs and keep the state directory private.

## Schema and ownership

The runtime owner is `factory.py`; all CLI readers and writers use its loader under
the task lock. External consumers must not edit or migrate these records. The
runtime's `new_task`, `admit`, `launch` and `record_result` functions define schema version 2:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `2` on every task record |
| `task` | Task slug matching the JSON filename |
| `admission` | `null`, or goal, repository, initial candidate, acceptance references and correction/investigation limits |
| `inputs` | `null` without admission; otherwise the selected candidate commit and requirements revision |
| `hold` | `null`, or the operator's pause reason |
| `rex` | `null` before connection, otherwise pinned binary, endpoint and host ID |
| `session_label`, `session_id` | Unique reconciliation marker and nullable returned Rex identity |
| `assignments` | Assignment IDs mapped to immutable request inputs, launch state, brief path, unique window marker and returned pane IDs |

Assignment requests retain role, goal, working directory, command and instruction
hash. New admitted assignments also contain the declared purpose, an input
snapshot and any declared dependencies. Each assignment has a nullable `result`.
States are
`launching` or `launched`, describing the effect, not completion of engineering work.
Allowance totals are generated output; no second usage ledger is stored.

The original unversioned launcher record is legacy version 0. Version 1 added
admission and holds. The owner migrates either on read, preserving requests,
launch intents, limits, holds and IDs. It adds `result: null` to old assignments,
without inventing revision-bound evidence. Old assignments can reconnect but
cannot acquire results retroactively. Admitted version 1 tasks initialize current
inputs from their recorded initial candidate. `show` can write a legacy format
upgrade, but never contacts Rex.

This is a gate store under the coding policy's Stateful Artifacts migration
exception: an unsupported version or damaged gate data refuses the action with a
diagnostic. It must never be treated as an empty task permitting a fresh launch.
Newer schemas require an updated runtime. Keep existing state for recovery;
deleting it is not reconciliation. Missing records allow first launch or admission;
inspection and pause/resume of a missing task fail.

Resource reservations, automatic dependency scheduling and cross-task capacity
enforcement remain future work. No acceptance or release is implied by admission,
a saved intent, a reported result or process exit.

## Results and dependencies

Select the inputs before launching a new assignment:

```sh
python3 factory.py inputs --task example --candidate HEAD --requirements requirements-v1
python3 factory.py result --task example --id qa --candidate '<full-commit-sha>' \
  --requirements requirements-v1 --outcome approved --artifact 'evidence/qa.md'
```

Candidate references resolve to existing commits in the admitted repository.
Requirements revisions are explicit nonempty identifiers; the operator must
change the identifier when the relevant requirements change. Admission defaults
the identifier to its initial Git commit. Set it explicitly before launch for
requirements versioned elsewhere. The runtime does not monitor external documents.

Each new admitted launch puts the selected inputs into its request and role brief.
Results report one of `completed`, `approved`, `findings` or `blocked`, plus at
least one original evidence reference. These are specialist reports; registration
does not establish their correctness or satisfy the project's release gates.
The requirements revision must match the assignment. Reviews and findings must
report the candidate they assessed. `completed` may report a produced commit,
allowing implementation to return a new candidate for independent review.

Repeating the same result is safe; conflicting reports require a new assignment.
Unknown launches must be reconciled before recording a result. Changing task
inputs retains prior reports as history. `show` generates `result_current` by
comparing the reported candidate and requirements revision with current inputs.
A late report for previous inputs remains historical. Reconnecting preserves the
original assignment's inputs and never rebinds its evidence to the current task.

Add `--after earlier-id` before the start command's `--` delimiter for each required
dependency. Dependencies must name existing assignments in the same task. New
launches require each dependency to have a current `completed` or `approved`
result. Findings, blocked or stale reports cannot advance work. Referencing only
earlier assignments keeps this launch graph acyclic without a planner. These are
launch guards; no background scheduler or automatic acceptance is implemented.
