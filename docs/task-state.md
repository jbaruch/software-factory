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

For an operator-approved addition to an existing task, use:

```sh
python3 factory.py extend-allowance --task example --purpose correction \
  --additional 1 --expected-limit 1 --request-id approved-correction-one \
  --reason 'Operator approved one additional correction for the existing scope'
```

This command records an approval already obtained; a worker or classifier cannot
grant itself more attempts. It adds a positive number of correction or
investigation attempts to the current limit. The supplied expected limit must
match. Original admission, consumed attempts, selected revisions, reports and holds
stay unchanged. It neither retries a failed assignment nor starts a worker.
Repeat the same request ID and inputs after a lost reply; the replay adds nothing,
even after subsequent decisions. A reused ID with changed inputs or a stale
expected limit refuses. Changed scope still needs a new explicit admission.

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
Each pause/resume records one control request in the task itself. Supply
`--request-id ID` to retry the same decision safely, including after a lost reply
or an intervening decision. Reusing an ID with different inputs refuses. Optional
`--expected-revision N` compares against the current number of control events and
refuses stale decisions. The dashboard supplies both values automatically.

`show` generates a snapshot and allowance arithmetic from the task record. It does
not query live processes, quota, results or acceptance. Without admission, allowances
are `null`, not unlimited or zero. Task state can contain command arguments and
briefs; keep credentials out of those inputs and keep the state directory private.

## Schema and ownership

The runtime owner is `factory.py`; all CLI readers and writers use its loader under
the task lock. Task records must be regular files; links, directories and FIFOs
refuse without resetting holds or history. External consumers must not edit or migrate these records. The
runtime's `new_task`, `admit`, `launch`, `hold_task`, `extend_allowance`,
`provision_worktree` and `record_result` functions define schema version 8:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `8` on every task record |
| `task` | Task slug matching the JSON filename |
| `admission` | `null`, or goal, repository, repository identity, initial candidate, acceptance references and correction/investigation limits |
| `inputs` | `null` without admission; otherwise the selected candidate commit and requirements revision |
| `hold` | `null`, or the operator's nonempty pause reason |
| `control_events` | Ordered pause/resume requests; each has a unique `id`, nullable integer `expected_revision`, `previous_hold`, resulting `hold`, and `at` in UTC epoch seconds |
| `provisioning` | `null`, or one immutable writer-worktree request, unique owner token, effect state and nullable returned Git directory |
| `allowance_events` | Ordered operator additions; each has a unique `id`, `purpose`, `previous_limit`, positive integer `additional`, nonempty `reason` and `at` in UTC epoch seconds |
| `rex` | `null` before connection, otherwise pinned binary, endpoint and host ID |
| `session_label`, `session_id` | Unique reconciliation marker and nullable returned Rex identity |
| `assignments` | Assignment IDs mapped to immutable request inputs, launch state, brief path, unique window marker and returned pane IDs |

Assignment requests retain role, goal, working directory, command and instruction
hash. New admitted assignments also contain the declared purpose, an input
snapshot and any declared dependencies. Each assignment has a nullable `result`.
States are
`launching` or `launched`, describing the effect, not completion of engineering work.
Allowance totals are generated output; no second usage ledger is stored.
Effective limits start at the immutable admission limits. Each allowance event's
`previous_limit` equals the effective limit for its correction/investigation
purpose at that position, then `additional` is added. Event IDs are unique within
allowance history; timestamps use the same finite year-9999 bound as hold events.
Malformed or missing history refuses without resetting it. Admission-free tasks
have an empty allowance history and unknown allowance totals.
Control revision is generated from the event count. A supplied expected revision
equals the event's position; `null` records a request without that precondition.
Hold values in an event are null or nonempty strings. Timestamps are finite,
nonnegative epoch seconds through year 9999; sequence comes from list order rather
than wall-clock order. Later events chain from the previous hold, and the final
event agrees with the current hold. Duplicate IDs or damaged history refuse reads.
An identical request replay returns the current record without another write,
even when a subsequent decision has changed the hold. These records trace control
effects; they do not interrupt workers or change scope, reports or allowances.

Schema 8 combines the earlier schema-6 provisioning owner and schema-7 allowance
owner. A schema-6 record retains its provisioning intent and gains only an empty
`allowance_events` list. A schema-7 record retains its allowance history and gains
only `provisioning: null`. Versions 0–5 gain both empty fields. Migration preserves
original admission, used attempts, control requests, holds, selected inputs and
assignment history; it grants no attempts and adopts no existing worktree.
Older-version records carrying fields their owner did not support refuse without
rewriting. Missing required fields and unsupported future versions also refuse.

