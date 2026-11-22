---
name: factory
description: "Deliver changes through the software factory: take over, continue or hand off a task or backlog, work in the factory, coordinate specialist writers and independent verifiers through ready plans and delivery, launch one explicit assignment, or inspect a task's delivery and exception state."
---

# Factory entrypoint

This skill is an action router — pick the step that matches the user's intent and execute only that step. Do not run other steps; do not parallelize.

Implementing, reviewing or delivering a change, and taking over, continuing or handing off work, is Step 1. Task authority and repository coding/release requirements remain binding.

The application ships in this skill's `runtime/` directory. Set `SKILL_DIR` to this skill's directory, such as `.claude/skills/acr__jbaruch__software-factory__factory` in an installed project or `skills/factory` in the factory's own checkout. Below, `factory.py` means `python3 -B "$SKILL_DIR/runtime/factory.py"`. The factory and ready runner install or upgrade the per-user runtime on first use. Task state lives in `~/.local/state/software-factory`. Each command's input, output and failure contract is in [the command reference](references/commands.md).

For specialty selection or an assigned role, consult only the relevant entries in [the role catalog](references/roles.json). Each entry defines activation, capabilities, decision authority and focused instructions; it does not require a permanently occupied agent.

## Step 1 — Coordinate a task

You coordinate; specialists do the work. Do not edit, commit, rebase, review or merge by hand where a step below exists.

Keep every line busy. When an assignment settles, write its next phase's plan at once: verification after a writer, a correction or a dispute after findings, delivery after approvals. When a worker frees and admitted work remains, launch it; when the admitted work runs out, triage and admit the next authorized items. Writing the next plan is your job, so idle capacity while authorized work remains is your failure, not a wait state.

1. Establish the authorized goal and its acceptance references. Commit any work being handed over and note the commit ID; the task record is the handover, so write no separate document.
2. Admit the task and select its inputs:
   - `factory.py admit --task TASK --line LINE --goal GOAL --repo /absolute/repo --acceptance REFERENCE`, where the line is your stream of work, such as `nanoclaw`, the same for every task you coordinate. A line's agents share one execution lock and the Rex session named after the line; when your own Rex session carries that name (`rex session rename` sets it), their tabs open beside you. The line's runner sees only its own tasks, so other coordinators' lines run in parallel. Tag a task admitted before lines with `factory.py line --task TASK --line LINE` once nothing of it is running.
   - `factory.py inputs --task TASK --candidate COMMIT --requirements REQUIREMENTS-ID`, where the requirements ID is any stable revision, such as `owner/repo#N@<issue updatedAt>`. When acceptance references GitHub issues, `inputs` captures their text for readers, which have no GitHub identity; it needs the read-only token in the macOS Keychain (`software-factory-github-read`), or `SOFTWARE_FACTORY_GITHUB_READ_TOKEN` where there is no Keychain, and refuses without it
3. Select specialists from the role catalog: at least one writer and one independent verifier, plus a specialist for each effect the change has. Backend, qa and code-review cover none of these; the release judgment checks them:
   - owner-facing UI, such as messages, cards, buttons or wording: `ui-design` (and `accessibility` for assistive use)
   - persisted state, a schema or a migration: `data-engineering`
   - a trust boundary, credentials or actions on an account: `security`
   - deploy, boot or runtime behavior: `sre`
   - tests that pass on the old code or leave branches untested: a `test-automation` writer or verifier
   - a branch replayed onto a moved base, or a shared interface: `integration`
   - unclear acceptance: `product`; a design choice across components: `architecture`; backlog triage and decomposition: `foreman`

   Select each job's route with [ready-plan routing](references/ready-plan.md). A verifier uses an independent session. Pick the writing role by the change's discipline: code is backend or frontend, rules, skills and prose are documentation, interfaces are architecture. Discover Grok and Antigravity models with `python3 -B "$SKILL_DIR/runtime/native_catalog.py" --binary /absolute/cli grok|antigravity`; give Claude the full model ID Claude Code reports; the runner checks Codex models against its fresh catalog.
