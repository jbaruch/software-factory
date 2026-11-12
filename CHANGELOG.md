# Changelog

## 0.37.0 — 2026-11-12

- Carry approvals over code a replay left alone (#121)

## 0.36.1 — 2026-11-12

- Fix three machinery failures from the ACR line (#119)
- docs(task-state): Add the required --line to the admit example (#118)

## 0.36.0 — 2026-11-12

- Give each factory line its own Rex session, lock and runner scope (#113)
- Realize coding-policy 0.9.0 rules (#112)

## 0.35.0 — 2026-11-12

- Make the release judgment check specialist coverage (#109)
- Let Claude and Grok reviewers run commands (#108)
- Decode unparsed tool input and ignore branch config in the commit guard (#110)
- Keep the main guard after every test class (#107)

## 0.34.0 — 2026-11-11

- The factory skill tells the coordinator to keep every line busy: write the next phase's plan as soon as an assignment settles, launch admitted work when a worker frees, and triage the next items when admitted work runs out. Twice in one night the owner had to ask why the lines were idle: finished writers had no verification plans, and no next wave was admitted. The skill described each phase but never made the next one the coordinator's duty.
- Every Git call gets 60 seconds instead of 10, and a Git timeout now says so ("Git did not finish within N seconds (git VERB)") instead of "Cannot run Git". Starting the Codex sandbox alone took 1.9 to 6.3 seconds on the operator's Mac, and a plain `git rev-parse` timed out at 10 seconds during a loaded test run. A writer that completed 70 turns of work then failed its factory commit with "Cannot run Git" during the secret scan, its change staged but uncommitted.
- One `--from-writer` verification plan can cover several tasks whose writers share the ID, and it may include `security` readers. The canary ran seven lines as a round-robin of single-task runners, because only one runner holds the execution lock and a handoff plan covered one task; security needed a further plan after selection. Every task's writer is checked before any task's inputs move.
- Writers are told not to use `git stash`: every worktree of a repository shares one stash, so a concurrent writer could pop another's changes. A wave-1 writer stashed its change to prove its tests failed on the old code.
- `factory.py cancel --task TASK --id ID` stops a running native assignment. It sends Ctrl-C to the pane; each worker records the interrupt as `failed` ("Cancelled by an interrupt in its pane") and stops its native child, so `status`, `capture` and `reset` work as for any failed attempt. A live cancel of a Haiku reader left a `failed` receipt with that error, a pane exit of 1 and no Claude process. Two release judgments launched on a goal known to be wrong had to run about six minutes each (#90).
- Claude assignments run with `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, so Bash has no `run_in_background`. A wave-1 writer left a background watcher running past its result; Claude Code woke the session when the watcher stopped, the run ended with two results, and the worker refused it as a conflicting lifecycle. A live probe on Claude Code 2.1.322 showed the second init and result without the switch and a single result with it.
- The Claude decoder accepts Claude Code's synthetic user nudges (`isSynthetic: true`, text only), such as «Your response above was cut off mid-stream. Resume directly…» after an API cut-off. A review that finished with one approved result was refused as a conflicting lifecycle because its stream carried that nudge.
- A read-only verifier that returns a verdict without a single tool call is recorded as `failed` ("Returned a verdict without a single tool call…"), on every harness. Claude's `StructuredOutput` call carries the report, so it doesn't count. Release judgments are exempt, because their evidence is the frozen payload in the brief. Two Grok qa runs on a 92 KB brief thought for one turn and returned `approved`, then `blocked`, with only an "UNTESTED" placeholder; the first was accepted as an approval.
- A ready-plan refusal names the job it is about (`Job TASK/ID: …`), for every per-job check, and a commit subject over 72 characters is refused with its length. The canary had to find the two over-long subjects in a seven-job plan by hand.

## 0.33.0 — 2026-11-11

- Each assignment's Rex pane shows one readable line per event worth watching: tool calls with their path or command, failures, agent text and the final outcome. It used to echo every raw native event, so a pane was thousands of JSON lines nobody could follow. The retained `.jsonl` stream is unchanged.
- Antigravity keeps a report it wrote in its brain directory when it names the file without a directory. A Sonnet review's report was lost that way.

## 0.32.1 — 2026-11-11

- The Antigravity decoder accepts a result response that ends with the report glued straight onto the model's prose, with no newline, in a one-turn run. A Sonnet review failed collection with a valid `structured_output` because the response was 2204 characters of prose and then the report.
- A report file a reader writes in its disposable checkout and names in its artifacts is copied beside its receipt (`TASK.ID.HARNESS.artifacts/`) before the checkout is removed; files of the candidate it cites stay where they are. Briefs also say a reader's checkout is deleted after the run. An Antigravity review approved with only `review-retire-service-pins.md`, which went with its checkout.
- Every Grok assignment runs with `--deny mcp__*`. Grok reaches the operator's MCP servers through its `use_tool` meta-tool even under `--tools read_file`, so a read-only Grok reader could call homekit, browser or nanoclaw tools; a live probe on grok 1.0.77 confirmed the rule refuses those calls. The canary saw homekit tools listed in a Grok QA stream.
- `integrate` and `delivered` accept a `test-automation` approval from Claude, Grok or Antigravity, as verification plans already allowed, and a refused approval names its ID, role and outcome. The canary's `delivered --after tests` was refused with no hint which approval was wrong.
- Collection accepts a completed Claude run that reports more turns than its `--max-turns` budget. A Fable release judgment under the default 30 made 72 parallel Reads, finished normally and reported 74 turns, and the decoder refused it as an unsupported lifecycle. Claude Code enforces its own budget and reports `error_max_turns` when it hits it, which still refuses (#89).

## 0.32.0 — 2026-11-11

- Grok readers work in a disposable checkout of the candidate, like Codex and Antigravity readers. Grok loads the operator's MCP servers whatever `--tools` says, and Playwright's wrote `.playwright-mcp/` into a writer worktree that a Grok QA reader shared, which blocked every later assignment there (#85). Reader receipts are version 4 and name the checkout; version 2 receipts still collect.
- `factory.py reset` restores a checkout that a failed reader left dirty, as it does for a failed writer. The canary's worktree had no factory way back (#85).
- The Antigravity decoder accepts a response streamed as repeated ACTIVE updates of one step, and agy's own empty turn that asks again for the structured report after a prose answer (two turns; the result response is the prose and then the report line). A Claude Sonnet review on agy failed collection on both (#86).
- The ready-plan reference says any read-only Claude, Grok or Antigravity job can set `base` to get the diff in its evidence, not only code review. A canary Claude QA reader could not confirm scope because its evidence had no diff.
- A ready-plan job with a wrong field set names what is missing or unexpected, and reminds that read-only jobs set `commit_message` to null. The canary dropped `commit_message` from read-only jobs and the refusal didn't say which field was wrong. SKILL Step 7 says a verification plan covers one task, as the ready-plan reference already did.

## 0.31.0 — 2026-11-10

- `factory.py delivered` is the delivery step for repositories without `integrate`'s prerequisites (a `checks.yml` `verify` job and strict classic protection): the repository's release skill merges, then `delivered` re-checks the approvals, confirms the PR merged at exactly the head the release judgment approved, and records `TASK.RELEASE_ID.delivered.json`. Neither nanoclaw repository meets those prerequisites, so every canary delivery so far was merged by hand under an operator override (#77).
- Any harness can give the release judgment: Codex, Claude, Grok or Antigravity, on the strongest model available, at that model's maximum effort. `claude-start`, `grok-start` and `antigravity-start` take `--release-pr` and `--delivery` like `codex-start`, and a release judgment below maximum effort is refused at launch (Grok: when its worker starts). Codex was the only judge because release evidence was built before the other adapters, not by policy, so every delivery stopped whenever Codex ran out of quota (#77).
- Release helpers come from coding-policy's release skill as ACR or Tessl installs it (`.codex/skills/acr__jbaruch__coding-policy__release` or `.tessl/plugins/jbaruch/coding-policy/skills/release`). Both nanoclaw repositories install it through Tessl, so no release judgment could capture their PR feedback.
- The release judgment's brief no longer tells the judge the repository can't publish. That limit belongs to `integrate`, and the brief blocked repositories that publish through their release skill.
- The delivery reference says who deletes the writer branch: `release-worktree` removes only the factory-locked worktree, and the release skill's prune removes the merged branch afterwards. The canary found coding-policy's stop hook flagging the branch that `release-worktree` had kept.

## 0.30.0 — 2026-11-10

- `factory.py rebase-worktree --task TASK --onto COMMIT` refreshes a candidate whose base moved: it saves the change as a patch, moves the provisioned worktree onto the new base and selects it, and a writer replays the patch and resolves the conflicts. A canary branch conflicted with main's CHANGELOG and no step could refresh it, because writers cannot make merge commits.
- `factory.py release-worktree --task TASK` removes a task's provisioned worktree after delivery, keeping the branch. The factory locks the worktree, so the release skill's cleanup could not remove it after the first factory-delivered PR merged (#78).

## 0.29.2 — 2026-11-10

- A completed Antigravity run keeps a private copy of each report file it wrote in agy's brain directory, beside its receipt (`TASK.ID.antigravity.artifacts/`). A Gemini QA reader returned only a path into that directory, which agy does not retain, so an approval's evidence could vanish (#74).

## 0.29.1 — 2026-11-10

- The Antigravity decoder accepts the bare `error_message` steps agy 1.5.3 records when the agent carries on past an error. A Gemini QA reader that completed its run failed collection on one, the same family as #51.

## 0.29.0 — 2026-11-10

- An exhausted correction allowance escalates through the factory's roles instead of stopping at the operator: `architecture` diagnoses why the task isn't converging (overengineering first) and re-plans within the goal, the investigator gathers facts, a disagreement or exhausted investigation goes to the judge, and only the judge escalates to the operator. The default correction limit is 4 (#66).
- Every assignment brief says the structured report is the only output the factory collects, so each finding goes in its artifacts with path:line. A Claude QA reader, which can only read, returned `findings` with all of them in its final text and none in the report (#69).
- The factory commit also removes empty directories a writer leaves, which Git cannot track; nanoclaw's vitest leaves eight, and a writer committed and then failed the post-commit check (#68). That check's failure now says the checkout is not clean after the run instead of blaming the Git sandbox.
- `factory.py reset` recovers a writer that failed after its own factory commit: it keeps the commit, saves any leftovers as the patch, and says to select the commit with `inputs` and verify it with a plain plan.

## 0.28.0 — 2026-11-09

- The skill says what happens when the correction allowance runs out and names `extend-allowance`, which needs the operator's approval.
- The factory commit's secret scan covers what the writer adds (its added lines, changed paths, identities and commit message), not the whole tree. Both canary repositories hold test fixtures and documentation examples that match default Gitleaks rules, so every factory commit there was refused (#63). A refusal now names each finding's rule and path, redacted, and no longer blames the Git sandbox, twice.
- The writer contract tells every writer that its added lines are scanned with default rules and that `gitleaks:allow` is ignored. Claude writers now get the shared contract instead of their own copy.
- `factory.py reset` prints the next step, and the ready-plan reference says how to rerun a failed writer: a new ID on the same checkout, a goal that applies the patch first, an empty `after`, and one plan for several tasks because a second runner refuses while the first holds the execution lock.

## 0.27.0 — 2026-11-09

- Codex and Antigravity readers, which run with every tool, work in a disposable worktree of the candidate that the factory removes after the run. A Gemini QA reader that ran `npm ci` and `pytest` in a checkout it shared with a Grok review left caches that made the review uncollectable (#53). Antigravity reviewer receipts are version 3 and name the disposable checkout.
- The factory commit discards a writer's ignored outputs, such as test caches and bytecode, and lists them in `commit.discarded`, instead of refusing finished work.
- `factory.py reset --task TASK --id ID` recovers a failed writer's worktree: it saves the leftover edits as a patch beside the receipt and restores the candidate, so a new writer can run there and cite the patch.
- The skill answers what a coordinator had to guess: requirements IDs, model discovery, writing roles, the execution lock, `seconds`, correction plans and verifying a writer that was never collected.
- The Claude decoder accepts a `run_in_background` Bash task that `background_tasks_changed` announces one event before its `task_started`. The 0.24.1 fix required `task_started` first, so a completed 52-turn writer that ran its tests in the background failed collection.
- The Antigravity decoder accepts a tool step that ends in `ERROR` with a `tool_info.error` record (the agent saw the failure and carried on) and the empty `system_message` steps agy 1.5.3 records. A Gemini QA run that retried a relative-path read and finished `approved` failed collection on the error and would have failed again on the system step (#51).

## 0.26.0 — 2026-11-09

- The package ships an always-on rule, `factory-process`. In a project that installs the factory, implementation, review and delivery go through the `factory` skill's coordinator step, never by hand where a factory step exists; specialists the factory launches ignore it. A coordinator taking over a backlog had matched the single-writer recipe and skipped verification and integration (#46).
- The `factory` skill's Step 1 is the concrete coordinator procedure, commands in order: admit, inputs, provision, ready plan, `ready_runner.py`, `--from-writer` verification, corrections, delivery, and what to do when a step cannot run. The single-writer recipe is now a single launch that never completes a task. The ready-plan schema and the delivery prerequisites ship with the skill as references (#46).
- The skill's commands use the installed skill directory. They pointed at `skills/factory/runtime/`, which exists only in the factory's own checkout, not in a project that installs it.

## 0.25.0 — 2026-11-09

- Ready-plan jobs take optional `turns` (Claude and Grok, 1 to 1000) and `base` (the review base for Claude, Grok and Antigravity code reviews). A plan can now run a long writer and a code review instead of freezing 30 turns and having no way to express a review range; invalid values refuse before execution (#47).
- Antigravity jobs can run without an effort. `antigravity-start` without `--effort`, or a ready-plan job with `"effort": null`, runs `agy` without the flag, so Antigravity's Claude models, which refuse `--effort`, can start; Gemini jobs still pass it (#50).

## 0.24.1 — 2026-11-09

- The Claude decoder accepts `system/api_retry` and the task events Claude Code emits for long or backgrounded Bash calls (`tool_progress`, `task_started`, `task_updated`, `task_notification`, `background_tasks_changed`). A completed 100-turn writer failed collection on a transient API retry and would have failed again on its long Bash calls (#45).

## 0.24.0 — 2026-11-09

Includes the unpublished 0.23.0 changes below.

- Antigravity can review and write. `antigravity-start`/`antigravity-collect` run `agy` with `--dangerously-skip-permissions`, the brief on stdin as one stream-json message, and the existing strict decoder; writers go through the shared factory commit. The ready runner (`harness: antigravity`, `--antigravity`), integration approvals and the dashboard know it.

## 0.23.0 — Unpublished

- Grok can write. `grok-start --write --commit-message --sandbox` runs Grok with every built-in tool in its worktree, and the shared factory commit stages, scans, signs and commits a completed report. Writer receipts are version 3 and collection binds the produced candidate.
- The ready runner dispatches Grok jobs (`harness: grok`, `--grok`), collects them, and accepts a Grok writer for `--from-writer`. `grok-collect` now marks native collection like the other collectors.
- Integration accepts collected Grok `qa`, `security` and `code-review` approvals and treats Grok writers as contributors; before, any Grok assignment made a task unintegrable.
- One owner maps a command to its harness, evidence reader and collector (`factory.native_harness`); the runner and integration use it instead of their own checks. The writer contract text lives in `factory_commit.WRITER_CONTRACT`; Grok writers receive it through `--rules`, after a live Grok writer committed on its own without it.

## 0.22.0 — 2026-11-09

Includes the unpublished 0.20.0 and 0.21.0 changes below.

- Every native agent runs without approval prompts or a harness sandbox: Codex with `--dangerously-bypass-approvals-and-sandbox`, Claude and Grok with `--permission-mode bypassPermissions`. The boundary is structural: a worktree per writer, a factory-owned commit with a secret scan, and no push or merge authority for agents. This replaces Codex's read-only/workspace-write sandbox, Claude's `--restricted`/`acceptEdits`/`dontAsk` modes and its shell sandbox.
- Grok inspection runs again. The execution hold is gone, project trust is no longer required, and imported plugins, hooks, MCP and LSP servers are recorded rather than refused. The native `strict` sandbox blocked CLI startup on 1.0.77.
- The Claude decoder accepts the `system`/`ui_invalidate` notices that Claude Code 2.1.319 emits in bypass mode, including before `init`.
- The Claude decoder checks tool-call shapes instead of checkout paths, so reads, searches and edits outside the checkout no longer fail an attempt; the factory commit still takes only the worktree. The writer's `.claude/.cc-writes` cleanup is gone with the shell sandbox that created it.

## 0.21.0 — Unpublished

- The ready runner dispatches Claude as well as Codex. Plan jobs may name `harness: claude`; the runner builds Claude commands, collects Claude reports and accepts a Claude writer for `--from-writer`. The Codex quota gate applies only to Codex jobs, and `--claude` supplies the Claude binary.
- Claude can answer bounded Jev questions as the standby reasoner or the judge, with version-3 receipts that close decision-inbox items. `judge_models` in `config/coordination.json` now names one judge per harness; a Claude judge runs the configured model at effort `max`.

## 0.20.0 — Unpublished

- Claude writers get a `Bash` tool in Claude Code's sandbox, so they can run tests: writes stay in the checkout and temporary directories, network is refused, and commands cannot leave the sandbox or run in the background. Sandbox network refusals decode as inspection-only events.
- Accept Claude Code 2.1.319 or newer instead of exactly 2.1.319; the strict decoder still refuses unrecognized shapes.

## 0.19.0 — 2026-11-08

First published ACR release; it includes the unpublished 0.16.0–0.18.1 changes below.

- `acr install github:jbaruch/software-factory` and `acr realize` now install the factory itself: the application moved into the `factory` skill as `skills/factory/runtime/`, with its configuration, dashboard assets and scanner pin.
- A project's installed copy installs itself as the one per-user runtime in `~/.local/share/software-factory/skill` and runs from there, so tasks keep stable worker paths across projects. The runtime is upgraded in place to the newest version any project brings and never downgraded; a source checkout runs in place.
- Task state defaults to the per-user `~/.local/state/software-factory` instead of a directory beside the application.
- The factory skill gains a hand-off step that drives the bundled CLI: admit, select inputs, provision a writer worktree, start a Claude or Codex writer, collect and tidy.
- Installed copies never write bytecode, and `scripts/check_design.py` keeps the runtime `VERSION` and Gitleaks pin equal to the repository's.

## 0.18.1 — Unpublished

- Accept relative Glob/Grep paths from Claude writers, resolved against the checkout, instead of failing the attempt after its edits; refuse Glob patterns and Grep file filters that escape the checkout.

## 0.18.0 — Unpublished

- Claude can write code. `claude-start --write --commit-message M --sandbox /path/to/codex` runs a writing role with `Read`, `Edit`, `Write`, `Glob` and `Grep` confined to its checkout, then the shared `factory_commit.py` step stages, scans, runs hooks, signs and commits the result. Writers produce version-2 receipts that collection validates like any factory commit.
- Choose Claude's model and effort per assignment from the discovered catalog (`low` through `max`) instead of the hardcoded `claude-sonnet-5-5`/`low` pin.
- Move factory-owned commits out of the Codex worker into `factory_commit.py`, which confines Git with the Codex CLI's local `sandbox` command as a tool rather than as the writing agent.

## 0.17.1 — Unpublished

- Answer `status` and `capture` of tidied tabs and abandoned launches from the task record without contacting Rex, even while the server is down.
- Refuse only unrecorded windows labeled `<catalog role>-<assignment>-<8 hex>` as lost history; operator tab names such as `notes-20261108` no longer block a task.
- When tidying after a successful collect fails, still print the saved result, report the tidy error and exit 1.

## 0.17.0 — Unpublished

- Close finished Rex tabs automatically. Exited, failed and lost assignment tabs keep their final process observation and terminal output in the task record, then close; `status` and `capture` answer from that retained copy. The ready runner tidies every tick, provider collect commands close their own tab, `factory.py tidy` sweeps a task or state directory, and an empty task session is killed so the next launch opens a fresh one.
- Report `lost` instead of `running` after a Rex server restart, which kills every pane process and restores the panes as fresh shells. New assignments pin the server instance (task schema 10); lost is terminal, frees runner slots, reaches integration's read-only recovery and lets collection defer to the worker's completed receipt.
- Tolerate operator tab changes. Splitting, renaming, moving or closing factory panes, adding tabs to a factory session or killing it no longer blocks the task; a missing pane is `lost` for that assignment only.
- Add `factory.py abandon` for launches Rex rejected outright, after confirming no pane carries the launch marker, so a pending launch can no longer wedge a task.
- Decode Rex replies with the strict JSON decoder used for every other factory input.

## 0.16.1 — Unpublished

- Decode observed Claude 2.1.319 `system/thinking_tokens` nonnegative integer estimates as raw inspection metadata. Preserve strict lifecycle/collection checks and final actual usage; failed original assignments remain failed. Add fixed offline decoder and fake worker/collection regressions.

## 0.16.0 — Unpublished

- Add explicit read-only native Claude execution/strict collection, original session-bound evidence, requested Team metadata and independent ordinary specialist approvals. Codex release behavior stays binding; managed live qualification remains separate.

## 0.15.0 — 2026-11-08 (unpublished)

### Source-derived Decision Inbox and human allowance control

Add four owner queues to the authenticated loopback view, using task/Jev owners
and optional `--decision-store`. Keep original shadow proposals, current native
answers, current task reports, holds and exhausted allowances distinct. Show
original references/options/revisions without exposing classifier state or native
commands. Unconfigured, damaged, stale, unbound, conflicting and unavailable
evidence remain explicit; judging requires the original exact current completed
investigation. No automatic dispatch, semantic finding routing or acceptance.

Distinguish zero authorization with no attempts from failed correction loops.
Require current failure evidence and relevant attempted corrections before
proposing investigation; respect exhausted allowance, blocked/pending work and
existing successful investigation evidence. Detect Jev/task source changes during
collection and report unavailable instead of mixing empty/pending/current views.
Close the owner handoff race identified by investigator `investigate-owner-read`
at `a42524e` and retained shadow decision
`726bf3919446bd91178306280e9662327d498a1069b0ad016a3d86eeb663dd0f`:
Jev now uses the existing no-follow/nonblocking regular-file owner reader after
fingerprinting. Preserve schema validation and final consistency checks; reject
replacement/new FIFOs and FIFO links without partial proposals or resolutions.
The shadow result is diagnostic evidence, not approval or execution authority.

An authenticated same-origin valid-CSRF operator form can approve exactly one
additional correction/investigation attempt through `factory.extend_allowance`,
with bounded reason, observed limit and replay-safe request ID. Preserve original
admission, usage, reports, holds and decision history. Static renderers remain
read-only; transient dashboard facts advance to schema 4; the existing task owner defines the task schema.
Fixed offline source/render/handler/concurrency/replay cases use isolated records
and fake transports. Host/browser checks, independent QA/security, policy review
and CI remain separate gates. Default counts are progress-review checkpoints; foreman extensions may use
existing standing authorization while user budgets, scope and holds bind.
No real held task was granted an attempt. No
publisher is configured or authorized.

## 0.14.3 — 2026-11-08 (unpublished)

### Public CLI reconnect after worktree cleanup

Reconnect existing integrations through their frozen assignment and original Rex
pane after checkout removal, without new launch validation or resending effects.
Keep first-launch checks and original intent, brief and pane validation intact.
Exercise public CLI acceptance replay and lost-launch recovery with collected native
fixtures and temporary Git; preserve original evidence and cumulative allowances.
Replace stale secondary task-schema literals with links to the owner schema.

## 0.14.2 — 2026-11-07 (unpublished)

### Acceptance replay after worktree cleanup

Replay complete collected native evidence against the admitted repository and
retained Git objects after normal worktree removal. Keep first collection strict,
original artifacts immutable, and input currency and final acceptance gates intact.
Cover linked-worktree delivery/acceptance replay, missing first-collection checkouts,
damaged results/provenance and changed repository identity with temporary Git and
fake native transports. Schema 9 records one per-assignment strict native-collection
flag; manual results cannot authorize historical replay. Older records migrate
without collection proof and need explicit strict recollection with the original
checkout still available; removed legacy checkouts cannot be backfilled.

## 0.14.1 — 2026-11-07 (unpublished)

### Progress-based fix-loop protection

Clarify [allowance review checkpoints](docs/task-state.md#review-checkpoints-and-allowance-extensions)
and link them from the [finite-loop design](docs/design.md#exceptions-and-finite-loops).
The external foreman may record evidence-backed extensions through the existing
owner API under standing authorization, preserving admission, usage and extension
history. Repeated failures without new evidence require investigation or a changed
approach; substantive disputes retain investigator -> Jev reassessment -> judge.
User budgets, scope, holds and uncertain effects remain binding. This documentation
clarification adds no runtime mechanism or automatic renewal.

## 0.14.0 — 2026-11-07 (unpublished)

### Exact-candidate integration and original release evidence

Add `factory.py integrate`, a mechanical head-conditional merge adapter and
frozen original canonical feedback for selected native specialist/release
judgments. Run the original mechanical adapter through `factory.launch` with real
Rex pane identities and a version-1 receipt; retain ordinary schema-8 readers.
Release task/ready-work locks during watcher waits, serialize repository integration,
reload pre-effect controls, and collect into current owner state. Reuse intents,
results, holds and cumulative attempts. Require the full review packet only for
release judgment, preserving ordinary exact-input QA/security approvals and failed
writers' original contributor identities. Version frozen payloads and support the
protected ignored managed `.codex` helper installation through canonical bash.
Require unchanged canonical watcher readiness, original exact-input independent
approvals, actual repository gates, source-bound merge ancestry, terminal current-
attempt push CI and issue checks. Replays reconcile read-only without resending
unknown effects. Final acceptance requires a separate native delivered-evidence
judgment. Keep PR preparation and cleanup with release, add no automatic quality
waiver or publication claim. Fixed offline transports and temporary Git cover
refusals, lost replies and recovery; live delivery and judgment remain unqualified.

Preserve collected successful writer provenance across subsequent corrections.
Move release/delivery capture outside execution/task locks and revalidate the owner
before submission. Freeze fresh successful CI rerun observations into version-2
native delivery briefs while retaining original receipts/results. Reject helpers
from sibling worktrees and newly selected checkouts before execution, remove
exported Bash functions, and make process failures actionable. Require verified
strict required `verify` CI protection with administrator enforcement before the
head-conditional merge; unsafe/unknown server guards hold without mutation. Offline
race and recovery fixtures do not establish live enforcement or release readiness.
Malformed helper JSON or text now reports an actionable restoration and same-intent
reconciliation diagnostic without exposing the response body; a pre-effect hold
still requires reassessment, and post-submission recovery never resends the merge.
Keep full release/delivery evidence in the existing brief with digest-only command
arguments, and pass native prompts through private temporary stdin. Preserve legacy
inline evidence. Check GitHub issue links directly with a versioned factory-owned
checker; refuse incomplete or unsupported observations without invoking an
undocumented cross-plugin issue-helper interface.
Classify native nonzero HTTP 405/409 merge responses as rejected, while preserving
unknown outcomes and reconciliation for transport or malformed-response failures.
Finish interrupted pre-submission waits as held, and allow unrelated assignment
and allowance updates during the watcher while retaining selected-evidence guards.


## 0.13.0 — 2026-11-07 (unpublished)

### Task-owned writer worktrees

Provision one explicitly named writer branch and path from an admitted task's
selected candidate. Save the immutable request and effect intent in the existing
task store before Git mutation. Create independent regular files from raw blobs
without checkout hooks or filters, preserving existing source checkout edits.
Reuse completed requests after lost replies; reconcile pending effects only when
the uniquely owned checkout is already complete and clean. Retain missing,
partial, dirty or conflicting outcomes for inspection without retries or repair.
Reject unsupported trees/configuration and destinations overlapping the task store.
Schema 8 preserves legacy holds, inputs, provisioning intents and allowance
additions from the earlier schema-6/schema-7 owners, without adopting worktrees
or granting attempts. Expose provisioning through the existing owner view. No automatic
candidate handoff, task acceptance or release authority is added.

Preserve CR/LF in UTF-8 Git filenames. Probe file and directory names on the
destination filesystem before saving intent, refusing case/Unicode aliases
without creating a branch or worktree. Reject physical file aliases in source
validation so an earlier partial checkout cannot be certified as complete.
Reject ancestor/descendant branch namespace conflicts before intent, and report
actionable recovery for missing or unreadable ownership metadata without mutation.


## 0.12.1 — 2026-11-07 (unpublished)

### Native Codex shell startup

New model children use strict per-run configuration to disable login shells,
shell snapshots and profile loading, with an identical private empty ZDOTDIR in
the native environment and TOML override. Retain the directory through confirmed
child exit and finish cleanup before factory Git work. Preserve failed receipts
and earlier errors when allocation, execution or cleanup fails; unconfirmed
termination retains the owned directory for reconciliation, without retry.
Native auth locations, permissions, other providers, factory Git environments and
historical receipt/reconnect contracts remain unchanged. Fixed offline regressions
cover controls, isolation, lifetime and failure paths. The canonical inventory
records version/worktree compatibility and outstanding independent review, host
checks and explicit owned native qualification. No publication or broader
readiness claim is authorized.

## 0.12.0 — 2026-11-07 (unpublished)

### Sanitized native Claude auth metadata

Add an independent stdlib adapter for fixed native version/auth-status commands,
strict allowlisted sign-in facts and explicit unknown eligibility/quota. Preserve
signed-out exit 1 as an observation and non-subscription auth facts without
claiming subscription access. Refuse environment overrides before launch, preserve
an explicitly selected native config directory without reading its contents, and
omit private native payloads/diagnostics. Fixed socket-free process tests cover
success and refusal paths. Require CLI 2.1.317 or newer before auth status to avoid
the upstream token-refresh persistence defect; fixed tests cover the floor,
older-version refusal and installed/newer versions. Renew compatibility review
when command/authentication contracts change, as documented in native providers.
Candidate live observation, independent QA/security,
host full tests and current-head policy/CI remain separate gates. No publisher is
configured or authorized; no route is activated.

## 0.11.0 — 2026-11-07 (unpublished)

### Retained Antigravity result decoding

Add a pure decoder for original 1.5.3 init/step_update/result JSON lines with
frozen model/cwd/schema, consistent conversation identity, ordered single-turn
lifecycle and strict terminal report/response agreement. Preserve nullable native
usage/duration as observations. Fixed offline fixtures cover integrity failures;
no execution, settings, stored formats or usage CLI change. Execution permission,
live lifecycle/full-factory qualification, independent verification and current-head
policy/CI remain separate gates. No publisher is configured or authorized.

## 0.10.0 — 2026-11-07 (unpublished)

### Team view and owned Rex pane reads

Show original specialist assignments, source/result freshness, supported native
worker request metadata and saved Rex identities in the authenticated operator
view. Keep requested model/effort/route separate from unknown observed model,
readiness and capacity. Link to a read-only pane detail that revalidates the saved
scope before process/output reads, with actual observation timestamps, escaped
terminal text and explicit partial-output markers. Preserve pause/resume and
task schema 7; transient dashboard facts advance to version 3 while older static
render callers remain compatible. Fake records/transports and socket-free
handler tests cover the addition; host/browser/live acceptance remains separate.
The application and package version advance together; no publication channel is configured.

Pane observations release the task lock before Rex waits, preserving concurrent
operator controls and their saved history without retargeting an in-progress read.
Native request metadata recognizes exact original-owner worker paths across
registered worktrees of the application repository, using read-only Git binding.
Fixed fake-Rex concurrency and cross-checkout owner tests cover these corrections;
direct-candidate host/live checks and independent re-review remain separate gates.

## 0.9.0 — 2026-11-07 (unpublished)

### Opt-in writer candidate handoff

Add `ready_runner.py --from-writer ASSIGNMENT_ID` for explicitly authorized
read-only independent verification plans. Validate the collected native Codex
writer against original receipts/events, its factory commit and live Rex status;
select its exact output with unchanged requirements through the existing task
owner. Preserve original plans/history, holds and cumulative allowances, and
reuse selected targets and existing verification launches on restart. Keep
schema versions, launch gates and acceptance/release authority unchanged.
Offline fake-transport tests with real temporary Git commits cover handoff,
replay and refusal paths; live integration and judgment quality remain separate.

## 0.8.0 — 2026-11-07

### Bounded native reasoner answers

Bind explicit read-only Codex standby/judge assignments to one retained uncertain
question. Freeze its original request/response in the existing brief and return
a selected answer or concrete missing input in the native report. Require the
exact current investigator result for post-investigation judging. Ordinary
reports stay unchanged; bounded reports use version-6 receipts. The offline inbox
can use collected current evidence to close only its matching question; missing
inputs, stale results, uncollected effects and conflicts remain operator-visible.
No extra resolution store, automatic dispatch, acceptance authority or retry
allowance is introduced.

## 0.7.0 — 2026-11-07

### Retained Jev decisions

Add optional private decision storage to shadow evaluations. Bind reuse to the
endpoint, versioned model, evidence revision, question versions and exact compiled
payload. Save intent before HTTP and retain original usage/timing; interrupted,
failed or malformed attempts never become implicit retries. Derive an offline
uncertainty inbox from those records, proposing standby reasoning for generic
uncertainty and judging after unresolved investigation. Keep consequential
routing, reasoner dispatch, recovery authorization and task acceptance separate.

## 0.6.0 — 2026-11-07

### Explicit read-only Grok execution

Keep this route held before native inspection, metadata or model children start.
Refuse new assignments without consuming allowance and refuse new result collection;
preserve existing effects and raw artifacts. The native sandbox does not confine
reads to the assigned checkout, and mutable executable configuration is reloaded
after inspection. Offline protocol tests and historical fixtures do not qualify
that boundary. No runtime override bypasses the hold.

Add `grok-start` and `grok-collect` with frozen native sessions, brief/repository
bindings, bounded seconds/turns, retained original event lines and version-1
receipts. Verify cached OIDC subscription metadata and advertised model/effort
before a model turn; retain that observation and its probe duration. Filter host
credential environment variables from metadata and worker children. Decode only
the final structured report, preserving native usage and model labels separately
from quota and spend. Collection verifies original events and current repository/
worktree identity without accepting a task. Existing effects reconnect unchanged;
failed or uncertain attempts never retry themselves. Automatic Grok quota-aware
scheduling and writer mode remain incomplete. Preserve the selected `GROK_HOME`
profile and refuse native authentication/model/endpoint overrides before a model
turn. Retain only inspected configuration paths/hashes in version-2 execution
receipts; earlier execution receipts remain unchanged and inspection-only.
Require native project trust and no executable customizations before metadata
agents start. Restrict model tools to file reads with overriding denies and the
strict sandbox; refuse inherited route/profile environment overrides. Earlier
configuration-1 evidence remains inspection-only under the tightened reader.
Validate nested configuration layers, CLI identity and timing, and reuse the
native discovery validation at both execution and collection boundaries.
Supply candidate path inventories in the existing frozen brief and require an
explicit base for code reviews, with revision-bound changed paths and full diffs.
Use native prompt-file input; keep older reviews without a range inspection-only.

## 0.5.0 — 2026-11-07

### Operator allowance additions

Add `extend-allowance` for explicitly approved correction/investigation additions.
Keep original admission limits and assignment history unchanged; derive effective
limits from ordered, reasoned additions in the task record. Require a request ID
and expected current limit. Replaying a saved decision grants nothing twice and
preserves subsequent decisions. Task schema 7 migrates supported earlier records
with no invented grants; schema 6 provisioning-prototype records remain held for
their compatible owner. Holds, task acceptance and execution are unchanged.

## 0.4.0 — 2026-11-07

### Native Antigravity quota observations

Read the native CLI version and standalone `/usage` report without a model turn.
Retain original group labels, weekly percentages/reset times and collection
duration in version-1 observations. Refuse malformed reports and API/ADC overrides;
keep worker readiness and model-to-pool binding separate. Record the bounded
Gemini fixture and current signed Codex writer/controller evidence.

## 0.3.0 — 2026-11-06

### Explicit ready-work execution

Run preselected Codex assignments from a versioned plan, collect finished native
reports and start current successful dependencies without a foreman turn. Reuse
the task store's intents, holds and cumulative limits across controller restarts.
Bound instance workers/tasks, preserve verification/recovery slots, share starts
across tasks and gate new launches on fresh native quota/model facts. Keep unknown
processes reserved and failed attempts unretried. Serialize controllers/manual
launch commands while leaving observation and operator controls available.

Reuse the freshly resolved common Git directory throughout each isolated phase's
permission, private-hook-view and signing-path setup to reduce repeated native
sandbox launches.

## 0.2.0 — 2026-11-06

### Factory-owned writer commits

Let explicit `--write --commit-message` assignments leave Git staging/commit to
the runtime. Retain before-effect intent and staged tree in version-5 receipts,
respect configured hooks/filters/signing and preserve failures without retries.
Materialize shared regular output files before Git without changing their aliases.
The operator-approved exception permits those helpers to load edited source.
Run Git and final checkout checks in a local-only native sandbox, with restricted
filesystem access and no publication-token environment or host fallback. Native
model sandbox permissions remain unchanged. Collect
valid version-4 receipts without rewriting them. Produced commits still require
independent verification and the repository's review/CI gates.
Keep all launch guards callback-free by comparing the index and raw candidate
bytes, and read only the selected Codex executable/required vendor files.
Respect explicit catalog writer permissions before new launch effects and use
the macOS socket flag only on macOS; historical reconnects remain unchanged.
Separate pre-commit into a private Git view with sanitized configuration, a
disposable index and no signing socket. Deny original configuration and shared
metadata; enforce writer roles inside the worker before execution. Stage/commit
without callbacks; unsupported hook/filter/signer configurations fail visibly
without bypassing checks or signatures.
Require an offline Gitleaks scan of staged paths, raw blobs and commit messages
even without a hook; refuse
missing scanners, scan errors and findings before signing or committing.
Recheck the retained checkout's repository marker and exact Git directory during
collection, including legacy version-4 receipts. Bind every writer candidate to
its retained worktree HEAD, including writers without a factory commit.
Verify the scanner's pinned version before a native writer starts, including
embedded Go module metadata for unstamped builds. Structure the factory skill
as separate coordinate and inspect actions.
Scan effective author and committer identities resolved under the final commit's
Git configuration and environment before copying inputs into the private scan view.
Refuse in-progress Git operations before native launch and staging; a clean
resolved cherry-pick cannot supply an unscanned author. Use UTF-8 for factory
commits without copying a configured encoding header. Cover the explicit Git
smoke command's orchestration and failure cleanup with offline fake transports.
Report malformed or unreadable SSH public keys with focused signing-recovery
guidance before starting the private signing agent.

## 0.1.0 — 2026-11-05

### Safe Codex capacity snapshot

Curate one native metadata probe into account type/plan, native allowance pools,
overlapping quota windows and actual model/effort IDs. Preserve unknown values,
deduplicate pool identities and refuse conflicting, ambiguous or malformed observations.
Omit account identifiers, raw initialization and billing/credit data. This exposes
metadata facts; it does not establish eligibility or automate scheduling.
Preserve per-pool reached/spend-control status and refuse unsupported individual
spending limits rather than presenting window headroom without its constraints.

### Native Grok metadata

Observe cached native sign-in, access gates, model IDs and supported effort
choices through an owned ACP child without requesting a model turn or creating
a conversation. Preserve unknown quota and readiness, omit account identifiers,
and disable automatic updates and worktree cleanup for the probe process.

### Local operator view

Generate a local dashboard from existing task records and explicit Rex process
observations. Show holds, cumulative allowances, current/stale reports, evidence
references and unknown provider capacity without adding a second datastore or
automatic polling. Keep process exit, specialist reports and task acceptance
distinct. Serve rendered HTML and source facts on loopback with no new dependencies.
Require a random per-process capability before reading state, keeping credentials
in a private temporary file rather than startup output. Reject invalid ports
before opening a listener. Consolidate implementation coverage in one inventory.
Keep timed-out Rex connection/process observations unavailable without aborting
the view request or changing task evidence.
Display lone surrogate code points as visible escapes so one accepted record
cannot break the HTML response; preserve the original source and API values.
Add authenticated pause/resume forms with visible hold/history feedback and no
client dependencies. Keep control requests in task schema 5, preserving legacy
holds without invented events. Stable request IDs prevent a repeated submission
from overwriting later decisions; stale forms refuse under the existing task lock.
Require same-page CSRF credentials and bound form inputs before reading task data.
Serve control history in dashboard snapshot version 2.

### Adapter input and evidence validation

Bound native execution allowances before state reads or attempts, reject oversized
integers throughout provider/event decoding and require numeric protocol response
identities. Preserve actionable recovery without consuming attempts on bad input.

Reject blank native selections before reserving attempts, damaged saved worker
arguments and incomplete completed receipts before collection. Validate finite
question catalogs, option criteria and native model identities before use, and
retain actionable process/stream I/O recovery diagnostics in failed receipts.
Validate selections at the public CLI before legacy state upgrades, enforce
ordered native thread/turn/report events, and reject ambiguous provider JSON or
blank model identities before recording discovery or shadow evidence.
Reject stateless path/allowance errors before legacy state reads. Freeze native
worktree identity in commands and version-4 receipts; older receipts retain their
history for inspection. Reject nonstandard/non-finite numbers throughout provider
and event decoding with the existing recovery diagnostics.

### Jev shadow adapter

Compile factory Choice questions into the TypeSafe HTTP contract, expand actual
candidate IDs, discover account models and perform explicit shadow evaluation.
Preserve question/evidence binding, uncertainty, model identity and observed usage.
Service failures retain actionable diagnostics without automatic retry or exposed
credentials. Live routing, calibration, durable queues and caching remain separate.
Reject Choice answers that contradict their distributions and distinguish request
repair from account/service recovery in HTTP diagnostics.
Read runtime credentials from the environment, and identify damaged state/candidate
input files with repair guidance before making any network request.
Validate input shapes and refuse non-standard numbers or ambiguous duplicate keys.
Name question-catalog access, encoding and shape repairs before provider access.

### Native model catalogs

Expose native Grok and Antigravity model IDs through metadata-only CLI probes.
Preserve observation time, CLI identity and source output while leaving worker
readiness, quota sharing and supported effort unverified. Refuse changed catalog
formats, duplicate IDs and command failures without automatic retries.

### Native Codex metadata

Read the signed-in native account, metered quota buckets and model/effort catalog
through an owned stdio app-server process. Preserve unknown fields and duplicate
quota views without treating discovery as model access or lifecycle verification.
Malformed protocol input gives inspection/update guidance and stops the owned probe.
Missing native binaries identify the executable path recovery before starting a child.
Handle closed metadata pipes and exit-before-kill races with recovery guidance
and observed child exit status.
Translate metadata-process startup failures into executable/resource recovery guidance.

### Native Codex results

Add explicit model/effort assignments through Rex with private native events,
before-start receipts and compact revision-bound result collection. Reject
altered briefs, unsuccessful or incomplete native runs and conflicting evidence.
Retain uncertain attempts, observed thread identity and child exit status for
inspection instead of rerunning them. Offline
tests cover completion, timeout and collection; live execution remains unverified.
Version local event records while preserving original native lines. Check the
native executable before reserving a new attempt, preserving reconnect behavior.
Reject damaged or mistyped receipts with actionable recovery diagnostics before
registering any result.
Freeze the resolved native CLI, require the owned worker/interpreter during
collection, and preserve malformed native bytes with inspection/update guidance.
Identify damaged preflight inputs and reject duplicate receipt/event keys before
registering results.
Retain observed web-search telemetry with repeated item IDs while discarding the
ambiguous IDs from derived observations. Keep result, lifecycle and local-envelope
fields strict; failed attempts remain failed and their original streams unchanged.
Carry the admitted repository identity into worker preflight/postflight checks and
version-3 receipts using the Git-local marker. Preserve legacy command reconnection and inspection without
inventing a historical pin or accepting older receipts as new evidence. Execute
the native child in the inherited, verified directory rather than reopening the
saved pathname. Preserve final receipts when cleanup races with child exit, and
name recovery for a CLI that disappears before process creation.
Support native event lines up to 1 MiB and retain a failed attempt with actionable
guidance for larger output. Keep native assignments as leaf workers, isolate new
host workers from Python startup overrides and prevent wrapper/worker bytecode
artifacts. Preserve historical command reconnection and receipt collection.

### Operator-approved runtime and review exceptions

Authorize native provider assignments through the factory's Rex adapter for
runtime and conformance work, retaining task scope, isolated worktrees, budgets
and permissions. Record the operator's temporary Claude-review waiver during
the subscription outage expected to end Saturday afternoon, November 7. Policy
review and CI remain required; restore the Claude gate on capacity recovery.

### Task admission

Refuse linked/non-regular task records and require owner-private, single-link
repository markers. Resolve verifier Git outside the inherited executable path
and disable Python environment/user-site startup in the exec guard, keeping
unverified checkout bytes from running before source checks.

Add task scope, acceptance references and an initial Git revision to the native
launcher. Admitted tasks enforce declared cumulative correction and investigation
limits from persisted launch intents. Add pause/resume, offline state inspection
and an owner-managed upgrade for legacy task records. Automatic scheduling and
provider integration remain outside this slice.

### Revision-bound results

Bind new admitted assignments and specialist reports to candidate and requirements
revisions. Preserve stale results as history and require current successful results
for declared launch dependencies. Completed implementation can return a produced
commit for independent review. Existing assignments gain no invented evidence
during the version 2 state upgrade. Verify marker identity around revision reads
and check Rex's actual working directory in a small exec wrapper before running
a new admitted command, closing replacement gaps around persistence and spawn.
Refuse damaged schema downgrades and incomplete request/evidence identities
before they can erase history or authorize dependent launches. Unknown assignment
diagnostics direct the operator to the recorded IDs.
Keep surviving Rex IDs from masking missing attempt history, verify the live
session identity before reuse and explain recovery for unreadable task/repo inputs.
Validate admission/input consistency, investigation purposes, dependency ordering
and live pane identities. Include admitted scope and acceptance references in new
briefs, preserving frozen instructions on reconnect.
Keep valid task/assignment ID combinations from overwriting another task's brief.
Reject duplicate JSON keys, pending pane IDs and aliased assignment identities.
Require saved assignment history to match the live session and diagnose malformed
Rex responses with compatibility/recovery guidance.
Verify the admitted repository pin on repository-backed mutations, retain pending
intent when returned Rex IDs disagree with the live pane, and explain Git process
failures and session/history mismatch recovery.
Bind session/window marker formats to their task and assignment owners, and
allow explicit admission limits when configuration defaults are unavailable.
Clear ambient Git overrides during verification and refuse ignored worktree
overlays, keeping revision-bound inputs tied to the selected commit. Decode
malformed Rex process bytes at the transport boundary with compatibility guidance.
Clear Git overrides in the launched command as well as verification. Bind Git
inspection to the actual worktree, reject redirecting worktree configuration and
hidden index flags, refuse submodules and enabled fsmonitor, and explain malformed
Git output. Preserve whitespace in configured worktree paths.
Preserve environment-supplied author/committer identity for writer commands and
identify pinned Rex availability failures with restoration/reconciliation guidance.
Compare tracked bytes and modes with the candidate tree even when Git stat hints
hide an edit. Preserve command SSH, credential, TLS and prompt settings while
removing Git routing overrides. Pin the CI Python patch with a monthly renewal.
Disable replacement objects and the untracked cache during source verification.
Disable replacement objects in child commands too, and pin the launch wrapper
to its exact worktree Git directory rather than only the shared repository.
Reject empty untracked directories by checking the actual candidate layout.
Preserve inherited authentication configuration while refusing source-routing
options. Refuse source symlinks and shared hard links to keep inputs and writes
inside an independent checkout.
Pin new admissions to an immutable Git-local repository marker in task schema
version 4. Ordinary clones do not inherit the marker, and recycled filesystem
inodes cannot authorize a replacement repository. Preserve older history and
filesystem pins for inspection; require operator authorization for a new admission
before further repository-backed work. Pass the saved pin through launch checkout
validation so a replacement between the repository and checkout checks refuses
before saving an attempt or contacting Rex.

### CI and review

Add independent policy and quality review triggers, Python diagnostics, offline
tests, design checks and pinned ACR validation. Dependency updates run weekly;
the ACR binary pin has a monthly refresh procedure beside its download command.
Add a pinned Gitleaks pre-commit hook and document skill installation.
Dispatch reviews from a base-controlled workflow without checking out or executing
PR code, keeping the cross-repository token outside PR-controlled execution.
Dispatch again when PR metadata or its base changes.
Renew pinned pre-commit hooks through weekly Dependabot updates.
