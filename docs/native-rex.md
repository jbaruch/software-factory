# Native Rex launcher

`factory.py` is the first live application slice. Each factory line (one coordinator's stream of work, such as nanoclaw or ACR, named at admission with `--line`) has one Rex session, named after the line, and each explicit specialist assignment gets its own tab/pane there, labeled `<task>:<role>-<assignment>-<8 hex>` so the tasks' tabs tell apart. A line's launches run under its own execution lock, so no two of its tasks create the session at once, and other lines launch in parallel. A coordinator whose own Rex session carries the line's name gets its agents' tabs there, beside its own; the factory adds and closes only its own tabs and kills the session only once it holds no tab at all. Without such a session, the first launch opens one under the line's name. Tasks recorded before lines belong to the `default` line; ones still holding an older session, `factory-<line>` or their own `factory-<task>-<8 hex>`, keep it until it closes. It runs the actual command directly through Rex with `--shell=none`; there is no Herdr or nested tmux layer. It never renames an existing session or switches the user's visible session.

## Launch and inspect

In a project that installed the factory with ACR, run these commands from the realized skill. ACR rewrites the `skills/factory/...` paths in the skill's own instructions to that location, and the first command installs the per-user runtime it then runs from. Upgrade with `acr update` and `acr realize`; the next command upgrades the per-user runtime in place. Restart long-running controllers such as the ready runner or dashboard after an upgrade.

Use Python 3.11+ on macOS/Linux with `fcntl` support. The first launch requires an existing absolute Rex binary and an explicit local Unix-socket endpoint. Commands also use absolute executable paths so the server's PATH cannot select another program. Working directories must exist. Supply an isolated writer worktree for implementation; admitted tasks can explicitly [provision one through the owner](task-state.md#writer-worktree-provisioning) before launch.

```sh
python3 -B skills/factory/runtime/factory.py --rex '/absolute/path/to/rex' --server 'unix:///path/to/server.sock' \
  start --task example --id qa --role qa --goal 'Review the supplied candidate' \
  --cwd '/absolute/path/to/worktree' -- '/absolute/path/to/agent' '{instructions}'

python3 -B skills/factory/runtime/factory.py status --task example --id qa
python3 -B skills/factory/runtime/factory.py capture --task example --id qa
python3 -B skills/factory/runtime/factory.py tidy [--task example]
```

The actual native agent flags depend on that harness. `{instructions}` as a whole argument expands to the selected role instructions and assignment goal. `{brief}` expands to a private Markdown file with the same content. Put the appropriate placeholder where that command accepts its prompt or prompt-file argument. Merely labeling an assignment `qa` does not make an arbitrary program read the brief. No provider/model is selected or permission mode weakened by the launcher.

State defaults to the per-user `~/.local/state/software-factory` (or `$XDG_STATE_HOME/software-factory`), shared by every project that installs the factory; `--state-dir PATH` overrides it. Each task has one private JSON record and a short-lived advisory lock. The record pins the Rex binary, endpoint and host identity and retains assignment inputs, a unique session/window marker and returned full IDs. A short role brief is the only generated handoff artifact.

Repeat `start` with the same task, assignment ID and inputs to reconnect to the original pane. Changed inputs require a new explicit assignment ID. [Admitted tasks](task-state.md) require an explicit attempt purpose and enforce cumulative correction/investigation limits before new launches. They bind new assignments and reported results to selected revisions and check any `--after` dependencies before launch. Direct `start` without admission retains the original explicit-launch behavior with unknown task allowances. Neither mode selects roles or authorizes additional scope.

## Recovery and status meanings

Before launch, save the pending assignment. After Rex returns, save its session, window and block IDs. If the controller stops or the CLI times out between those writes, `status` or repeated `start` searches the exact unique session/window marker and records the existing pane. A pending assignment prevents another launch on that task until reconciliation.

If no unique matching pane is visible, the launch remains uncertain and is not repeated. Changed host identity, inaccessible servers or modified assignment inputs are errors for the operator to inspect; deleting state is not recovery. A user-renamed/deleted pending window may require manual reconciliation. This is deliberately conservative and does not claim exactly-once delivery or complete task recovery.

When the operator knows Rex rejected the request, for example with a CLI usage error after a Rex update, `factory.py abandon --task TASK --id ID --reason TEXT` records that decision instead of deleting state. It refuses while a pane carrying the launch marker exists in the line's session; reconcile it with `status`. The abandoned assignment keeps its history and allowance use, reports `abandoned`, never launches again and blocks dependents. Launch the work again under a new ID. If a pane with that marker appears later anyway, close it by hand.

`factory.py cancel --task TASK --id ID` stops a running native assignment, for example one launched on a brief known to be wrong. It sends Ctrl-C to the assignment's pane. The worker's `asyncio.run` turns that SIGINT into cancelling its execution, and the terminal delivers the same SIGINT to the native child. The worker then saves its receipt as `failed` with a cancel error, stops the child as it does at a timeout, and exits. Inspect it with `status` or `capture`, `reset` a writer's checkout as for any failed attempt, and launch the replacement under a new ID. It refuses a pending, finished or non-native assignment. A Ctrl-C typed into the pane by hand records the same failure.

Operator tab changes never block a task. Splitting, renaming, moving or closing factory panes and adding your own tabs to a factory session are tolerated, because each command checks only the pane it acts on. A pane or session closed outside the factory makes that assignment `lost`; other assignments are unaffected. A window labeled like this task's factory marker (`<task>:<catalog role>-<assignment>-<8 hex>`, or `<catalog role>-<assignment>-<8 hex>` in a task's own earlier session) that is missing from task history still refuses as lost task history. Other tasks' tabs in the shared session and any other operator tab name, such as `notes-20261108`, are ignored.