The original unversioned launcher record is legacy version 0. Version 1 added
admission and holds. The owner migrates either on read, preserving requests,
launch intents, limits, holds and IDs. It adds `result: null` to old assignments,
without inventing revision-bound evidence. Old assignments can reconnect but
cannot acquire results retroactively. Admitted version 1 tasks initialize current
inputs from their recorded initial candidate. `show` can write a legacy format
upgrade, but never contacts Rex. Version 2 added revision-bound inputs/results.
Version 3 used a Git common-directory path plus filesystem device/inode. Those
numbers can be reused after deletion and cannot establish admission continuity.
Version 4 replaces that active pin with `{common_dir, repository_id}`. The path is
absolute; the ID is a lowercase 32-character UUID hex string. Linked worktrees
share the Git-local marker described below; ordinary clones do not inherit it.
Version 5 adds `control_events`. The owner migrates versions 0–4 on read,
preserving their holds/history and adding an empty event list. It never invents
historical control requests. Existing holds without events remain valid. Version-4
consumers must update before using version-5 task files; they refuse newer gates.
Version 6 introduced nullable `provisioning` in the retained prototype. Version 7
introduced operator allowance additions on main without interpreting that
prototype's records. Both formats now migrate through the schema-8 contract above.
Older readers must update before opening schema-8 task files.
Before repository-backed mutations the owner verifies this identity and the
initial commit. Revision reads verify the marker before and after Git resolves the commit. Launch
passes the saved identity through checkout validation; `checkout_exec.py` checks
the selected candidate and marker in Rex's actual working directory before
executing any new admitted command. A refusal leaves the saved attempt available
for inspection; it never retries the command. Existing panes reconnect unchanged.

Older admissions upgrade with `repository_identity: null`, preserving scope,
holds, attempts and reports without inventing a historical pin from today's path.
Version 3's nonnull filesystem pin is retained as `legacy_repository_identity`
with its original `common_dir`, integer `device` (nonnegative) and `inode` (positive).
This optional field is inspection-only. Old tasks remain inspectable and
reconnectable, but refuse new repository-backed work. The operator must inspect
the original history and authorize a new admission; migration never resets the
old task or automatically carries its approvals forward.
Legacy records containing newer-version fields refuse migration. The loader validates
request identity and evidence references before using saved results as dependencies.
Recorded Rex identity without assignment history also refuses action. Live
connections verify the saved session label before reusing its ID.
They also match every retained launched window marker and window/block ID to
that inspected session. Duplicate JSON keys or pane identities refuse loading;
pending launches cannot have pane IDs. The inspected session must contain exactly
the recorded assignment windows, allowing a pending window to be absent before
reconciliation. Malformed Rex shapes require API compatibility inspection.
Assignment-map order preserves launch order; dependencies
must reference earlier entries. Task inputs are null only without admission, and
the investigator role and investigation purpose remain paired on reads.
Unadmitted assignments cannot carry admitted-only purpose, input or dependency
fields. New brief filenames separate task and assignment IDs with a dot; existing
records keep their original paths and frozen contents.
Create responses must match the
live session marker and pane before the launcher saves `launched`; mismatches
retain the original pending intent for reconciliation without another effect.
Session markers must match `factory-<task>-<8hex>`; window markers must match
`<role>-<assignment-id>-<8hex>`. A complete pane tuple cannot be reassigned to
another owner by swapping saved fields. Admission reads only needed defaults;
both explicit limit flags work without the defaults file.

This is a gate store under the coding policy's Stateful Artifacts migration
exception: an unsupported version or damaged gate data refuses the action with a
diagnostic. It must never be treated as an empty task permitting a fresh launch.
Newer schemas require an updated runtime. Keep existing state for recovery;
deleting it is not reconciliation. Missing records allow first launch or admission;
inspection and pause/resume of a missing task fail.

The [ready-work runner](ready-runner.md) uses these existing intents/results and
serializes launch decisions with the owner's `.execution.lock`. It adds no task
fields or migrations. No acceptance or release is implied by admission,
a saved intent, a reported result or process exit.

## Writer worktree provisioning

After admission and explicit input selection, provision one named writer checkout:

```sh
python3 factory.py --state-dir '/absolute/path/to/private-state' provision \
  --task example --id backend --path '/absolute/path/to/new-writer'
python3 factory.py --state-dir '/absolute/path/to/private-state' show --task example
```

The destination's parent must exist. The destination must be absent and outside
existing checkouts, Git directories and the task store. The branch is
`factory/<task>/<id>` and must not already exist. The API is
`provision_worktree(task_record_path, worktree_id=..., destination=...)`; callers
hold the existing execution and task locks, as the CLI does. There is one
provisioning request per task in this slice; it does not consume or restore
specialist correction/investigation attempts.

Before Git mutation, the owner atomically saves `provisioning` with `id`,
`request: {path, branch, inputs}`, a unique `owner_token`, `state: provisioning`
and `git_dir: null`. Admission supplies the immutable repository identity. Paths
are absolute with a resolved existing parent. A paused task or pending launch
blocks new provisioning. Missing legacy repository identity refuses action.
Git creates a locked worktree without checkout; its lock reason includes this
task, worktree ID and owner token. The owner writes the new index and independent
regular files directly from raw blobs, preserves executable modes, verifies the
selected candidate and repository, then saves `state: provisioned` and `git_dir`.
No checkout hooks, clean/smudge filters or provider calls run. Existing source
checkouts may be dirty and remain unchanged.

