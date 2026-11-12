---
name: factory
description: "Deliver changes through the software factory: take over, continue or hand off a task or backlog, work in the factory, coordinate specialist writers and independent verifiers through ready plans and integration, launch one explicit assignment, or inspect a task's delivery and exception state."
---

# Factory entrypoint

This skill is an action router: pick the step that matches the user's intent and execute only that step. Implementing, reviewing or delivering a change, and taking over, continuing or handing off work, is Step 1. Task authority and repository coding/release requirements remain binding.

The application ships in this skill's `runtime/` directory. Set `SKILL_DIR` to this skill's directory, such as `.claude/skills/acr__jbaruch__software-factory__factory` in an installed project or `skills/factory` in the factory's own checkout. Below, `factory.py` means `python3 -B "$SKILL_DIR/runtime/factory.py"` and `ready_runner.py` means `python3 -B "$SKILL_DIR/runtime/ready_runner.py"`. Either installs or upgrades the per-user runtime on first use and runs from there; task state lives in `~/.local/state/software-factory`.

For specialty selection or an assigned role, consult only the relevant entries in [the role catalog](references/roles.json). Each entry defines activation, capabilities, decision authority and focused instructions; it does not require a permanently occupied agent.

## Step 1 — Coordinate a task

You coordinate; specialists do the work. Do not edit, commit, rebase, review or merge by hand where a step below exists.

Keep every line busy. When an assignment settles, write its next phase's plan at once: verification after a writer, a correction or a dispute after findings, delivery after approvals. When a worker frees and admitted work remains, launch it; when the admitted work runs out, triage and admit the next authorized items. Writing the next plan is your job, so idle capacity while authorized work remains is your failure, not a wait state.

1. Establish the authorized goal and its acceptance references. Commit any work being handed over and note the commit ID; the task record is the handover, so write no separate document.
2. Admit the task and select its inputs:
   - `factory.py admit --task TASK --line LINE --goal GOAL --repo /absolute/repo --acceptance REFERENCE`, where the line is your stream of work, such as `nanoclaw`, the same for every task you coordinate. A line's agents share one Rex session and one execution lock, and its runner sees only its own tasks, so other coordinators' lines run in parallel. Tag a task admitted before lines with `factory.py line --task TASK --line LINE` once nothing of it is running.
   - `factory.py inputs --task TASK --candidate COMMIT --requirements REQUIREMENTS-ID`, where the requirements ID is any stable revision, such as `owner/repo#N@<issue updatedAt>`
3. Select specialists from the role catalog: at least one writer and one independent verifier, plus a specialist for each effect the change has. Backend, qa and code-review cover none of these; the release judgment checks them:
   - owner-facing UI, such as messages, cards, buttons or wording: `ui-design` (and `accessibility` for assistive use)
   - persisted state, a schema or a migration: `data-engineering`
   - a trust boundary, credentials or actions on an account: `security`
   - deploy, boot or runtime behavior: `sre`
   - tests that pass on the old code or leave branches untested: a `test-automation` writer or verifier
   - a branch replayed onto a moved base, or a shared interface: `integration`
   - unclear acceptance: `product`; a design choice across components: `architecture`; backlog triage and decomposition: `foreman`

   Choose a harness, a discovered model and an effort per job; a verifier never reuses the writer's session. Pick the writing role by the change's discipline: code is backend or frontend, rules, skills and prose are documentation, interfaces are architecture. Discover Grok and Antigravity models with `python3 -B "$SKILL_DIR/runtime/native_catalog.py" --binary /absolute/cli grok|antigravity`; give Claude the full model ID Claude Code reports; the runner checks Codex models against its fresh catalog.
