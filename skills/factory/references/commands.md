# Command contracts

`factory.py` is `python3 -B "$SKILL_DIR/runtime/factory.py"`, and `ready_runner.py` is `python3 -B "$SKILL_DIR/runtime/ready_runner.py"`. Both read and write task state in `~/.local/state/software-factory` (`--state-dir` overrides it).

Every command follows the same contract:

- **Success:** exit 0 and one JSON document on stdout. `capture` prints the pane's text instead.
- **Refusal or failure:** exit 1, and stderr carries `factory: <reason>` naming what to inspect or do next. A refused command changes nothing unless its message says what it kept.
- **Busy line:** exit 75 when another runner or launch holds the line's execution lock. Retry later; it isn't a failure.

| Command | Input | Output on success |
|---|---|---|
| `admit` | `--task --line --goal --repo --acceptance`, optional `--corrections --investigations` | The task view: `task`, `line`, `admission`, `inputs`, `assignments`, `allowances`, `hold` |
| `inputs` | `--task --candidate COMMIT --requirements ID` | The task view with the new `inputs` |
| `provision` | `--task --id WORKTREE_ID --path /absolute/new/path` | The task view with `provisioning` |
| `show`, `pause --reason`, `resume` | `--task` | The task view |
| `line` | `--task --line` | The task view on its new line |
| `extend-allowance` | `--task --purpose --additional --expected-limit --request-id --reason` | The task view with the new allowance |
| `codex-start`, `claude-start`, `grok-start`, `antigravity-start` | `--task --id --role --purpose --goal --cwd`, the harness binary, `--model --effort`; writers add `--write --commit-message` | The task view with the launched assignment |
| `status` | `--task --id` | `{role, state, session_id, window_id, block_id, exit}`, where `state` is `running`, `exited`, `failed`, `lost`, `unknown` or `abandoned`, plus `reason` when lost |
| `capture` | `--task --id` | The pane's text |
| `codex-collect`, `claude-collect`, `grok-collect`, `antigravity-collect` | `--task --id` | The task view with the assignment's `result`: `{inputs, candidate, outcome, artifacts}`. Exit 1 if the result was saved but its tab wasn't tidied: run `tidy` |
| `tidy` | optional `--task` | `{TASK: {closed, running, session_closed}}`; exit 1 if any task failed |
| `cancel` | `--task --id` | `{cancelled, interrupted}`; the worker records the attempt failed |
| `abandon` | `--task --id --reason` | The task view with the launch abandoned |
| `reset` | `--task --id` | `{task, assignment, checkout, candidate, patch, next}` |
| `rebase-worktree` | `--task --onto COMMIT` | `{task, worktree, previous, candidate, patch, next}` |
| `release-worktree` | `--task` | `{task, worktree, branch, head}` |
| `integrate`, `delivered` | `--task --id --pr --review --after ... --gh --release-scripts` | The delivery receipt; exit 0 only when it is launched or delivered |
| `ready_runner.py` | `--plan --codex --rex --server`, optional harness binaries, `--from-writer`, `--seconds`, `--once` | `{schema_version, result, jobs, active_workers, issues, tidy_issues, metrics, capacity, capacity_error, elapsed_seconds}`, where `result` is `complete`, `needs_action`, `pending` or `pending_at_budget` and each job is `{task, id, state, reason}`; exit 0 only when `complete` |
