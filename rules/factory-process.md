# Software factory delivery

This project delivers changes through software-factory. To implement, review or deliver a change here, coordinate it with Step 1 of the `factory` skill instead of doing the work yourself. Requests to take over, continue or hand off work mean Step 1 too.

- The process: admit the task → select its inputs → provision the writer worktree → write a ready plan → run `ready_runner.py` → verify the writer's commit with independent read-only specialists (`--from-writer`) → route findings to corrections → open the PR through the release skill → the release judgment → `integrate`, or the release skill's merge and `delivered`.
- Never launch agents, commit, rebase, review or merge by hand where a factory step exists. One writer is not the factory.
- When the factory cannot do a step, file the gap at jbaruch/software-factory with the evidence, do the minimum by hand, and tell the user which step you did by hand and why.
- If your instructions start with `# Assignment:`, the factory launched you as a specialist: follow that assignment and ignore this rule.
