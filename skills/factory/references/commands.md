# Command contracts

`factory.py` is `python3 -B "$SKILL_DIR/runtime/factory.py"`, whose command line and dispatch live in `main()` in `skills/factory/runtime/factory.py`. `ready_runner.py` is `python3 -B "$SKILL_DIR/runtime/ready_runner.py"`, with `main()` in `skills/factory/runtime/ready_runner.py`. Both keep task state in `~/.local/state/software-factory`, and `--state-dir` overrides it.

Exit codes:

- **0:** the command succeeded, and its output is on stdout (one JSON document; `capture` prints text).
- **1 with stdout:** a non-success result the table names, with the document on stdout.
- **1 with stderr only:** a refusal. stderr carries `factory: <reason>` (`ready-runner: <reason>` from the runner) naming what to inspect or do next. Nothing changed unless the reason says what was kept.
- **2:** a malformed command line (argparse).
- **75:** the line's execution lock is busy. Retry later; it isn't a failure.

**Task view** (`task_view()`): the task record (`task`, `line`, `admission`, `inputs`, `assignments`, `hold`, `provisioning` and its event logs), with `allowances` and each assignment's `result_current` added.

**Status object** (`status()`): `{role, state, session_id, window_id, block_id, exit}`. `state` is `running`, `exited`, `failed`, `lost`, `unknown` or `abandoned`. A lost or abandoned attempt adds `reason`. A tidied assignment adds `closed: true` and keeps the state it closed with.

Native starts (`codex-start`, `claude-start`, `grok-start`, `antigravity-start`) require `--purpose work|correction|investigation` once the task is admitted, and so does `start`. Writers add `--write --commit-message`, and outside Codex also `--sandbox /absolute/codex`.

Unless noted, a source is in `skills/factory/runtime/factory.py`.

| Command | Required input | Optional input | Side effects | Success output | Other outcomes | Source |
|---|---|---|---|---|---|---|
| `admit` | `--task --line --goal --repo --acceptance` | `--corrections --investigations` | Creates the task record and selects the repository's HEAD as its first inputs | Task view | | `admit()` |
| `inputs` | `--task --candidate --requirements` | | Selects the task's inputs | Task view | | `set_inputs()` |
| `provision` | `--task --id --path` | | Creates the locked worktree and branch `factory/TASK/ID` | Task view | | `provision_worktree()` |
| `show` | `--task` | | None | Task view | | `load_task()` |
| `pause` | `--task --reason` | `--request-id --expected-revision` | Holds new launches | Task view | | `hold_task()` |
| `resume` | `--task` | `--request-id --expected-revision` | Releases the hold | Task view | | `hold_task()` |
| `line` | `--task --line` | | Moves a task with nothing open, and its session label | Task view | | `move_line()` |
| `extend-allowance` | `--task --purpose --additional --expected-limit --request-id --reason` | | Raises the allowance; run only with the operator's approval | Task view | | `extend_allowance()` |
| `result` | `--task --id --candidate --requirements --outcome --artifact` | | Records a non-native assignment's result | Task view | | `record_result()` |
| `start` | `--task --id --role --goal --cwd`, then `-- COMMAND...` | `--purpose --after` | Writes the brief, records the assignment, opens its Rex tab | Status object | | `launch()` |
| `codex-start` | `--task --id --role --goal --cwd --codex --model --effort` | `--purpose --after --write --commit-message --seconds --release-pr --gh --release-scripts --delivery --decision-store --decision --question` | As `start`, running the Codex worker | Status object | | `codex_command()`, `launch()` |
| `claude-start` | `--task --id --role --goal --cwd --claude --model --effort` | `--purpose --after --write --commit-message --sandbox --seconds --turns --base --release-pr --gh --release-scripts --delivery --decision-store --decision --question` | As `start`, running the Claude worker | Status object | | `claude_command()`, `launch()` |
| `grok-start` | `--task --id --role --goal --cwd --grok --model --effort` | `--purpose --after --write --commit-message --sandbox --seconds --turns --base --release-pr --gh --release-scripts --delivery` | As `start`, running the Grok worker | Status object | | `grok_command()`, `launch()` |
| `antigravity-start` | `--task --id --role --goal --cwd --agy --model` | `--effort --purpose --after --write --commit-message --sandbox --seconds --base --release-pr --gh --release-scripts --delivery` | As `start`, running the Antigravity worker | Status object | | `antigravity_command()`, `launch()` |
| `status` | `--task --id` | | None | Status object | | `status()` |
| `capture` | `--task --id` | | None | The pane's text, live or retained | | `captured()` |
| `codex-collect`, `claude-collect`, `grok-collect`, `antigravity-collect` | `--task --id` | | Records the result from the worker's receipt, then closes its tab | Task view; the assignment's `result` is `{inputs, candidate, outcome, artifacts}` | Exit 1: the task view on stdout and `factory: Collected result saved, but its tab was not tidied; run tidy` on stderr | `collector()` |
| `tidy` | | `--task` | Saves each finished tab's output, closes it, and kills a line's session once it holds no tab | `{TASK: {closed, running, session_closed}}` | Exit 1: the same report on stdout, a failed task shown as `{TASK: {error}}` | `tidy_tasks()` |
| `cancel` | `--task --id` | | Sends ctrl+c; the worker records the attempt failed | `{cancelled, interrupted, next}`, `interrupted` being the status object | | `cancel_assignment()` |
| `abandon` | `--task --id --reason` | | Marks a launch that never made a pane abandoned | Task view | | `abandon_launch()` |
| `reset` | `--task --id` | | Saves leftover edits as `RECEIPT.reset.patch` and restores the checkout | `{task, assignment, checkout, candidate, patch, next}` | | `reset_writer()` |
| `rebase-worktree` | `--task --onto` | | Saves the change as a patch, moves the worktree onto the base, selects it as inputs | `{task, worktree, previous, candidate, patch, next}` | | `rebase_worktree()` |
| `release-worktree` | `--task` | | Removes the worktree and keeps its branch | `{task, worktree, branch, head}` | | `release_worktree()` |
| `integrate` | `--task --id --pr --review --after --gh --release-scripts` | `--acceptance` | Launches the integration assignment, which merges through the release skill | The receipt, its `state` `launched` or `delivered` | Exit 1: the receipt on stdout in any other state | `integrate()` in `skills/factory/runtime/integration.py` |
| `delivered` | `--task --pr --review --after --gh --release-scripts` | | Writes `TASK.REVIEW.delivered.json` beside the task | `{schema_version, kind: "release-skill-delivery", task, inputs, pr, head, merge_commit, review, required, approvals, receipt_path}` | | `delivered()` in `skills/factory/runtime/integration.py` |
| `ready_runner.py` | `--plan --codex --rex --server` | `--claude --grok --antigravity --from-writer --seconds --once` | Launches, collects and tidies the plan's jobs on its line | `{schema_version, result: "complete", jobs, active_workers, issues, tidy_issues, metrics, capacity, capacity_error, elapsed_seconds}`, each job `{task, id, state}` plus `reason` when one applies | Exit 1: the same document with `result` `needs_action`, `pending` or `pending_at_budget` | `run()`, `Runner.tick()` in `skills/factory/runtime/ready_runner.py` |
