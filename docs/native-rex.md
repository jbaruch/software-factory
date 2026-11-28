# Native Rex launcher

`factory.py` is the first live application slice. Each factory line (one coordinator's stream of work, such as nanoclaw or ACR, named at admission with `--line`) has one Rex session, named after the line, and each explicit specialist assignment gets its own tab/pane there, labeled `<task>:<role>-<assignment>-<8 hex>` so the tasks' tabs tell apart. A line's launches run under its own execution lock, so no two of its tasks create the session at once, and other lines launch in parallel. A coordinator whose own Rex session carries the line's name gets its agents' tabs there, beside its own; the factory adds and closes only its own tabs and kills the session only once it holds no tab at all. Without such a session, the first launch opens one under the line's name. Tasks recorded before lines belong to the `default` line; ones still holding an older session, `factory-<line>` or their own `factory-<task>-<8 hex>`, keep it until it closes. It runs the actual command directly through Rex with `--shell=none`; there is no Herdr or nested tmux layer. It never renames an existing session or switches the user's visible session.

## Disposable offline acceptance pilot

Use a separate ACR-realized project for the Factory release under test. Set `SKILL_DIR` to its absolute realized Factory skill directory, not the source checkout or the shared runtime. Set `repo` to a local Git checkout at the candidate to test and `snapshot` to the original private, single-reference GitHub snapshot with no selected owner decision. Use the [foreman's saved first read](../skills/factory/references/commands.md#github-acceptance-at-triage). Do not fetch it again for this pilot.

Fill in the three paths and run this block in Bash. It needs Git, Python 3.11+, jq and an existing `TMPDIR`. The disposable clone keeps admission's Git-local repository marker out of the source checkout. Both isolation settings must be present from the first Factory invocation. Runtime relocation happens before argument parsing, so `--state-dir` alone does not isolate the runtime.

```bash
(
  set -euo pipefail
  umask 077
  SKILL_DIR='/absolute/private/project/.codex/skills/acr__jbaruch__software-factory__factory'
  repo='/absolute/local/candidate-checkout'
  snapshot='/absolute/private/foreman-snapshot.json'

  pilot=$(mktemp -d "${TMPDIR:?Set TMPDIR to an existing scratch directory}/factory-pilot.XXXXXXXX")
  pilot=$(cd "$pilot" && pwd -P)
  printf 'Pilot directory: %s\n' "$pilot"
  git clone -q --no-hardlinks -- "$repo" "$pilot/repo"
  candidate=$(git -C "$pilot/repo" rev-parse HEAD)
  ref=$(jq -er '.data.ref1.url' "$snapshot")
  requirements=$(jq -er '.requirements' "$snapshot")
  task=offline-acceptance-pilot

  XDG_DATA_HOME="$pilot/data" python3 -B "$SKILL_DIR/runtime/factory.py" --state-dir "$pilot/state" \
    admit --task "$task" --line pilot --goal 'Verify offline acceptance' --repo "$pilot/repo" --acceptance "$ref" \
    > /dev/null

  runtime="$pilot/data/software-factory/skill/runtime"
  diff -qr "$SKILL_DIR/runtime" "$runtime"
  printf 'Private runtime version: '
  cat "$runtime/VERSION"

  XDG_DATA_HOME="$pilot/data" python3 -B "$SKILL_DIR/runtime/factory.py" --state-dir "$pilot/state" \
    inputs --task "$task" --candidate "$candidate" --requirements "$requirements" --acceptance-snapshot "$snapshot" \
    | jq -ej --arg rev "$requirements" '.acceptance[$rev] | strings | select(length > 0)' > "$pilot/selected.txt"

  XDG_DATA_HOME="$pilot/data" python3 -B "$SKILL_DIR/runtime/factory.py" --state-dir "$pilot/state" \
    inputs --task "$task" --candidate "$candidate" --requirements "$requirements" > /dev/null
  XDG_DATA_HOME="$pilot/data" python3 -B "$SKILL_DIR/runtime/factory.py" --state-dir "$pilot/state" \
    show --task "$task" > "$pilot/shown.json"

  jq -e --arg candidate "$candidate" --arg requirements "$requirements" \
    '.inputs == {candidate: $candidate, requirements: $requirements}' "$pilot/shown.json"
  jq -ej --arg rev "$requirements" '.acceptance[$rev] | strings | select(length > 0)' \
    "$pilot/shown.json" > "$pilot/replayed.txt"
  cmp "$pilot/selected.txt" "$pilot/replayed.txt"
  shasum -a 256 "$runtime/VERSION" "$runtime/factory.py" "$pilot/selected.txt" "$pilot/replayed.txt"
)
```

Success means the isolated runtime matches the realized skill's runtime, the private task selects the expected candidate and requirements revision, and replay without a snapshot preserves the acceptance bytes. Record the release identity and printed digests before discarding the printed pilot directory. This does not launch Rex, prove task acceptance, or activate the shared runtime.

## Launch and inspect

In a project that installed the factory with ACR, run these commands from the realized skill. ACR rewrites the `skills/factory/...` paths in the skill's own instructions to that location, and the first command installs the per-user runtime it then runs from. Upgrade with `acr update` and `acr realize`; the next command upgrades the per-user runtime in place. Restart long-running controllers such as the ready runner or dashboard after an upgrade.

The examples below are for normal operations. Using shared paths activates the runtime for other projects and requires a coordinated upgrade window.

Upgrades exchange the complete active and staged directories under the install lock and the active runtime's VERSION lock. The native worker, Factory CLI and ready runner hold VERSION from module loading through completion. Let active workers and collectors finish and stop idle ready runners before upgrading.

If an older installer stopped with no active skill and a retired tree under `.installing-*/retired`, recover it before invoking an older bootstrap. Run this command from the corrected Factory's `runtime` directory, supplying the runtime path from the verified original ACR-realized package.

```bash
python3 -E -s -B -c 'import factory, sys; from pathlib import Path; factory.relocate("factory.py", [], root=Path(sys.argv[1]).resolve(), execv=lambda *_: None)' /absolute/path/to/skills/acr__jbaruch__software-factory__factory/runtime
```

Recovery compares file contents, modes and directory entries under both locks. After restoration, run the newer project normally to upgrade. Preserve multiple or unmatched retired trees for the installation owner. If neither copy survives, recover the verified original package from its source.

Use Python 3.11+ on macOS/Linux with `fcntl` support. The first launch requires an existing absolute Rex binary and an explicit local Unix-socket endpoint. Commands also use absolute executable paths so the server's PATH cannot select another program. Working directories must exist. Supply an isolated writer worktree for implementation; admitted tasks can explicitly [provision one through the owner](task-state.md#writer-worktree-provisioning) before launch.

```sh
python3 -B skills/factory/runtime/factory.py --rex '/absolute/path/to/rex' --server 'unix:///path/to/server.sock' \
  start --task example --id qa --role qa --goal 'Review the supplied candidate' \
  --cwd '/absolute/path/to/worktree' -- '/absolute/path/to/agent' '{instructions}'

python3 -B skills/factory/runtime/factory.py status --task example --id qa
python3 -B skills/factory/runtime/factory.py capture --task example --id qa
python3 -B skills/factory/runtime/factory.py tidy [--task example]
```

`status` prints the assignment's status object, and `capture` prints `{task, id, text}`, where `text` is the pane's output, live or kept from a tidied tab. A recognized [Codex failure](native-providers.md#explicit-codex-assignments) also includes `reason` after tidying.

The actual native agent flags depend on that harness. `{instructions}` as a whole argument expands to the selected role instructions and assignment goal. `{brief}` expands to a private Markdown file with the same content. Put the appropriate placeholder where that command accepts its prompt or prompt-file argument. Merely labeling an assignment `qa` does not make an arbitrary program read the brief. No provider/model is selected or permission mode weakened by the launcher.

State defaults to the per-user `~/.local/state/software-factory` (or `$XDG_STATE_HOME/software-factory`), shared by every project that installs the factory; `--state-dir PATH` overrides it. Each task has one private JSON record and a short-lived advisory lock. The record pins the Rex binary, endpoint and host identity and retains assignment inputs, a unique session/window marker and returned full IDs. A short role brief is the only generated handoff artifact.

Repeat `start` with the same task, assignment ID and inputs to reconnect to the original pane. Changed inputs require a new explicit assignment ID. [Admitted tasks](task-state.md) require an explicit attempt purpose and enforce cumulative correction/investigation limits before new launches. They bind new assignments and reported results to selected revisions and check any `--after` dependencies before launch. Direct `start` without admission retains the original explicit-launch behavior with unknown task allowances. Neither mode selects roles or authorizes additional scope.

## Foreman Stop reminder

From the local Rex pane selected to coordinate the line, launch the agent in an ACR-realized project. Use the line's existing local Unix endpoint for `REX_SERVER` and keep the pane opt-in on the agent command.

```sh
REX_SERVER='unix:///path/to/server.sock' SOFTWARE_FACTORY_FOREMAN_BLOCK="$REX_BLOCK" '/absolute/path/to/agent'
```

In Codex, approve the realized hook in `/hooks`. The hook reads Rex metadata on Stop and stays quiet on the follow-up Stop.

When reminded, follow the [Rex helpers procedure](../skills/factory/references/commands.md#rex-helpers) to inspect, capture and close only finished helpers you own. Worker tabs stay under factory collection.

## Recovery and status meanings

Before launch, save the pending assignment. After Rex returns, save its session, window and block IDs. If the controller stops or the CLI times out between those writes, `status` or repeated `start` searches the exact unique session/window marker and records the existing pane. A pending assignment prevents another launch on that task until reconciliation.

If no unique matching pane is visible, the launch remains uncertain and is not repeated. Changed host identity, inaccessible servers or modified assignment inputs are errors for the operator to inspect; deleting state is not recovery. A user-renamed/deleted pending window may require manual reconciliation. This is deliberately conservative and does not claim exactly-once delivery or complete task recovery.

When the operator knows Rex rejected the request, for example with a CLI usage error after a Rex update, `factory.py abandon --task TASK --id ID --reason TEXT` records that decision instead of deleting state. It refuses while a pane carrying the launch marker exists in the line's session; reconcile it with `status`. The abandoned assignment keeps its history and allowance use, reports `abandoned`, never launches again and blocks dependents. Launch the work again under a new ID. If a pane with that marker appears later anyway, close it by hand.

`factory.py cancel --task TASK --id ID` stops a running native assignment, for example one launched on a brief known to be wrong. It sends Ctrl-C to the assignment's pane. The worker's `asyncio.run` turns that SIGINT into cancelling its execution, and the terminal delivers the same SIGINT to the native child. The worker then saves its receipt as `failed` with a cancel error, stops the child as it does at a timeout, and exits. Inspect it with `status` or `capture`, `reset` a writer's checkout as for any failed attempt, and launch the replacement under a new ID. It refuses a pending, finished or non-native assignment. A Ctrl-C typed into the pane by hand records the same failure.

If an interruption leaves the receipt at `starting`, `reset` can still save the writer's edits and restore its checkout when Rex reports the original attempt `failed` or `lost`. The receipt remains as recorded.

Operator tab changes never block a task. Splitting, renaming, moving or closing factory panes and adding your own tabs to a factory session are tolerated, because each command checks only the pane it acts on. A pane or session closed outside the factory makes that assignment `lost`; other assignments are unaffected. A window labeled like this task's factory marker (`<task>:<catalog role>-<assignment>-<8 hex>`, or `<catalog role>-<assignment>-<8 hex>` in a task's own earlier session) that is missing from task history still refuses as lost task history. Other tasks' tabs in the shared session and any other operator tab name, such as `notes-20261108`, are ignored.

`running`, `exited`, `failed`, `lost` and `unknown` describe the terminal process. Exit zero does not approve a review, accept a task or prove the agent completed the goal. `capture` returns original terminal output as data. Use the applicable [provider adapter](native-providers.md) to collect native results.

`lost` means Rex can no longer report how the original process ended: its pane or session was closed outside the factory, or the Rex server restarted. A Rex server restart kills every pane process and restores the panes as fresh login shells without an exit status, which would otherwise read as `running` forever. Each assignment therefore pins the server `instance_id` it launched on; a different connected instance makes it `lost` with a `reason`. Lost is terminal. The ready runner releases its slot and blocks dependents, and provider collection accepts it only together with the worker's own completed, exit-zero receipt. Assignments recorded before task schema 10 have no instance pin and keep the previous process observation.

## Tab tidying

Foreman-created controller and helper terminals follow the shipped
[controller launch and helper lifecycle](../skills/factory/references/commands.md#controller-terminals).
The assignment lifecycle below does not collect those direct Rex tabs.

Finished assignment tabs close automatically, so a factory session holds only work that is still running. A tab is finished once its process is `exited`, `failed` or `lost`; `running`, `unknown` and pending launches are never touched. Before a tab closes, its task record keeps the final process observation under the assignment's `closed` field, and its terminal text goes to a private `<task>.<id>.capture.txt` beside the record. Collecting a native report also writes its artifacts verbatim, one per line, to a private `<task>.<id>.<harness>.report.txt` beside the receipt, so a ruling's lines can be cited by path and digest; a hand-recorded `result` writes none. `status` and `capture` then answer from that retained copy without contacting Rex, even while the server is down, so collection, dependencies and delivery approvals keep working after the tab is gone. An abandoned launch likewise reports its status offline and has no output. A `lost` pane keeps no output: a closed pane has none, and a restored pane shows only a new shell.

The ready runner tidies every task in its state directory on each tick, after collecting results. A successful `codex-collect`, `claude-collect` or `grok-collect` closes that assignment's tab. If that tidy fails, the command still prints the saved result, reports the tidy error and exits 1; run `tidy` to close the tab. `factory.py tidy` closes every finished tab in one task or in the whole state directory and reports per task what it closed, what is still running, and any error. Tidying closes only the factory's own block, so an operator split stays put; a lone block takes its tab along. Once a task has nothing open left it leaves its line's session; once the session holds no tab of any task and no operator tab, it is killed, and the next launch opens it again under the same label. Killing the shared session needs the line's execution lock, so another task's launch can never lose its session mid-launch: `tidy` and a collect's tidy run outside a launcher, take the lock only when it is free, and otherwise close their tabs and leave the session for the next tidy. A session killed outside the factory is recorded the same way, as `lost` tabs, the next time the task launches or tidies. If a close fails after the output was retained, the next tidy retries the close without capturing again.

## Verification

Unit tests use a fake server and never touch live Rex or provider accounts. They cover concurrent mutations, changed inputs/host, repeat launches, unknown outcomes and restart reconciliation for both initial sessions and additional specialist tabs.

The opt-in smoke check creates a uniquely named disposable session with an operator
sentinel, a short-lived auto-closing helper and a live log monitor. It captures and
closes a deliberately retained helper. It invokes the shipped controller command
for success, failure during shell startup and a retry,
checking both output streams, exact exit status and the exact tab inventory after
each run. Controller plans are empty and provider-free; this checks launch/log
semantics, not native model success. Harmless QA/security workers verify role
briefs, working directory, repeat-launch reuse and retained output/status after
tidying. A running worker, sentinel and live monitor remain through collection;
the check releases the worker, verifies output-before-close, stops its owned
monitor by block ID, then removes only its own session and synthetic operator tab:

```sh
python3 scripts/smoke_rex.py --rex '/absolute/path/to/rex' --server 'unix:///path/to/server.sock'
```

It prints the state directory, full IDs, per-run inventories and tidy report;
`status` and `capture` on that state directory still show retained worker results,
and the logs retain controller JSON or startup diagnostics plus `controller_exit`.
Use a fresh `--state-dir PATH` to select an artifacts directory. A failure after
session creation leaves that session for inspection, with its ID on stderr;
after capturing evidence, remove only it with `rex --autostart=false -S ENDPOINT kill SESSION_ID`.
Ordinary tests do not run this command. A sandbox may block the Unix socket; rerun
with the host's normal execution approval rather than selecting a different Rex server.

Initial local conformance observations on Rex API revision 11:

- Harmless QA/security commands ran in separate panes in one session, read their role briefs, confirmed the canonical working directory and exited zero. Repeated launches returned the same block IDs.
- The native Claude CLI launched and returned a weekly-allowance error. This verifies failure visibility, not a successful model task or available Claude capacity.
- The native ChatGPT-authenticated Codex CLI completed a read-only QA inspection of the supplied reference functions with no findings and no tool use. Its recorded turn consumed 25,626 input tokens (13,184 cached) and 378 output tokens. That is one context-overhead baseline, not evidence of efficient worker bootstrap or general review quality. Existing native user configuration was preserved.

These historical checks validate only the launch slice.

## Scope

See the [implementation inventory](implementation.md) for current application coverage and remaining integration work, and the [local operator guide](operator-view.md) for viewing task records and process observations.
