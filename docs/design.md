# Specialist software factory

Status: agreed design. See the [implementation inventory](implementation.md) for current coverage and verification limits. Numeric limits in configuration are initial tunable defaults, not measured optimums.

## Outcome and constraints

Deliver accepted product changes through a broad team of specialists while minimizing coordination delay, repeated context, avoidable model calls and manual operator work. Measure speed, quality, rework, resource use and intervention together. Busy agents and exhausted subscriptions are not delivery outcomes.

The factory uses scripts for known rules, Jev for bounded semantic judgments, specialist agents for engineering work, and the foreman for priority, decomposition and exceptional planning. It uses Rex sessions, tabs and panes to run and inspect native agent harnesses. A terminal status indicator is an observation, not proof that a task passed review.

## Team and authority

`skills/factory/references/roles.json` is the complete initial catalog. It includes product management, UX research, UI design, accessibility, architecture, frontend, backend, mobile, database/data engineering, security/privacy, QA strategy, test automation, performance, code review, SRE, release, technical writing, investigation, judging, coordination, observation and optimization.

Roles express responsibilities, abilities, relevant tools, prompts and decision rights. They are not permanently occupied seats, fixed models or mandatory stages. Select the necessary specialists from the actual task, effects and repository policy. A developer does not silently take over the QA, security or architecture responsibility. A verifier must be independent of the implementation it verifies.

Product establishes the intended outcome and acceptance criteria. Architecture identifies interfaces and useful independent slices. Relevant specialists own judgments in their disciplines. The foreman owns priorities, decomposition and unresolved planning changes. Investigator gathers evidence; judge adjudicates substantive cases that remain unresolved after investigation and reassessment. Actual user scope, operator holds and project release requirements remain binding.

The task is a shared durable record and a dependency graph of specialist assignments, not one multidisciplinary agent. Findings go directly to the responsible specialist with links to original evidence. An expensive foreman does not rewrite, read back and acknowledge every exchange. Clear specialist outcomes are routed by code without another classifier call.

## Package and application

One repository contains the application and its ACR package. The application ships inside the `factory` skill (`skills/factory/runtime/`), so `acr install` and `acr realize` install it into each project. Task state is per user, and task records freeze absolute worker paths, so a project's copy installs itself as the one per-user runtime (`~/.local/share/software-factory/skill`) and runs from there. That runtime is upgraded in place to the newest version any project brings and is never downgraded; a source checkout runs in place.

The application will own scheduling, provider adapters, Rex integration, durable task state, measurement and the dashboard. Worker hooks will emit bounded lifecycle events after their native harness semantics are verified. Native Claude Code and Codex ACR realization does not establish Grok or Antigravity compatibility. Those harnesses need explicit instruction delivery and lifecycle conformance tests. ACR is not the process supervisor or model broker.

