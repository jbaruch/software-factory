# Implementation inventory

This is the canonical inventory of current coverage and remaining work. The
[design](design.md) and [dashboard contract](dashboard.md) define the full outcome;
the command guides describe operation rather than maintain separate status lists.

## Current coverage

| Surface | Implemented | Evidence and limits |
| --- | --- | --- |
| Contracts and package | Agreed design, specialist role catalog, configuration, one ACR skill, reference decisions and recorded replay scenarios | Structural validation and deterministic tests; no live judgment-quality claim |
| Task runtime | Admission, cumulative attempt limits, holds, revision-bound reports, dependency launch guards, explicit Rex launches and limited interrupted-launch reconciliation | Fake-server tests and harmless live Rex conformance; process success never accepts a task |
| Ready-work runner | Explicit plan execution, terminal native result collection, dependency advancement, instance work limits, verification/recovery slots and fresh quota gating | Fixed fake-process tests and the bounded live controller evidence below. Candidate handoff/worktree provisioning and complete delivery remain incomplete |
| Jev adapter | Choice compilation, dynamic candidates, account model discovery and explicit shadow evaluation | Recorded HTTP tests plus a live two-question shadow evaluation on jev-1.14.0; calibration and consequential routing remain unverified |
| Native Codex adapter | Account/catalog/quota metadata, explicit execution adapter, private versioned event receipts, result collection and opt-in factory-owned writer commits | Native subscription sign-in, revision-bound collection/reconnect and scoped source approvals; writer/Git qualification is described below. Broader automatic delivery and judgment quality remain unverified |
| Codex capacity snapshot | One-probe allowlisted account/plan, native pool windows and model/effort IDs; duplicate-pool conflict refusal and nullable unknown facts | Offline recorded/fake transport tests and a live metadata-only observation validate curation of the native shape. No scheduler, reservation, eligibility or broad readiness claim |
| Native Grok/Antigravity catalogs | Versioned metadata observations with actual model IDs, CLI identity, original stdout and collection duration | Verified against native catalog commands; quota, worker lifecycle, supported effort and allowance sharing remain unknown |
| Native Grok metadata | Cached OIDC sign-in, subscription/access-gate metadata and per-model effort choices through a private ACP child | Fixed protocol/process tests and a live owner-script probe of agent 1.0.77: X Premium+, no access gate, four model choices and their reported effort values. Quota and worker lifecycle remain unverified |
| Native Grok read-only baseline | Generic Rex launch, headless structured response and exact-assignment reconnect | On 2026-11-07, Grok 4.7 read an owned fixture and returned its exact contents and working directory in 12 seconds. The source stayed unchanged and replay reused the same exited pane and original output. Dedicated event collection, automatic capacity observation and broader lifecycle/recovery remain unverified |
| Local operator view | Stored task facts, holds, allowances, report freshness, evidence references, explicit live process observations and pause/resume controls | Offline replay/lost-reply/conflict tests and an owned Chrome fixture exercise pause/resume, keyboard submission and visible history. No live worker was interrupted. Other controls and the full seven-view dashboard remain incomplete |
| CI and review | Offline tests, diagnostics, design/package validation and independent review dispatch | Policy review and CI remain required; the operator temporarily waived waiting for Claude review during its subscription outage, as recorded in AGENTS.md |

