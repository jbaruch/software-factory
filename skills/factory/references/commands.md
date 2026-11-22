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

## Controller terminals

Run one attempt in the existing foreman terminal with `ready_controller.py`.

```sh
python3 -B "$SKILL_DIR/runtime/ready_controller.py" \
  /absolute/task/logs/run-001.log /absolute/project /absolute/python3.11 -- \
  --plan /absolute/plan.json --codex /absolute/codex \
  --rex /absolute/rex --server 'unix:///explicit/socket' --seconds 600
```

Set `SKILL_DIR` to the absolute realized skill directory. Pass a fresh absolute
log path in an existing private directory, the absolute working directory, and the
absolute Python 3.11+ executable. Pass all runner flags after `--`. Include
`--claude`, `--grok` and `--antigravity` for installed harnesses and
`--from-writer` when selecting a writer. For retries and resumes,
reuse the interpreter in the existing assignment's `request.command[0]` from
`factory.py show --task TASK`. Frozen commands depend on that path.
[Interpreter resume limitation](https://github.com/jbaruch/software-factory/issues/239)

The command creates the private log before login-shell startup and captures both
streams there unchanged, including early diagnostics and any runner JSON. When
the direct child exits, the command records a standalone `controller_exit=N` line,
prints `{"log": "...", "controller_exit": N}` on stdout, and exits with status
`N`. Use the process exit status and stdout summary to determine the launcher
outcome. Startup descendants can append to the log after the exit record.
Invalid launcher arguments exit 2. Log open or write errors exit 1 with a
diagnostic on stderr. A child that exits zero before the runner acknowledges
startup yields launcher exit 1. Inspect the login shell and pinned interpreter
if the log reports that acknowledgment is missing. For a completed plan, look
for `"result": "complete"` in the runner's JSON. On exit 75, wait for the line
lock to clear. Reconcile existing effects with `show` or `status` before retrying
with a fresh log.

For background work, append `&` to the same invocation in the existing terminal.
Keep that shell until the command settles and collect its terminal result before
handoff.

## Rex helpers

A direct `rex run` tab is owned by the foreman that opens it, outside factory
worker collection. A visible command needs no `--keep-open` while it runs:

```sh
rex --autostart=false -S ENDPOINT run -s LINE --label preflight --shell=none -- /absolute/preflight
```

For optional visibility, reuse at most one log monitor per line. For example:

```sh
rex --autostart=false -S ENDPOINT run -s LINE --label controller-log --shell=none -- tail -F /absolute/task/logs/run-001.log
```

Save the endpoint used to open the helper and the full session, window and block
IDs returned by Rex. Keep the monitor visible while monitoring is needed. When
monitoring is done or switching logs, stop your owned monitor by its saved IDs.

```sh
rex --autostart=false -S ENDPOINT send-key -s SESSION_ID -b BLOCK_ID ctrl+c
```

If Rex retains the monitor, follow the finished-helper steps below. At handoff,
either stop it or give the next foreman its endpoint, full IDs and log path with
explicit ownership.
Preserve active unrelated monitors. Helpers normally auto-close on exit; Rex
can retain a quick failure for inspection even without `--keep-open`.

1. Inspect your retained helpers before handoff or wrap-up. A populated
   `last_exit` confirms the process finished.

   ```sh
   rex --autostart=false -S ENDPOINT block call -s SESSION_ID -b BLOCK_ID process
   ```

2. Capture needed output into existing task logs, receipts or plan results.

   ```sh
   rex --autostart=false -S ENDPOINT capture -s SESSION_ID -b BLOCK_ID --trim > /absolute/task/logs/helper.txt
   ```

3. Close the finished helper after any needed capture succeeds.

   ```sh
   rex --autostart=false -S ENDPOINT block close -s SESSION_ID BLOCK_ID
   ```

Deliberate `--keep-open` is an inspection exception with the same owner and closing
condition. Close only your finished helper block, preserving any user split.
Factory worker tabs retain [their own capture-before-close lifecycle](https://github.com/jbaruch/software-factory/blob/main/docs/native-rex.md#tab-tidying).

## Commands

Sources are in `skills/factory/runtime/factory.py` unless the row names another file.

| Command | Effect | Prints | Source |
|---|---|---|---|
| `admit` | Records the task's goal, acceptance, allowances and line, and selects the repository's HEAD as its first inputs | Task view | `admit()` |
| `line` | Moves a task with nothing open to another line | Task view | `move_line()` |
| `inputs` | Selects the task's candidate and requirements revision, capturing its GitHub acceptance issues | Task view | `set_inputs()` |
| `extend-allowance` | Raises a correction or investigation allowance with the operator's approval, or adds the one correction round a current `completed` judge ruling grants (`--judge ID`, three per task) | Task view | `extend_allowance()` |
| `pause`, `resume` | Holds or releases new launches | Task view | `hold_task()` |
| `show` | None; an assignment's `state` is its launch, and `closed.state` how its process ended (`failed` with no result: relaunch under a new ID) | Task view | `task_view()` |
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
| `export-writer` | Exports a settled writer as source history for one-hop transfer; prints source host and SHA-256 | `{source_host, sha256, out}` | `writer_transfer.export_writer()` |
| `import-writer` | Imports a writer bundle on the destination host; requires `--rex` and `--server` | Task view | `writer_transfer.import_writer()` |
| `ready_runner.py` | Launches, collects and tidies a plan's jobs on its line | `{schema_version, result, jobs, …}`; exit 1 unless `result` is `complete` | `skills/factory/runtime/ready_runner.py` `run()` |

- **Task view:** the task record (`task`, `line`, `admission`, `inputs`, `assignments`, `hold`, `provisioning` and its event logs), plus `allowances` and each assignment's `result_current`.
- **Status object:** `{role, state, session_id, window_id, block_id, exit}`, plus `reason` for a lost or abandoned attempt and `closed: true` once tidied.

## Cross-host writer transfer

Transfer a settled writer's provenance from SOURCE_HOST (task `SRC`) to DEST_HOST (task `DST`). The bundle file must be outside the state directory, written to a path only the operator can read.

**On the source host** (task must be held and schema 11):

```sh
# Export; prints source_host and sha256 — record both values.
python3 -B "$SKILL_DIR/runtime/factory.py" --state-dir /src/state \
  export-writer --task SRC --id WRITER_ID \
  --to-host DEST_HOST_ID --to-task DST \
  --out /tmp/transfer.bundle
# {"source_host": "src-host-id", "sha256": "aabbcc...", "out": "/tmp/transfer.bundle"}
```

Transfer the bundle to the destination host by any secure operator channel. Confirm the SHA-256 of the received file matches the printed value before proceeding.

**On the destination host** (fresh admission, no assignments):

```sh
# 1. Admit the task (use the exact candidate C and requirements R from the source).
python3 -B "$SKILL_DIR/runtime/factory.py" --state-dir /dst/state \
  admit --task DST --line DST --goal 'Imported writer provenance' \
  --repo /dst/checkout

# 2. Select inputs (must match the source bundle's candidate and requirements).
python3 -B "$SKILL_DIR/runtime/factory.py" --state-dir /dst/state \
  inputs --task DST --candidate C --requirements R

# 3. Import; --from-host and --sha256 come from the export output, never from the bundle.
python3 -B "$SKILL_DIR/runtime/factory.py" --state-dir /dst/state \
  --rex "$(command -v rex)" --server 'unix:///dst/rex.sock' \
  import-writer --task DST --file /path/to/transfer.bundle \
  --from-host src-host-id --sha256 aabbcc...
```

**Verify the destination is fresh before import** — `show --task DST` must have no assignments and no `writer_origin`. After import, `show` returns a schema-12 task view. Run `resume --task DST` to allow launches, then verify with a plain read-only plan (no `--from-writer`).

The SHA-256 digest authenticates the operator-pinned bytes; it is not a host signature and cannot protect against a dishonest source host. The destination proves Git objects, repository, requirements and all new approvals independently.
