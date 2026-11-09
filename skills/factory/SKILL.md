---
name: factory
description: "Coordinate an explicitly assigned software-factory task or inspect its current delivery and exception state."
---

# Factory entrypoint

This skill is an action router — pick the step that matches the user's intent and execute only that step. Do not run other steps; do not parallelize.

Use the connected factory application's task and assignment state when available. Without a connected application, identify this as a design/skill installation and handle the requested planning or inspection directly. Task authority and repository coding/release requirements remain binding.

For specialty selection or an assigned role, consult only the relevant entries in [the role catalog](references/roles.json). Each entry defines activation, capabilities, decision authority and focused instructions; it does not require a permanently occupied agent.

## Step 1 — Coordinate a task

Establish the authorized goal and relevant acceptance evidence, then select only the needed specialists. Preserve their decision authority and independent verification.

Scripts own deterministic execution and known transitions. Jev handles bounded semantic coordination; every question offers `uncertain`. Generic uncertainty goes to the standby reasoner. A substantive dispute follows investigator -> Jev reassessment -> resolution or judge. A remaining specialist objection prevents automatic closure. Use the expensive maximum-thinking judge only after that sequence.

Read current artifacts by reference, route findings directly to their responsible specialist and let independent tasks proceed. Do not rewrite reports, narrate rounds, maintain duplicate ledgers or poll workers with model turns. Unknown external effects are reconciled before retrying, and changing agents never resets task allowances.

Finish here.

## Step 2 — Hand off work

Use this when the user asks to hand the current work to the factory. The factory's intake is its task record; do not write a separate handover document.

1. Commit the work to hand off in this repository and note the commit ID.
2. Run the factory as `python3 -B skills/factory/runtime/factory.py`. It installs or upgrades the per-user runtime on first use and keeps task state in `~/.local/state/software-factory`.
3. Record the task, its inputs and a writer worktree:
   - `admit --task TASK --goal GOAL --repo /absolute/repo --acceptance REFERENCE`
   - `inputs --task TASK --candidate COMMIT --requirements REQUIREMENTS-ID`
   - `provision --task TASK --id writer --path /absolute/new/worktree`
4. Start a writer on that worktree with a model and effort that fit the role. The first launch on a task also needs `--rex "$(command -v rex)" --server ENDPOINT`, with the endpoint from `rex whoami --json`.
   - Claude: `claude-start --task TASK --id ID --role ROLE --purpose work --goal GOAL --cwd WORKTREE --claude /absolute/claude --model MODEL --effort EFFORT --write --commit-message MESSAGE --sandbox /absolute/codex`
   - Grok: `grok-start --task TASK --id ID --role ROLE --purpose work --goal GOAL --cwd WORKTREE --grok /absolute/grok --model MODEL --effort EFFORT --write --commit-message MESSAGE --sandbox /absolute/codex`
   - Antigravity: `antigravity-start --task TASK --id ID --role ROLE --purpose work --goal GOAL --cwd WORKTREE --agy /absolute/agy --model MODEL --effort EFFORT --write --commit-message MESSAGE --sandbox /absolute/codex`
   - Codex: `codex-start --task TASK --id ID --role ROLE --purpose work --goal GOAL --cwd WORKTREE --codex /absolute/codex --model MODEL --effort EFFORT --write --commit-message MESSAGE`
5. Check it with `status --task TASK --id ID`, collect it with `claude-collect`, `grok-collect`, `antigravity-collect` or `codex-collect`, and close finished tabs with `tidy`.

Report the task ID and its current state. A collected result is not task acceptance. Finish here.

## Step 3 — Inspect current state

Read stored task, assignment and exception facts and relevant original evidence. Use current process observations for liveness. Distinguish completed processes, specialist findings, task acceptance and release readiness. Preserve unknown or stale evidence as unknown or stale; reconcile uncertain effects before proposing another attempt.

Do not claim that installing this skill started a service, launched agents or transferred existing work. Finish here.
