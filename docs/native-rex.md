# Native Rex launcher

`factory.py` is the first live application slice. It creates a Rex session for a task and a separate tab/pane for each explicit specialist assignment. It runs the actual command directly through Rex with `--shell=none`; there is no Herdr or nested tmux layer. It never renames an existing session or switches the user's visible session.

## Launch and inspect

Use Python 3.11+ on macOS/Linux with `fcntl` support. The first launch requires an existing absolute Rex binary and an explicit local Unix-socket endpoint. Commands also use absolute executable paths so the server's PATH cannot select another program. Working directories must exist. Supply an isolated writer worktree for implementation; admitted tasks can explicitly [provision one through the owner](task-state.md#writer-worktree-provisioning) before launch.

```sh
python3 factory.py --rex '/absolute/path/to/rex' --server 'unix:///path/to/server.sock' \
  start --task example --id qa --role qa --goal 'Review the supplied candidate' \
  --cwd '/absolute/path/to/worktree' -- '/absolute/path/to/agent' '{instructions}'

python3 factory.py status --task example --id qa
python3 factory.py capture --task example --id qa
```

The actual native agent flags depend on that harness. `{instructions}` as a whole argument expands to the selected role instructions and assignment goal. `{brief}` expands to a private Markdown file with the same content. Put the appropriate placeholder where that command accepts its prompt or prompt-file argument. Merely labeling an assignment `qa` does not make an arbitrary program read the brief. No provider/model is selected or permission mode weakened by the launcher.

State defaults to `.factory-state/` beside the application; `--state-dir PATH` overrides it. Each task has one private JSON record and a short-lived advisory lock. The record pins the Rex binary, endpoint and host identity and retains assignment inputs, a unique session/window marker and returned full IDs. A short role brief is the only generated handoff artifact.

Repeat `start` with the same task, assignment ID and inputs to reconnect to the original pane. Changed inputs require a new explicit assignment ID. [Admitted tasks](task-state.md) require an explicit attempt purpose and enforce cumulative correction/investigation limits before new launches. They bind new assignments and reported results to selected revisions and check any `--after` dependencies before launch. Direct `start` without admission retains the original explicit-launch behavior with unknown task allowances. Neither mode selects roles or authorizes additional scope.

## Recovery and status meanings

Before launch, save the pending assignment. After Rex returns, save its session, window and block IDs. If the controller stops or the CLI times out between those writes, `status` or repeated `start` searches the exact unique session/window marker and records the existing pane. A pending assignment prevents another launch on that task until reconciliation.

If no unique matching pane is visible, the launch remains uncertain and is not repeated. Missing sessions, changed host identity, inaccessible servers or modified assignment inputs are errors for the operator to inspect; deleting state is not recovery. A user-renamed/deleted pending window may require manual reconciliation. This is deliberately conservative and does not claim exactly-once delivery or complete task recovery.

`running`, `exited`, `failed`, `lost` and `unknown` describe the terminal process. Exit zero does not approve a review, accept a task or prove the agent completed the goal. `capture` returns original terminal output as data. Use the applicable [provider adapter](native-providers.md) to collect native results.

`lost` means Rex can no longer report how the original process ended. A Rex server restart kills every pane process and restores the panes as fresh login shells without an exit status, which would otherwise read as `running` forever. Each assignment therefore pins the server `instance_id` it launched on; a different connected instance makes it `lost` with a `reason`. Lost is terminal. The ready runner releases its slot and blocks dependents, and provider collection accepts it only together with the worker's own completed, exit-zero receipt. Assignments recorded before task schema 10 have no instance pin and keep the previous process observation.

## Verification

Unit tests use a fake server and never touch live Rex or provider accounts. They cover concurrent mutations, changed inputs/host, repeat launches, unknown outcomes and restart reconciliation for both initial sessions and additional specialist tabs.

The opt-in smoke check creates one session with two harmless commands, verifies the role briefs and actual working directory, checks process exit, captures output and repeats launches to confirm pane reuse:

```sh
python3 scripts/smoke_rex.py --rex '/absolute/path/to/rex' --server 'unix:///path/to/server.sock'
```

It prints the state directory and full IDs and keeps the panes available for inspection. Use `--state-dir PATH` to resume that same check. Ordinary tests do not run this command. A sandbox may block the Unix socket; rerun with the host's normal execution approval rather than bypassing it or selecting a different Rex server.

Initial local conformance observations on Rex API revision 11:

- Harmless QA/security commands ran in separate panes in one session, read their role briefs, confirmed the canonical working directory and exited zero. Repeated launches returned the same block IDs.
- The native Claude CLI launched and returned a weekly-allowance error. This verifies failure visibility, not a successful model task or available Claude capacity.
- The native ChatGPT-authenticated Codex CLI completed a read-only QA inspection of the supplied reference functions with no findings and no tool use. Its recorded turn consumed 25,626 input tokens (13,184 cached) and 378 output tokens. That is one context-overhead baseline, not evidence of efficient worker bootstrap or general review quality. Existing native user configuration was preserved.

These historical checks validate only the launch slice.

## Scope

See the [implementation inventory](implementation.md) for current application coverage and remaining integration work, and the [local operator guide](operator-view.md) for viewing task records and process observations.
