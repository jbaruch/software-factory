# Command contracts

`factory.py` is `python3 -B "$SKILL_DIR/runtime/factory.py"` (`main` and `parser` setup in `runtime/factory.py`). `ready_runner.py` is `python3 -B "$SKILL_DIR/runtime/ready_runner.py"` (`main` in `runtime/ready_runner.py`). Both keep task state in `~/.local/state/software-factory`, and `--state-dir` overrides it.

Exit codes:

- **0:** the command succeeded, and its output is on stdout (one JSON document; `capture` prints text).
- **1 with a structured stdout:** a non-success result the table names, such as `tidy` with a failed task or a runner that hasn't completed.
- **1 with stderr only:** a refusal. stderr carries `factory: <reason>` (`ready-runner: <reason>` from the runner) naming what to inspect or do next, and nothing changed unless the reason says what was kept.
- **2:** a malformed command line (argparse).
- **75:** the line's execution lock is busy. Retry later; it isn't a failure.

The **task view** is the task record with its `allowances` and each assignment's `result_current` added (`task_view`). The **status object** is `{role, state, session_id, window_id, block_id, exit}`, where `state` is `running`, `exited`, `failed`, `lost`, `unknown` or `abandoned`, plus `reason` when lost (`status`).

| Command | Required input | Optional input | Success output | Other outcomes | Source |
|---|---|---|---|---|---|
| `admit` | `--task --line --goal --repo --acceptance` | `--corrections --investigations` | Task view | | `admit` |
| `inputs` | `--task --candidate --requirements` | | Task view | | `set_inputs` |
| `provision` | `--task --id --path` | | Task view | | `provision_worktree` |
| `show` | `--task` | | Task view | | `load_task` |
| `pause` | `--task --reason` | `--request-id --expected-revision` | Task view | | `hold_task` |
| `resume` | `--task` | `--request-id --expected-revision` | Task view | | `hold_task` |
| `line` | `--task --line` | | Task view | | `move_line` |
| `extend-allowance` | `--task --purpose --additional --expected-limit --request-id --reason` | | Task view | | `extend_allowance` |
| `result` | `--task --id --candidate --requirements --outcome --artifact` | | Task view | | `record_result` |
| `start` | `--task --id --role --goal --cwd`, then `-- COMMAND...` | `--purpose --after` | Status object of the launched pane | | `launch`, `status` |
| `codex-start` | `--task --id --role --goal --cwd --codex --model --effort` | `--purpose --after --write --commit-message --seconds --release-pr --gh --release-scripts --delivery --decision-store --decision --question` | Status object | | `codex_command`, `launch` |
| `claude-start` | `--task --id --role --goal --cwd --claude --model --effort` | `--purpose --after --write --commit-message --sandbox --seconds --turns --base --release-pr --gh --release-scripts --delivery --decision-store --decision --question` | Status object | | `claude_command`, `launch` |
| `grok-start` | `--task --id --role --goal --cwd --grok --model --effort` | `--purpose --after --write --commit-message --sandbox --seconds --turns --base --release-pr --gh --release-scripts --delivery` | Status object | | `grok_command`, `launch` |
| `antigravity-start` | `--task --id --role --goal --cwd --agy --model` | `--effort --purpose --after --write --commit-message --sandbox --seconds --base --release-pr --gh --release-scripts --delivery` | Status object | | `antigravity_command`, `launch` |
| `status` | `--task --id` | | Status object | | `status` |
| `capture` | `--task --id` | | The pane's text | | `captured` |
| `codex-collect`, `claude-collect`, `grok-collect`, `antigravity-collect` | `--task --id` | | Task view, the assignment's `result` being `{inputs, candidate, outcome, artifacts}` | Exit 1: the task view on stdout and `factory: Collected result saved, but its tab was not tidied; run tidy` on stderr | `collector` |
| `tidy` | | `--task` | `{TASK: {closed, running, session_closed}}` | Exit 1: the same report on stdout, a failed task as `{TASK: {error}}` | `tidy_tasks` |
| `cancel` | `--task --id` | | `{cancelled, interrupted}`; the worker records the attempt failed | | `cancel_assignment` |
| `abandon` | `--task --id --reason` | | Task view | | `abandon_launch` |
| `reset` | `--task --id` | | `{task, assignment, checkout, candidate, patch, next}` | | `reset_writer` |
| `rebase-worktree` | `--task --onto` | | `{task, worktree, previous, candidate, patch, next}` | | `rebase_worktree` |
| `release-worktree` | `--task` | | `{task, worktree, branch, head}` | | `release_worktree` |
| `integrate` | `--task --id --pr --review --after --gh --release-scripts` | `--acceptance` | The integration receipt, its `state` `launched` or `delivered` | Exit 1: the receipt on stdout in any other state | `integration.integrate` |
| `delivered` | `--task --pr --review --after --gh --release-scripts` | | The delivery confirmation | | `integration.delivered` |
| `ready_runner.py` | `--plan --codex --rex --server` | `--claude --grok --antigravity --from-writer --seconds --once` | `{schema_version, result: "complete", jobs, active_workers, issues, tidy_issues, metrics, capacity, capacity_error, elapsed_seconds}`, each job `{task, id, state, reason}` | Exit 1: the same document on stdout with `result` `needs_action`, `pending` or `pending_at_budget` | `run`, `Runner.tick` |