4. Provision the writer's worktree: `factory.py provision --task TASK --id WRITER_ID --path /absolute/new/worktree`.
5. Write the implementation plan per [the ready-plan reference](references/ready-plan.md): the writer job with `write: true`, a `commit_message`, the worktree as `cwd`, and `turns` for long Claude or Grok work.
6. Run the plan with the Controller terminals command in [the command reference](references/commands.md). Pass every installed harness and the endpoint from `rex whoami --json`. The runner result's `harnesses` says whether each one can take work: plan for those `available: true`, try an `available: null` harness when no `true` one can take the work (its windows, when present, are information, not a gate), skip `available: false`, and use a harness again the moment it turns available, without waiting to be told. Use a fresh log for each attempt until the runner returns `complete` or `needs_action`; the runner launches, collects and tidies by itself. A plan serves one line, and several plans can run on it at once. A runner holds the line's execution lock for each tick, never while it polls: the line's `provision`, `reset`, manual launches and other runners run between ticks and refuse with exit 75 only during one, which means retry later, not failure. A runner whose first tick finds the line busy exits 75 too; a later tick another holder has is skipped, so the budget holds. Stop it with a `--seconds` budget or `--once`, never by killing it mid-cycle.
   - Launch controllers in your existing terminal.
   - Pin the runner's Python interpreter for launches and resumes.
   - Check the launcher's exit status and JSON summary.
   - Read the runner result in the controller log.
   - Keep controller attempts out of Rex tabs.
   - Reuse at most one log monitor per line.
   - Own the direct Rex helper tabs you open.
   - Omit `--keep-open` by default.
   - Capture needed helper output before close.
   - Close your finished helpers before handoff or wrap-up.
   - Stop your log monitor when done or hand it over by ID.
   - Preserve unrelated monitors, factory worker tabs, user tabs and unrelated running work.
   - Follow the Rex helpers section of [the command reference](references/commands.md).
