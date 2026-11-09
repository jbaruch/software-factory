# Ready plan

`ready_runner.py` runs a plan: one JSON file, schema 1. Plans are declarative operator input, not mutable queue history. Unknown versions, unknown fields, duplicate JSON keys, non-finite values and invalid job graphs refuse before execution. Keep plans private when goals or paths are sensitive, and never put credentials in one. The runner's full contract (recovery, capacity, results) is in [docs/ready-runner.md](https://github.com/jbaruch/software-factory/blob/main/docs/ready-runner.md).

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

- `candidate`/`requirements` must equal the task's currently selected inputs (`factory.py show --task TASK`). One plan uses one pair per task; a job planned against other inputs reports `stale`.
- `harness` is optional: `codex` (the default when omitted), `claude`, `grok` or `antigravity`. Model and effort belong to the job. Claude and Antigravity jobs take an effort from `low` to `max`. An Antigravity job may set `effort` to null for a model that refuses `--effort`, such as `claude-opus-4-6-thinking`. Grok jobs take one of the model's discovered efforts.
- `turns` is optional: a Claude or Grok job's turn budget, from 1 to 1000. Without it the job gets the worker's default of 30, so set it for long writers. Codex and Antigravity jobs refuse it.
- `base` is optional: the full commit ID a Claude, Grok or Antigravity `code-review` job reviews from. Those code reviews require it, writers refuse it and Codex jobs take none.
- `write: true` with an explicit `commit_message` authorizes the factory-owned local commit after a successful writer report, under the [writer contract](https://github.com/jbaruch/software-factory/blob/main/docs/native-providers.md#explicit-codex-assignments). Read-only jobs use `commit_message: null`. Changing the message changes the immutable assignment request.
- `purpose` is `work`, `correction` or `investigation`; `investigation` goes with the `investigator` role and only with it.
- Jobs have distinct `(task, id)` pairs. `after` names earlier jobs in this plan or existing assignments in the same task. A dependency needs a successful current report and an observed successful process exit; findings, failures, stale reports and changed requests do not advance it.

## Runner flags

`--codex` is always required because it also sandboxes every writer's factory commit. `--claude`, `--grok` and `--antigravity` are required when the plan has jobs for them. The Codex quota gate applies only to Codex jobs. Collection and `--from-writer` evidence follow each assignment's harness.

## Verifying a writer's commit

After a writer is collected, run a verification plan with `--from-writer WRITER_ID`:

- The plan covers exactly one task. Every job is read-only `qa`, `code-review` or `test-automation`, uses an ID distinct from the writer and names the writer in `after`.
- Keep the plan's `candidate`/`requirements` at the writer's original input pair. The runner selects the writer's produced commit as the task's inputs and retargets the plan in memory.
- Restart with the same plan and writer ID; selection is reused, never repeated.

Later phases, such as a security review or a correction, plan against the produced commit, which is then the task's selected candidate.
