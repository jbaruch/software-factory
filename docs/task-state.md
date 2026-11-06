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
runtime's `new_task`, `admit` and `launch` functions define schema version 1:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1` on every task record |
| `task` | Task slug matching the JSON filename |
| `admission` | `null`, or goal, repository, initial candidate, acceptance references and correction/investigation limits |
| `hold` | `null`, or the operator's pause reason |
| `rex` | `null` before connection, otherwise pinned binary, endpoint and host ID |
| `session_label`, `session_id` | Unique reconciliation marker and nullable returned Rex identity |
| `assignments` | Assignment IDs mapped to immutable request inputs, launch state, brief path, unique window marker and returned pane IDs |

Assignment requests retain role, goal, working directory, command and instruction
hash. Admitted assignments also contain the declared purpose. States are
`launching` or `launched`, describing the effect, not completion of engineering work.
Allowance totals are generated output; no second usage ledger is stored.

The original unversioned launcher record is legacy version 0. The owner migrates
it on read by adding version 1 with `admission: null` and `hold: null`, preserving
requests, launch intents and IDs. It invents no historical purposes or limits.
`show` can therefore write a legacy format upgrade, but never contacts Rex.

This is a gate store under the coding policy's Stateful Artifacts migration
exception: an unsupported version or damaged gate data refuses the action with a
diagnostic. It must never be treated as an empty task permitting a fresh launch.
Newer schemas require an updated runtime. Keep existing state for recovery;
deleting it is not reconciliation. Missing records allow first launch or admission;
inspection and pause/resume of a missing task fail.

Dependency scheduling, result/revision transitions, resource reservations and
cross-task capacity enforcement remain future work. No acceptance or release is
implied by admission, a saved intent or process exit.
