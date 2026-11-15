# Command reference

`factory.py` is `python3 -B "$SKILL_DIR/runtime/factory.py"` and `ready_runner.py` is `python3 -B "$SKILL_DIR/runtime/ready_runner.py"`. Their parsers, in each script's `main()`, are the source of truth for flags: `factory.py COMMAND --help` lists a command's flags, required ones unbracketed. `--state-dir`, `--rex` and `--server` go before the command.

## Exit codes

- **0:** success; stdout is one JSON document, except for `--help`, which prints usage text.
- **1, stderr only:** refused, with `factory: REASON` (`ready-runner: REASON` from the runner) naming what to do next.
- **1, JSON on stdout:** a non-success result from `tidy` or `ready_runner.py`, as the table says.
- **1, JSON on stdout and `factory: REASON` on stderr:** `*-collect` saved the result but couldn't close the tab.
- **2:** the command line doesn't parse; argparse prints usage and the error on stderr.
- **75:** the task's line is busy, because another launcher or runner holds its execution lock; stderr says so as `factory: REASON` (`ready-runner: REASON`). Retry later.

A command that fails may already have written part of its effect. Check with `show` or `status` before running it again.

## Commands

Sources are in `skills/factory/runtime/factory.py` unless the row names another file.

| Command | Effect | Prints | Source |
|---|---|---|---|
| `admit` | Records the task's goal, acceptance, allowances and line, and selects the repository's HEAD as its first inputs | Task view | `admit()` |
| `line` | Moves a task with nothing open to another line | Task view | `move_line()` |
| `inputs` | Selects the task's candidate and requirements revision, capturing its GitHub acceptance issues | Task view | `set_inputs()` |
| `extend-allowance` | Raises a correction or investigation allowance; run only with the operator's approval | Task view | `extend_allowance()` |
| `pause`, `resume` | Holds or releases new launches | Task view | `hold_task()` |
| `show` | None | Task view | `task_view()` |
| `provision` | Creates the task's locked worktree on branch `factory/TASK/ID` | Task view | `provision_worktree()` |
| `result` | Records a non-native assignment's result | Task view | `record_result()` |
| `start`, `codex-start`, `claude-start`, `grok-start`, `antigravity-start` | Writes the brief, records the assignment and opens its Rex tab; an existing ID reconnects | Status object | `launch()` |
| `status` | Reconciles a pending launch | Status object | `status()` |
| `capture` | None | `{task, id, text}`, the pane's output | `captured()` |
| `codex-collect`, `claude-collect`, `grok-collect`, `antigravity-collect` | Records the result from the worker's receipt and closes its tab | Task view; exit 1 with it when the tab didn't close (run `tidy`) | `collector()` |
| `tidy` | Saves each finished tab's output, when it has any (a lost attempt has none), and closes it | `{TASK: {closed, running, session_closed}}`, or `{TASK: {error}}`; exit 1 when any task failed | `tidy_tasks()` |
| `cancel` | Sends Ctrl-C to the pane; the worker records the attempt failed | `{cancelled, interrupted, next}` | `cancel_assignment()` |
| `abandon` | Marks a launch that never made a pane abandoned | Task view | `abandon_launch()` |
| `reset` | Saves a failed writer's leftover edits as a patch and restores its checkout | `{task, assignment, checkout, candidate, patch, next}` | `reset_writer()` |
| `rebase-worktree` | Saves the change as a patch, moves the worktree onto a new base and selects it | `{task, worktree, previous, candidate, patch, next}` | `rebase_worktree()` |
| `release-worktree` | Removes the provisioned worktree; the branch stays | `{task, worktree, branch, head}` | `release_worktree()` |
| `delivered` | Confirms the release skill merged the head the release judgment approved, and records its receipt | The receipt; `--acceptance` adds `acceptance` | `skills/factory/runtime/integration.py` `delivered()` |
| `ready_runner.py` | Launches, collects and tidies a plan's jobs on its line | `{schema_version, result, jobs, …}`; exit 1 unless `result` is `complete` | `skills/factory/runtime/ready_runner.py` `run()` |

- **Task view:** the task record (`task`, `line`, `admission`, `inputs`, `assignments`, `hold`, `provisioning` and its event logs), plus `allowances` and each assignment's `result_current`.
- **Status object:** `{role, state, session_id, window_id, block_id, exit}`, plus `reason` for a lost or abandoned attempt and `closed: true` once tidied.