The live Rex fixture checks exercise transport, timeout, reconnect and receipt
collection without a model turn or specialist judgment. The earlier native CLI
baseline in [native execution](native-rex.md#verification) is a separate observation;
it does not verify the execution adapter or a complete delivery loop. The operator
view has been inspected in Chrome against the harmless conformance session.
All five subscription routes remain unverified as complete scheduling routes.
The harmless fixtures used earlier receipts. Marker-bound version-3 receipts now
have live read-only coverage: a real review reported blocked, an investigator
completed source inspection, and reconnect reused both original panes without
consuming more allowance. The initial protocol-failure attempt remains failed.
Version-4 receipts additionally bind the individual worktree Git directory.
Offline two-worktree fixtures and a separate live Rex event-transport fixture
verify that binding, collection and exact-pane reconnect. The transport fixture
makes no model turn or specialist judgment. Current version-5 receipts retain
that binding and add the factory commit intent/result. The prerelease version-5
engine independently reviewed the pause/resume change in PR #7 and its blank-hold
correction, approving each exact candidate. Both reports were collected from
their original Rex panes and streams. The reviewers did not rerun the full suite
or accept the task; this establishes bounded source-review delivery rather than
general quality or writer-commit readiness. Existing version-4 receipts remain
collectable unchanged; version-3 artifacts stay unchanged and inspection-only
under the current receipt reader.

On 2026-11-06, the explicit plan controller launched and collected a real native
documentation assignment, producing local commit `c5089c4` in 31.7 seconds. An
independent native correction review approved that exact candidate and was
collected automatically. Earlier failed attempts remain retained. That pair
predates the closed Git backend: its separate OS/Git conformance uses fake native
events with real Git, the installed pre-commit hook and SSH signing. It creates
a local commit while blocking a hook's external push, network connection and
outside credential-file read. These are distinct component checks; they do not
establish a fresh end-to-end native writer run through the closed backend or
complete task acceptance/integration.
Jev then returned resolved in a live shadow reassessment of the narrow decoder
question; no acceptance or review gate changed. Task schema 4
pins new admissions to an immutable Git-local repository marker. Ordinary clones
do not inherit it, and inode reuse cannot establish continuity. The spawn-directory wrapper was exercised by these native Rex launches. Older
admissions retain their history with an unknown pin and require operator-approved
new admission before repository-backed work.
Task schema 5 adds ordered operator-control requests without changing task limits
or evidence. Legacy holds migrate without invented request history. Native receipt
versions remain separate from the task store.

Metadata discovery on 2026-11-06 confirmed installed Claude Code 2.1.319,
Grok Build 1.0.70 and Antigravity CLI (`agy`). Claude's `auth status --json`
reported a native subscription sign-in; `grok models` reported a grok.com sign-in
and model choices; `agy models` returned choices for both Gemini and Claude.
These commands made no model turn. They establish available discovery interfaces,
not fresh capacity, native worker conformance or independent allowance pools.
Refresh through those native commands rather than treating this dated observation
as scheduling authority. The structured catalog adapter subsequently observed
four Grok choices on 1.0.70 and six Antigravity choices on 1.5.2, including
Gemini and Claude families, without a model turn.

## Next slices

1. **Operational store and scripted execution.** Qualify live ready-work execution and add worktree provisioning, candidate handoff and finer provider reservations. Add task acceptance and integration/merge recovery using the existing general coding/release policy.
2. **Jev routing evaluation.** Verify live evaluation, cache independent judgments and route explicit uncertainty through a durable bounded standby queue. Build representative labeled and held-out evaluations before enabling consequential routing. Use recorded responses for replay, never fabricated live-success receipts.
3. **Rex and provider conformance.** Pin the discovered Rex binary/server identity and verify process launch, working directory, native session identity, input delivery, readiness and recovery for Claude, Codex, Grok and both Antigravity model families. Discover actual account eligibility, tools, supported thinking levels, model catalogs and shared allowance boundaries. Native subscription sign-in is not interchangeable with an API key.
4. **First live specialist loop.** Deliver an authorized repository task with the appropriate developer, independent verifier and domain specialists. Route findings directly; prove that investigator -> Jev can avoid a judge invocation. Exercise changed-input invalidation, one real exception and controller restart without duplicate effects.
5. **Dashboard and capacity scheduling.** Complete all seven views, controls and measurements in [the dashboard contract](dashboard.md) against operational facts/events. Show real work, resource freshness, subscription pools, decision inbox and efficiency drilldowns. Respect existing user authority for actions; do not ask workers for status prose.
6. **Learning and controlled rotation.** Record outcomes, version model/prompt/workflow choices and evaluate targeted changes. Explore within an explicit budget, compare similar task cohorts, audit confident errors and roll back regressions. Preserve specialist independence and task authority during exploration.

## Delivery checks

Before claiming a usable factory, demonstrate the actual selected native harnesses, known quota-sharing boundaries, live Jev evaluation behavior, interruption recovery, exact-revision verification, operator controls and observed dashboard events. Compare cycle time, quality, rework and coordination cost with representative prior work. Structural ACR validation, mock replays and a green process exit are separate evidence and must remain labeled separately.

Before publishing the plugin, create/verify its actual GitHub identity, commit the selected distribution, run ACR/native adapter checks and the selected release policy. Installing skills never silently starts the service or transfers existing Herdr work.