7. Verify the writer's commit: a plan of read-only verifier jobs (`qa`, `code-review` and the effect specialists from step 3) that depend on the writer, run with `--from-writer WRITER_ID`. Read the writer's diff against the step 3 list first, and add a specialist for each effect it has that step 3 didn't foresee; the release judgment otherwise finds the gap a round later. One plan can verify several tasks whose writers share that ID.
8. Route findings to the responsible specialist: a correction job (`purpose: correction`, writing role, the writer's worktree) in a new plan, then verify the correction with `--from-writer CORRECTION_ID`: the specialists whose findings it answers, the approvals delivery names with `--after`, and each effect specialist whose code it changed. The correction's goal cites the findings reports by path, and its `after` stays empty because findings never advance a dependency. Corrections consume the task's allowance, failed ones included; changing agents never resets it. At zero, launch refuses further corrections; don't ask the operator. Start an `architecture` assignment that diagnoses why the task isn't converging, overengineering first, and returns a simpler plan within the admitted goal or a supported reason it can't be met. Implement that plan as new `work` from the task's current candidate; it doesn't restore the correction allowance. The architect may call the `investigator` for facts (each call spends the investigation allowance). A disagreement with the architect goes to the `judge`, and an exhausted investigation allowance does too. A judge reporting `completed` has ruled: carry the ruling out without the operator. When it needs another correction round and the allowance is at zero, its ruling grants exactly one: `factory.py extend-allowance --task TASK --purpose correction --additional 1 --expected-limit CURRENT --request-id STABLE-ID --reason REASON --judge JUDGE_ID` (once per ruling, three per task). Only a judge reporting `blocked` escalates to the operator, for scope or risk as its report names. `extend-allowance` without `--judge` runs only with the operator's approval.
   - An assignment running on a brief known to be wrong: `factory.py cancel --task TASK --id ID` interrupts it, and its worker records it failed; reset a writer's checkout, then launch the replacement under a new ID.
   - A writer that fails after editing, or is lost when its host restarts, leaves its worktree dirty: `factory.py reset --task TASK --id WRITER_ID` saves the edits as a patch beside its receipt and restores the candidate. Plan a new writer whose goal cites the patch. A failed reader that left files in a shared checkout resets the same way; rerun it under a new ID. A writer that was never collected leaves no `--from-writer` source; select its commit with `factory.py inputs` and verify with a plain plan.
9. Deliver per [the delivery reference](references/delivery.md): push the selected candidate and open the PR yourself, run the release judgment on the strongest available model at maximum effort, do each out-of-repo prerequisite its `Pre-deploy:` artifact lists, then merge with `Skill(skill: "release", args: "existing-pr PR_URL")` and confirm with `factory.py delivered`. If the PR branch conflicts with a moved base, refresh it through the factory: `factory.py rebase-worktree --task TASK --onto BASE-COMMIT` saves the change as a patch and moves the worktree onto the base, then a writer (`purpose: work`) applies the patch with `git apply --3way`, resolves the conflicts and re-runs the gate. Verify the produced commit with `--from-writer`: `integration`, the approvals delivery names with `--after`, and each effect specialist whose code the resolution changed. An earlier approval still covers an effect whose code the replay left as it was; the release judgment checks that. After delivery, and any deploy, run the final acceptance judgment the delivery reference describes, before selecting other inputs: it judges the admitted acceptance against the confirmed delivery, including what the verifiers listed as not yet verifiable. Its findings become new work for the task; when it reports blocked on a normal path's next run or another task's delivery, run a new final acceptance once that has happened. After the merge, `factory.py release-worktree --task TASK` removes the provisioned worktree; the factory locks it, so ordinary cleanup cannot.

When a step cannot run, file the gap at jbaruch/software-factory with the evidence, do the minimum by hand, and tell the user which step was done by hand and why.

Scripts own deterministic execution and known transitions. Jev handles bounded semantic coordination; every question offers `uncertain`, and generic uncertainty goes to the standby reasoner. A substantive dispute follows investigator → Jev reassessment → resolution or judge, and a remaining specialist objection prevents automatic closure. Read artifacts by reference; do not rewrite reports, narrate rounds, keep duplicate ledgers or poll workers with model turns. Reconcile unknown external effects before retrying.

Report the task ID, its state and the PR. A collected result is not task acceptance. Finish here.

## Step 2 — Launch one assignment

Use this only for a single assignment a ready plan cannot express, such as a release judgment or a recovery launch. It never completes a task; return to Step 1 for the rest.

The first launch on a task also needs `--rex "$(command -v rex)" --server ENDPOINT`, before the subcommand.

- Claude: `factory.py claude-start --task TASK --id ID --role ROLE --purpose work --goal GOAL --cwd WORKTREE --claude /absolute/claude --model MODEL --effort EFFORT`
- Grok: `factory.py grok-start` with `--grok /absolute/grok` and the same task, role and model flags.
- Antigravity: `factory.py antigravity-start` with `--agy /absolute/agy`. Omit `--effort` for a model that refuses it, such as `claude-opus-4-6-thinking`.
- Codex: `factory.py codex-start` with `--codex /absolute/codex`.

Writers add `--write --commit-message MESSAGE`, plus `--sandbox /absolute/codex` outside Codex. A code review judges a range, so it runs on Claude, Grok or Antigravity with `--base COMMIT`; Codex takes no `--base` and refuses `--role code-review`. Check it with `factory.py status --task TASK --id ID`, collect it with the matching `*-collect` command, and close finished tabs with `factory.py tidy`; [the command reference](references/commands.md) gives each one's output. Finish here.

## Step 3 — Inspect current state

Read stored task, assignment and exception facts and relevant original evidence. Use current process observations for liveness. Distinguish completed processes, specialist findings, task acceptance and release readiness. Preserve unknown or stale evidence as unknown or stale; reconcile uncertain effects before proposing another attempt.

Do not claim that installing this skill started a service, launched agents or transferred existing work. Finish here.
