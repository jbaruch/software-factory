# Ready plan

`ready_runner.py` runs a plan: one JSON file, schema 1. Plans are declarative operator input, not mutable queue history. Unknown versions, unknown fields, duplicate JSON keys, non-finite values and invalid job graphs refuse before execution. Keep plans private when goals or paths are sensitive, and never put credentials in one. Keep plan files in a durable directory, such as `~/.local/state/software-factory/plans/LINE/`, never a session scratchpad: macOS clears `/private/tmp` on reboot, and recovering after one means re-running the same plans. The runner's full contract (recovery, capacity, results) is in [docs/ready-runner.md](https://github.com/jbaruch/software-factory/blob/main/docs/ready-runner.md).

Every job supplies all fields shown here. Replace the candidate and model with the actual selected commit and a discovered eligible model ID:

```json
{
  "schema_version": 1,
  "limits": {
    "workers": 2,
    "tasks": 4,
    "reserve_workers": 1,
    "minimum_remaining_percent": 15
  },
  "jobs": [
    {
      "task": "example",
      "id": "qa",
      "role": "qa",
      "goal": "Verify the selected candidate against the admitted acceptance references",
      "cwd": "/absolute/path/to/clean/worktree",
      "candidate": "<full-commit-sha>",
      "requirements": "requirements-v1",
      "harness": "<selected-harness>",
      "model": "<discovered-eligible-model-id>",
      "effort": "high",
      "seconds": 180,
      "purpose": "work",
      "write": false,
      "commit_message": null,
      "after": []
    }
  ]
}
```

## Fields

- `candidate`/`requirements` must equal the task's currently selected inputs (`factory.py show --task TASK`). One plan uses one pair per task; a job planned against other inputs reports `stale`, except a writer whose own commit became the selection, which reports its result.
- `harness` selects `codex`, `claude`, `grok` or `antigravity`. Write it explicitly in new plans. Schema 1 accepts omission as Codex for saved plans. Model and effort belong to the job. Claude and Antigravity jobs take an effort from `low` to `max`. An Antigravity job may set `effort` to null for a model that refuses `--effort`, such as `claude-opus-4-6-thinking`. Grok jobs take one of the model's discovered efforts.
- `turns` is optional: a Claude or Grok job's turn budget, from 1 to 1000. Without it the job gets the worker's default of 30, so set it for long writers. Codex and Antigravity jobs refuse it.
- `base` is optional: the full commit ID a Claude, Grok or Antigravity read-only job sees the change from. Its evidence then carries the changed paths and the full diff, which a Claude reader, limited to reading files, cannot get otherwise; set it on QA too. Code reviews require it, writers refuse it, and Codex jobs take none, so a Codex job can't be a code review.
- `write: true` with an explicit `commit_message` authorizes the factory-owned local commit after a successful writer report, under the [writer contract](https://github.com/jbaruch/software-factory/blob/main/docs/native-providers.md#explicit-codex-assignments). Read-only jobs use `commit_message: null`. Changing the message changes the immutable assignment request.
- `seconds` is the job's wall-clock budget, at most 86400: roughly 3600–5400 for writers, 1800–2400 for readers.
- `cwd`: readers may share a checkout, including the writer's once it is collected. Codex, Antigravity and Grok readers work in a disposable copy of it, so their tests and MCP output never dirty it.
- `purpose` is `work`, `correction` or `investigation`; `investigation` goes with the `investigator` role and only with it.
- Jobs have distinct `(task, id)` pairs. `after` names earlier jobs in this plan or existing assignments in the same task. A dependency needs a successful current report and an observed successful process exit; findings, failures, stale reports and changed requests do not advance it.

## Selecting a route

For a fresh quota report before planning, run the ready runner with an empty `jobs` list, the intended limits, `--once` and every installed harness binary. For every job:

1. Read the `harnesses` report. `available: true` means the Codex pool and catalog gates passed. `available: false` makes the route unavailable for the reported reason. `available: null` means headroom was measured and eligibility is provisional. A failed or stale observation supplies no capacity until a fresh measurement succeeds. Reobserve after a reset.
2. Verify every route's role capabilities, including tool access, write mode, review scope and independence. Verify model and effort support through native discovery or operator evidence. Require included subscription billing without a paid override and a verified mapping from the model to its applicable quota windows. Each applicable window must show fresh numeric headroom strictly above the plan's reserve. For `null`, resolve any missing model, billing or pool evidence before selection or trial and apply the same window gates.
3. Compare eligible routes by fit for the same role and work, and by measured headroom in their applicable windows. Give neither provider nor report status precedence. A Claude `null` route that meets all the gates and a Codex `true` route that meets them can both take suitable work.
4. An otherwise eligible route that still needs evidence of suitability for the role can take one bounded trial with an explicit assignment, limited scope and budget. Check existing assignments across the line for that role and route first and await any outstanding trial. Investigate a failed trial before planning another. Use ordinary assignment and trial results only as evidence of suitability for that role. Use existing matched reviews for cross-model performance claims, with the same role, prompt and input packet, equivalent tool access, context and effort, and independent sessions.

Gemini Flash and Gemini Pro share Antigravity's Gemini quota group. Use its observed weekly window when planning assignments. [Antigravity CLI /usage](https://antigravity.google/docs/cli/commands/usage/)

Use existing plans and task records to track assignments and outcomes. Route selection carries no task acceptance, budget extension or launch authority.

## Runner flags

`--codex` is always required because it also sandboxes every writer's factory commit. Pass every installed harness binary, even one the plan doesn't use: the result's `harnesses` then says whether each one can take work, in the shape [docs/ready-runner.md](https://github.com/jbaruch/software-factory/blob/main/docs/ready-runner.md) defines. `--claude`, `--grok` and `--antigravity` are required when the plan has jobs for them. The Codex quota gate applies only to Codex jobs. Assignments outside the plan count toward `workers` while their process runs; the result's `active_workers` includes them. Collection and `--from-writer` evidence follow each assignment's harness.

## Verifying a writer's commit

After a writer is collected, run a verification plan with `--from-writer WRITER_ID`:

- The plan may cover several of the line's tasks whose writers share that ID; one task moving on to a correction doesn't stop the others' verification. Every job is a read-only verifier (`qa`, `code-review`, `test-automation`, `security`, `ui-design`, `accessibility`, `data-engineering`, `sre`, `performance` or `integration`, which verifies a replay), uses an ID distinct from the writer and names the writer in `after`. The runner checks every task's writer before it moves any task's inputs, so one refused task changes none.
- Keep the plan's `candidate`/`requirements` at the writer's original input pair. The runner selects the writer's produced commit as the task's inputs and retargets the plan in memory.
- Restart with the same plan and writer ID; selection is reused, never repeated.

Later phases, such as a correction, plan against the produced commit, which is then the task's selected candidate.

## Rerunning a failed writer

After `factory.py reset --task TASK --id WRITER_ID`, plan a new writer under a new ID on the same checkout and the task's current inputs, with the same role and `purpose`. Its goal starts with `git apply <patch>` (the path `reset` printed), then re-runs the gate and checks the result against the original goal. Keep `after` empty.