Tracked symlinks, submodules, partial clones, object alternates, sparse checkout,
executable fsmonitor, redirected worktree configuration and per-worktree config
are unsupported and visibly refuse. Working-tree transformations such as CRLF
conversion are bypassed: the new checkout contains the candidate's raw bytes.

Repeat the exact command after a restart or lost reply. A completed request
returns the same worktree after verifying its ownership, without writing state
or resetting subsequent writer edits. A pending request can reconcile only when
the uniquely locked worktree is already complete and clean; this read-only
inspection is permitted while held. Missing, partial, dirty or conflicting
outcomes remain pending for inspection, without retry, reset, deletion or repair.
A lock marker, branch, Git directory or repository identity mismatch refuses.
Changed name, path, candidate or requirements revision cannot rebind the saved
request. `show` and the existing operator view expose the saved provisioning
state and generated `inputs_current`; they do not observe live checkout health.

Provisioning does not launch a writer, select new task inputs, hand off a produced
candidate, accept a task or grant merge/release authority. Select the provisioned
path explicitly in the existing assignment command.

## Repository marker

`factory.py` also owns `software-factory-repository.json` in the Git common
directory. This immutable record has exactly `schema_version: 1` and
`repository_id` (lowercase 32-character UUID hex). New explicit admissions create
it once with private file permissions and atomic no-replace publication; concurrent
admissions read the winning marker. Reads require an owner-private regular file
with one hard link and never follow marker symlinks. Task records and worker checks read it but
never repair, rotate or create it. It is identity data, not a task/approval ledger.
Git does not include it in commits, so normal clones and forks acquire their own
marker only on their own admission. Backups of the original Git directory must
preserve it; copying that entire directory also copies its identity.

Missing, damaged, duplicate-key or unsupported markers refuse repository-backed
work. Restore the original marker/access, or inspect the retained task history and
have the operator authorize a new admission. Never replace a missing marker to
repair an existing task's pin. There is no inferred migration from filesystem
identity and no automatic marker-version migration. Unsupported versions require
an updated owner. This record is a gate store under the same Stateful Artifacts
exception as task state; readers must refuse unknown/damaged records.

## Results and dependencies

Select the inputs before launching a new assignment:

```sh
python3 factory.py inputs --task example --candidate HEAD --requirements requirements-v1
python3 factory.py result --task example --id qa --candidate '<full-commit-sha>' \
  --requirements requirements-v1 --outcome approved --artifact 'evidence/qa.md'
```

Candidate references resolve to existing commits in the admitted repository.
Each new admitted launch checks that its working directory belongs to that Git
repository, is checked out at the selected candidate and has no uncommitted inputs.
This includes ignored files and directories: use a clean isolated worktree and
keep runtime state, caches and output outside that checkout. Verification clears
ambient Git overrides (preserving standard author/committer identity) so repository/worktree/index/object/config environment
settings cannot redirect the checks to another source. Verification uses absolute
Git executables from the operating system's default executable path, ignoring
inherited `PATH`. The Python exec guard disables environment and user-site startup
loading; the eventual command retains its environment. The exec consumer removes
direct routing variables from the launched command too. It preserves inherited
Git configuration and checks its keys through Git before execution. Inherited
source-routing options or damaged configuration refuse
launch; SSH, credential helpers, TLS trust and terminal prompting remain available.
Git reads bind the worktree to the actual checkout root; redirecting `core.worktree` settings and hidden index flags
(`assume-unchanged`/`skip-worktree`/`fsmonitor-valid`) refuse execution. Disable
fsmonitor for the assignment checkout. This runtime refuses repositories containing
submodules; recursive submodule input validation is not implemented.
Tracked file bytes and executable modes must match the raw candidate tree;
index timestamps are only hints. Working-tree filters that change those bytes
require a clean checkout without those transformations for this runtime.
Verification disables replacement objects and the untracked cache, so neither
can substitute the selected source or hide an added input.
The launch wrapper also receives the worktree's absolute Git directory: linked
worktrees share repository identity but cannot substitute for the assigned checkout.
Child commands force `GIT_NO_REPLACE_OBJECTS=1`, keeping ordinary Git reads bound
to the same unreplaced source used by verification.
Filesystem enumeration permits only candidate files and their implied directories,
excluding the root Git metadata entry. Empty untracked directories refuse launch
even when Git status reports a clean checkout.
This runtime requires independent regular source files. Tracked symlinks refuse
launch. Shared hard links are copied into independent regular files before launch,
without changing their aliases. Source bytes and modes must still match the
candidate. Materialize symlinked inputs into the assigned
checkout before admitting work that needs them.
Linked worktrees are supported. Reconnection does not restart or rebind existing work.
Requirements revisions are explicit nonempty identifiers; the operator must
change the identifier when the relevant requirements change. Admission defaults
the identifier to its initial Git commit. Set it explicitly before launch for
requirements versioned elsewhere. The runtime does not monitor external documents.

Each new admitted launch puts the selected inputs into its request and role brief.
The brief also includes the admitted task goal and original acceptance references.
Reconnection uses the existing instruction hash and brief; a role/template change
does not rewrite an already-started assignment's context.
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