4. Provision the writer's worktree: `factory.py provision --task TASK --id WRITER_ID --path /absolute/new/worktree`.
5. Write the implementation plan per [the ready-plan reference](references/ready-plan.md): the writer job with `write: true`, a `commit_message`, the worktree as `cwd`, and `turns` for long Claude or Grok work.
6. Run it: `ready_runner.py --plan PLAN --codex /absolute/codex --rex "$(command -v rex)" --server ENDPOINT`, adding `--claude`, `--grok` or `--antigravity` for their jobs and taking the endpoint from `rex whoami --json`. Repeat the same command until it returns `complete` or `needs_action`; the runner launches, collects and tidies by itself. A plan serves one line. While it runs it holds that line's execution lock, so the line's `provision`, `reset`, manual launches and a second runner refuse with exit 75, which means retry later, not failure; stop it with a `--seconds` budget or `--once`, never by killing it mid-cycle.
7. Verify the writer's commit: a plan of read-only verifier jobs (`qa`, `code-review` and the effect specialists from step 3) that depend on the writer, run with `--from-writer WRITER_ID`. One plan can verify several tasks whose writers share that ID.
8. Route findings to the responsible specialist: a correction job (`purpose: correction`, writing role, the writer's worktree) in a new plan, then verify the correction with `--from-writer CORRECTION_ID`: the specialists whose findings it answers, the approvals delivery names with `--after`, and each effect specialist whose code it changed. The correction's goal cites the findings reports by path, and its `after` stays empty because findings never advance a dependency. Corrections consume the task's allowance, failed ones included; changing agents never resets it. At zero, launch refuses further corrections; don't ask the operator. Start an `architecture` assignment that diagnoses why the task isn't converging, overengineering first, and returns a simpler plan within the admitted goal or a supported reason it can't be met. Implement that plan as new `work` from the task's current candidate; it doesn't restore the correction allowance. The architect may call the `investigator` for facts (each call spends the investigation allowance). A disagreement with the architect goes to the `judge`, and an exhausted investigation allowance does too. Only the judge escalates to the operator, for example for a scope decision or `factory.py extend-allowance --task TASK --purpose correction --additional N --expected-limit CURRENT --request-id STABLE-ID --reason REASON`, which runs only with the operator's approval.
   - An assignment running on a brief known to be wrong: `factory.py cancel --task TASK --id ID` interrupts it, and its worker records it failed; reset a writer's checkout, then launch the replacement under a new ID.
   - A writer that fails after editing leaves its worktree dirty: `factory.py reset --task TASK --id WRITER_ID` saves the edits as a patch beside its receipt and restores the candidate. Plan a new writer whose goal cites the patch. A failed reader that left files in a shared checkout resets the same way; rerun it under a new ID. A writer that was never collected leaves no `--from-writer` source; select its commit with `factory.py inputs` and verify with a plain plan.
9. Deliver per [the delivery reference](references/delivery.md): open the PR through the release skill, run the release judgment on the strongest available model at maximum effort, then `factory.py integrate`, or, where the repository lacks integrate's prerequisites, merge through the release skill and confirm with `factory.py delivered`. If the PR branch conflicts with a moved base, refresh it through the factory: `factory.py rebase-worktree --task TASK --onto BASE-COMMIT` saves the change as a patch and moves the worktree onto the base, then a writer (`purpose: work`) applies the patch with `git apply --3way`, resolves the conflicts and re-runs the gate. Verify the produced commit with `--from-writer`: `integration`, the approvals delivery names with `--after`, and each effect specialist whose code the resolution changed. An earlier approval still covers an effect whose code the replay left as it was; the release judgment checks that. After the merge, `factory.py release-worktree --task TASK` removes the provisioned worktree; the factory locks it, so ordinary cleanup cannot.

When a step cannot run, file the gap at jbaruch/software-factory with the evidence, do the minimum by hand, and tell the user which step was done by hand and why.

Scripts own deterministic execution and known transitions. Jev handles bounded semantic coordination; every question offers `uncertain`, and generic uncertainty goes to the standby reasoner. A substantive dispute follows investigator → Jev reassessment → resolution or judge, and a remaining specialist objection prevents automatic closure. Read artifacts by reference; do not rewrite reports, narrate rounds, keep duplicate ledgers or poll workers with model turns. Reconcile unknown external effects before retrying.

Report the task ID, its state and the PR. A collected result is not task acceptance. Finish here.

## Step 2 — Launch one assignment

Use this only for a single assignment a ready plan cannot express, such as a release judgment or a recovery launch. It never completes a task; return to Step 1 for the rest.

The first launch on a task also needs `--rex "$(command -v rex)" --server ENDPOINT`.

- Claude: `factory.py claude-start --task TASK --id ID --role ROLE --purpose work --goal GOAL --cwd WORKTREE --claude /absolute/claude --model MODEL --effort EFFORT`
- Grok: `factory.py grok-start` with `--grok /absolute/grok` and the same task, role and model flags.
- Antigravity: `factory.py antigravity-start` with `--agy /absolute/agy`. Omit `--effort` for a model that refuses it, such as `claude-opus-4-6-thinking`.
- Codex: `factory.py codex-start` with `--codex /absolute/codex`.

Writers add `--write --commit-message MESSAGE`, plus `--sandbox /absolute/codex` outside Codex; code reviews add `--base COMMIT`. Check it with `factory.py status --task TASK --id ID`, collect it with the matching `*-collect` command, and close finished tabs with `factory.py tidy`. Finish here.

## Step 3 — Inspect current state

Read stored task, assignment and exception facts and relevant original evidence. Use current process observations for liveness. Distinguish completed processes, specialist findings, task acceptance and release readiness. Preserve unknown or stale evidence as unknown or stale; reconcile uncertain effects before proposing another attempt.

Do not claim that installing this skill started a service, launched agents or transferred existing work. Finish here.