`running`, `exited`, `failed`, `lost` and `unknown` describe the terminal process. Exit zero does not approve a review, accept a task or prove the agent completed the goal. `capture` returns original terminal output as data. Use the applicable [provider adapter](native-providers.md) to collect native results.

`lost` means Rex can no longer report how the original process ended: its pane or session was closed outside the factory, or the Rex server restarted. A Rex server restart kills every pane process and restores the panes as fresh login shells without an exit status, which would otherwise read as `running` forever. Each assignment therefore pins the server `instance_id` it launched on; a different connected instance makes it `lost` with a `reason`. Lost is terminal. The ready runner releases its slot and blocks dependents, and provider collection accepts it only together with the worker's own completed, exit-zero receipt. Assignments recorded before task schema 10 have no instance pin and keep the previous process observation.

## Tab tidying

Finished assignment tabs close automatically, so a factory session holds only work that is still running. A tab is finished once its process is `exited`, `failed` or `lost`; `running`, `unknown` and pending launches are never touched. Before a tab closes, its task record keeps the final process observation under the assignment's `closed` field, and its terminal text goes to a private `<task>.<id>.capture.txt` beside the record. `status` and `capture` then answer from that retained copy without contacting Rex, even while the server is down, so collection, dependencies and integration recovery keep working after the tab is gone. An abandoned launch likewise reports its status offline and has no output. A `lost` pane keeps no output: a closed pane has none, and a restored pane shows only a new shell.

The ready runner tidies every task in its state directory on each tick, after collecting results. A successful `codex-collect`, `claude-collect` or `grok-collect` closes that assignment's tab. If that tidy fails, the command still prints the saved result, reports the tidy error and exits 1; run `tidy` to close the tab. `factory.py tidy` closes every finished tab in one task or in the whole state directory and reports per task what it closed, what is still running, and any error. Tidying closes only the factory's own block, so an operator split stays put; a lone block takes its tab along. Once a task has nothing open left it leaves its line's session; once the session holds no tab of any task and no operator tab, it is killed, and the next launch opens it again under the same label. Killing the shared session needs the line's execution lock, so another task's launch can never lose its session mid-launch: `tidy` and a collect's tidy run outside a launcher, take the lock only when it is free, and otherwise close their tabs and leave the session for the next tidy. A session killed outside the factory is recorded the same way, as `lost` tabs, the next time the task launches or tidies. If a close fails after the output was retained, the next tidy retries the close without capturing again.

## Verification

Unit tests use a fake server and never touch live Rex or provider accounts. They cover concurrent mutations, changed inputs/host, repeat launches, unknown outcomes and restart reconciliation for both initial sessions and additional specialist tabs.

The opt-in smoke check creates one session with two harmless commands, verifies the role briefs and actual working directory, checks process exit, captures output, repeats launches to confirm pane reuse, then tidies both tabs and confirms that their status and output survive and the session is gone:

```sh
python3 scripts/smoke_rex.py --rex '/absolute/path/to/rex' --server 'unix:///path/to/server.sock'
```

It prints the state directory, full IDs and tidy report and leaves no tabs behind; `status` and `capture` on that state directory still show the retained results. Use `--state-dir PATH` to resume that same check. Ordinary tests do not run this command. A sandbox may block the Unix socket; rerun with the host's normal execution approval rather than bypassing it or selecting a different Rex server.

Initial local conformance observations on Rex API revision 11:

- Harmless QA/security commands ran in separate panes in one session, read their role briefs, confirmed the canonical working directory and exited zero. Repeated launches returned the same block IDs.
- The native Claude CLI launched and returned a weekly-allowance error. This verifies failure visibility, not a successful model task or available Claude capacity.
- The native ChatGPT-authenticated Codex CLI completed a read-only QA inspection of the supplied reference functions with no findings and no tool use. Its recorded turn consumed 25,626 input tokens (13,184 cached) and 378 output tokens. That is one context-overhead baseline, not evidence of efficient worker bootstrap or general review quality. Existing native user configuration was preserved.

These historical checks validate only the launch slice.

## Scope

See the [implementation inventory](implementation.md) for current application coverage and remaining integration work, and the [local operator guide](operator-view.md) for viewing task records and process observations.
