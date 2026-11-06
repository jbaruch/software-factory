# Implementation inventory

This is the canonical inventory of current coverage and remaining work. The
[design](design.md) and [dashboard contract](dashboard.md) define the full outcome;
the command guides describe operation rather than maintain separate status lists.

## Current coverage

| Surface | Implemented | Evidence and limits |
| --- | --- | --- |
| Contracts and package | Agreed design, specialist role catalog, configuration, one ACR skill, reference decisions and recorded replay scenarios | Structural validation and deterministic tests; no live judgment-quality claim |
| Task runtime | Admission, cumulative attempt limits, holds, revision-bound reports, dependency launch guards, explicit Rex launches and limited interrupted-launch reconciliation | Fake-server tests and harmless live Rex conformance; process success never accepts a task |
| Jev adapter | Choice compilation, dynamic candidates, account model discovery and explicit shadow evaluation | Recorded HTTP tests; live account evaluation, calibration and consequential routing remain unverified |
| Native Codex adapter | Account/catalog/quota metadata, explicit execution adapter, private versioned event receipts and result collection | Live metadata observation and harmless Rex event/timeout fixtures; adapter model execution and specialist delivery remain unverified |
| Local operator view | Stored task facts, holds, allowances, report freshness, evidence references and explicit live process observations | Read-only loopback HTML/API, protected by a per-process capability in a private file; not the complete dashboard contract |
| CI and review | Offline tests, diagnostics, design/package validation and independent review dispatch | Open PRs remain subject to both required reviews; Claude review is waiting for subscription capacity |

The live Rex fixture checks exercise transport, timeout, reconnect and receipt
collection without a model turn or specialist judgment. The earlier native CLI
baseline in [native execution](native-rex.md#verification) is a separate observation;
it does not verify the execution adapter or a complete delivery loop. The operator
view has been inspected in Chrome against the harmless conformance session.
All five subscription routes remain unverified as complete scheduling routes.

Metadata discovery on 2026-11-06 confirmed installed Claude Code 2.1.319,
Grok Build 1.0.70 and Antigravity CLI 1.5.1 (`agy`). Claude's `auth status --json`
reported a native subscription sign-in; `grok models` reported a grok.com sign-in
and model choices; `agy models` returned choices for both Gemini and Claude.
These commands made no model turn. They establish available discovery interfaces,
not fresh capacity, native worker conformance or independent allowance pools.
Refresh through those native commands rather than treating this dated observation
as scheduling authority.

## Next slices

1. **Operational store and scripted execution.** Add capacity reservations, queue fairness, automatic ready-work execution, worktree provisioning and cross-task concurrency controls. Add task acceptance and integration/merge recovery using the existing general coding/release policy.
2. **Jev routing evaluation.** Verify live evaluation, cache independent judgments and route explicit uncertainty through a durable bounded standby queue. Build representative labeled and held-out evaluations before enabling consequential routing. Use recorded responses for replay, never fabricated live-success receipts.
3. **Rex and provider conformance.** Pin the discovered Rex binary/server identity and verify process launch, working directory, native session identity, input delivery, readiness and recovery for Claude, Codex, Grok and both Antigravity model families. Discover actual account eligibility, tools, supported thinking levels, model catalogs and shared allowance boundaries. Native subscription sign-in is not interchangeable with an API key.
4. **First live specialist loop.** Deliver an authorized repository task with the appropriate developer, independent verifier and domain specialists. Route findings directly; prove that investigator -> Jev can avoid a judge invocation. Exercise changed-input invalidation, one real exception and controller restart without duplicate effects.
5. **Dashboard and capacity scheduling.** Complete all seven views, controls and measurements in [the dashboard contract](dashboard.md) against operational facts/events. Show real work, resource freshness, subscription pools, decision inbox and efficiency drilldowns. Respect existing user authority for actions; do not ask workers for status prose.
6. **Learning and controlled rotation.** Record outcomes, version model/prompt/workflow choices and evaluate targeted changes. Explore within an explicit budget, compare similar task cohorts, audit confident errors and roll back regressions. Preserve specialist independence and task authority during exploration.

## Delivery checks

Before claiming a usable factory, demonstrate the actual selected native harnesses, known quota-sharing boundaries, live Jev evaluation behavior, interruption recovery, exact-revision verification, operator controls and observed dashboard events. Compare cycle time, quality, rework and coordination cost with representative prior work. Structural ACR validation, mock replays and a green process exit are separate evidence and must remain labeled separately.

Before publishing the plugin, create/verify its actual GitHub identity, commit the selected distribution, run ACR/native adapter checks and the selected release policy. Installing skills never silently starts the service or transfers existing Herdr work.