The plugin ships one always-on rule, `rules/factory-process.md`, and no unimplemented hook stubs. It started with no global rules. That failed on 2026-11-09 (#46): asked to take over a nanoclaw backlog, a coordinator matched the request to the single-writer recipe, never opened the ready runner or integration, and hand-committed, hand-rebased and hand-opened PRs with no factory verification. The rule tells every agent in an installing project that the factory is the delivery process and points it at the skill's coordinator step; specialists the factory launches ignore it. Skills describe a discipline's work. Coordination algorithms live in code and versioned Jev questions, not a growing prose choreography interpreted on every turn.

## Admission and normal delivery

1. Establish the authorized goal, acceptance evidence, priority, affected repository, effects and budget. Clarify only information necessary to proceed.
2. Decompose useful independent deliverables and select roles. A known task recipe can execute directly; unfamiliar decomposition remains model work. Jev can select a recipe or identify missing coverage, not invent an arbitrary project plan from a closed choice.
3. Create isolated writer worktrees and explicit interface/dependency ownership. Review and testing get the exact candidate commit and relevant requirements. Concurrent writers never share one checkout.
4. Start eligible ready assignments within work-in-progress, provider and integration capacity. Reserve resources for verification, urgent work and recovery. Idle capacity without ready useful work stays idle.
5. Route specialist findings to implementation directly. Within-scope routine corrections consume the task's cumulative allowance; changing worker identity does not reset it. Verify the changed candidate independently.
6. Integrate through a merge queue, or a verified server-enforced strict up-to-date required-CI guard where a native queue is unavailable, and satisfy the project's actual review, test, publication and acceptance requirements. The guard must apply to administrators without bypass. Revalidate only evidence affected by changed inputs. Release never substitutes a green job or model claim for the required actual outcome.

No foreman turn is required to recognize that a dependency completed or to launch an already authorized next assignment. Dynamic scope decisions and unresolved technical conflicts still require reasoning.

## Facts, judgments and compact results

Store current task identity, goal/acceptance references, repository/worktree, candidate commit, dependencies, assignments, current results, remaining allowances, holds and any pending external effect. Keep original artifacts at stable references. The operational store and event history feed every view; agents do not maintain a second narrative ledger.

Scripts gather process identity, Git facts, CI results, actual launch outcomes and usage observations where supported. Specialists produce findings, decisions, evidence references and unresolved questions. A compact result needs only assignment identity, relevant revision, outcome and artifact references; capture identity and process metadata automatically. Do not require separate prose, duplicated JSON claims, multiple verdict markers and final chat markers for the same result.

Identity and revision binding protect concrete actions. Avoid proliferating overlapping owner stores, grant documents, schema generations and proof-of-acknowledgment gates. Recovery information should be generated by execution, not hand-authored by workers.

## Jev coordination

`config/coordination.json` defines narrow Choice questions with explicit criteria and an `uncertain` option in every question. Initial questions cover needed specialties (one independent question per specialty), task demand, eligible recipe, model fit, finding recipient, correction disposition, escalation category and post-investigation resolution.

Supply only relevant source state and current candidates. Deterministic eligibility, arithmetic, exact lookups, authority and side effects stay in scripts. Batch independent questions over the same state; sequence calls only when an answer changes necessary evidence. Cache by evidence revision, question/version and candidate set. Never reuse a judgment after its relevant evidence changes.

Generic `uncertain` results enter a standby reasoning queue with the original bounded question and sufficient evidence. The reasoner returns an answer or a concrete missing input; it does not become a second general-purpose foreman. Insufficient input, no eligible route and a service failure are explicit conditions, not confident guesses.

Probability thresholds are evaluated per question and consequence on real factory data. An explicit `uncertain` option does not catch confident mistakes. Audit a sample of accepted routing decisions, including missing specialist involvement, inappropriate model tiers and premature resolutions. Typed answers provide interface structure, not truth or authority.

## Investigation and judgment

The required sequence is: specialist disagreement or exhausted correction loop -> investigator -> Jev reassessment -> resolution or judge.

The investigator gathers the minimum evidence that could settle the issue: reproduction, actual behavior, requirements, alternatives, contradictory observations and decision consequences. Reuse original artifacts; do not commission another narrative history.

Jev asks whether the investigation resolves the exact outstanding question. A resolved outcome routes the evidence and supported resolution to the affected specialists. A remaining substantive objection prevents closure. `unresolved` or `uncertain` after investigation goes to the judge, rather than through an indefinite standby/investigation cycle. Stale or incomplete evidence is repaired before adjudication.

The judge runs on a highly capable eligible model at maximum supported thinking effort. It is expensive and reserved for genuinely unresolved cases. It issues a bounded decision with actionable consequences and any remaining missing authority. It does not diagnose provider outages, retry routine commands or waive independent verification. A ruling cannot grant effects outside user authority or silently restart exhausted budgets.

## Exceptions and finite loops

Known temporary failures get bounded scripted retry/backoff, an eligible alternative or a named wait condition. A timeout with an uncertain launch/merge outcome is reconciled before any retry. Workers and completed artifacts survive coordination-service outages; decisions requiring an unavailable classifier queue durably rather than flooding the standby model.

Every exception has a task, named owner, evidence references, attempts already made, next action and remaining allowance. Outcomes are resume with a specific fix, change approach, split, reassign, wait for a named dependency, stop, or request a concrete operator decision. Default correction/investigation counts are review checkpoints; apply the owner guide’s [progress review and authorized extension guidance](task-state.md#review-checkpoints-and-allowance-extensions). Substantive repeat failures trigger investigation. Unresolved adjudication or exhausted investigation requiring additional scope/resources reaches the operator with options. Independent tasks continue unless a real shared/global dependency or explicit global hold prevents them.

Operator requests are generated from existing state: what is blocked, what was tried and learned, the recommended action and the exact requested decision. The operator may prioritize, pause, cancel, authorize a proposed adjustment or stop the task. No fictitious pause or automatic unlimited attempt reset is permitted.

## Models, harnesses and subscription routes

The five initial routes are native Claude, native Codex, native Grok, Antigravity Gemini and Antigravity Claude. Their inclusion is a design requirement, not verified account access. `config/providers.json` starts with unknown model catalogs, quota boundaries, usage and readiness. Never infer that the two Antigravity allowances are independent, or that similarly named models expose the same tools in different harnesses.

Separate role, model, thinking effort, harness and allowance pool. Discover eligible actual models/tools and native authentication without inventing model names or replacing subscriptions with paid API use. Supply candidates to Jev, then enforce eligibility, task constraints, fresh capacity observations and quota reservations in scripts. Unknown or stale capacity is visible and prompts a bounded refresh; it is not treated as unlimited.

Use learned outcomes to rank suitable candidates and controlled rotation to explore alternatives. Preserve effective task context rather than switching models each turn. Record versioned selection inputs, measured quality, latency, rework and resource consumption. Keep a resource reserve for verification and urgent exceptions. Usage probes themselves count as coordination overhead.

## Integration and recovery

Bind review evidence to the inputs it assessed, including candidate commit and requirements revision. A changed input invalidates only dependent work; a new source commit must not inherit an old approval. Integration ownership covers shared interfaces, merge ordering, conflicts and required revalidation.
The release judgment alone consumes the complete frozen external-review packet;
ordinary exact-input specialist approvals remain valid when only review prose changes.
Failed native writers retain their original contributor identity for independence,
without promoting failed execution or blocked results into successful evidence.
Collected successful writers also retain their original provenance when a later
correction advances or cleanup removes the checkout. Collected evidence validates
against the admitted repository identity and retained candidate/parent/tree objects;
initial collection still requires the original checkout, Git directory and HEAD.
Schema-9 assignment `native_collected` records only successful strict native
collection; generic result registration and legacy migration never grant it.
Legacy evidence needs explicit strict recollection while its original checkout
remains available. Collection history does not relax task-input currency or final
acceptance gates.

The scheduler needs one durable effect identity and before-effect intent for each launch or external mutation. Reconnect/reconcile after restart and reuse completed effects. Do not claim exactly-once execution across unknown provider/network outcomes. See the [implementation inventory](implementation.md) for recovery coverage.

Explicit writer-worktree provisioning belongs to the existing task owner and its
locks. Save the selected-input request before Git mutation, retain a unique
worktree ownership marker, and reuse completed effects. Conflicting or incomplete
resources remain available for inspection; recovery never resets or deletes them.
Provisioning does not select a produced candidate or grant acceptance/release
authority. The [owner contract](task-state.md#writer-worktree-provisioning) defines
the supported tree/configuration inputs and conservative reconciliation.

Rex is the execution/view adapter. Use explicit host, session and block IDs, working directories and returned process identities. Keep the onboarding session unchanged. Live adapters must prove launch, readiness, delivery, interrupt and recovery against each harness. Terminal text is untrusted task data and cannot grant instructions, permissions or task acceptance.
Mechanical integration uses the same schema-9 `factory.launch` lifecycle and real
window/block identities. Its original worker owns a versioned execution receipt;
repeats and cold recovery reconcile existing effects read-only. A repository-scoped
mutex serializes integration without occupying unrelated ready-work capacity. Task
locks never span canonical watcher/network waits; controls and selected inputs are
reloaded immediately before effects and current owner state before result collection.
Release and delivery capture also run outside execution/task locks and revalidate
the owner before submission. Successful reruns of the same delivery CI run can be
recollected read-only. Fresh delivery observations freeze in a new final native
brief, preserving original receipts/results and requiring a new semantic judgment.

## Measurement and self-improvement

The observer collects operational facts automatically. The optimizer investigates opportunities and implements useful, authorized improvements using the same appropriate independent verification as other work. Neither is a mandatory delivery gate or a permanently reserved expensive seat. Improvement work has a configurable resource budget and must demonstrate benefit against product delivery.

Improve models/rotation, skills, prompts, task decomposition, context, tooling, subscriptions, workflow, reliability and coordination cost. Fold Jev evaluation into this program. Keep a versioned evaluation set, representative tasks and held-out cases; include routing failures and non-escalated sampled audits. Evaluate changed policies in shadow/replay or limited rollout before wider adoption. Detect regressions and restore the prior version. The improvement loop cannot edit away acceptance requirements, user authority or its own failure evidence.

Measure accepted delivery, cycle time, defects, rework, provider usage, worker waiting, foreman interventions and coordination overhead. Optimize comparable outcomes, not raw task counts, token minimization or 100% occupancy. Separate observed consumption, unknown consumption, subscription allowance and monetary spend. Instrumentation, classification, monitoring and dashboard operations have their own measured overhead.

## Acceptance boundary

The first milestone replays deterministic contracts from `evals/scenarios.json`. It covers current independent specialist approvals, invalidated evidence, explicit uncertainty, investigation filtering the judge, substantive objections, exhausted allowances, subscription routing constraints, and utilization/overhead accounting. It does not evaluate live Jev correctness or prove provider/hook integrations.

The live milestone must deliver one real multi-specialist change with quota-aware route selection, event-driven transitions, an operator exception, recovery and a dashboard derived from real events. Measure foreman interventions and coordination cost against comparable Herdr work before claiming an improvement.
