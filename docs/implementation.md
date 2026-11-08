# Implementation inventory

This is the canonical inventory of current coverage and remaining work. The
[design](design.md) and [dashboard contract](dashboard.md) define the full outcome;
the command guides describe operation rather than maintain separate status lists.

## Current coverage

| Surface | Implemented | Evidence and limits |
| --- | --- | --- |
| Contracts and package | Agreed design, specialist role catalog, configuration, one ACR skill, reference decisions and recorded replay scenarios | Structural validation and deterministic tests; no live judgment-quality claim |
| Task runtime | Admission, cumulative attempt limits, holds, revision-bound reports, dependency launch guards, explicit Rex launches and limited interrupted-launch reconciliation | Fake-server tests and harmless live Rex conformance; process success never accepts a task |
| Writer worktree provisioning | One explicit task-owned branch/path from selected inputs, durable before-effect intent, raw regular-file checkout, completed replay and conservative pending reconciliation | `tests/test_provisioning.py` uses real temporary Git repositories for lost replies/restarts, collisions, holds, changed inputs and callback denial. No provider/Rex, automatic candidate handoff or independent release-readiness claim |
| Ready-work runner | Explicit plan execution, terminal native result collection, dependency advancement, instance work limits, verification/recovery slots, fresh quota gating and opt-in `--from-writer` candidate selection | Fixed fake-process tests and the bounded live controller evidence below. Handoff tests use original receipt/event validation, observed fake Rex processes and real temporary Git commits, including restart and refusal paths; no live opt-in handoff or quality claim. Complete delivery remains incomplete |
| Jev adapter | Choice compilation, dynamic candidates, model discovery, shadow evaluation, exact-input reuse, a derived uncertainty inbox and explicitly bound native standby/judge answers | Recorded HTTP/restart/concurrency and native-carrier tests plus earlier live evaluation/replay on jev-1.14.0. Collected current answers close only their matching question; missing inputs remain operator requests. Automatic dispatch, calibration, consequential routing and failed-request recovery remain incomplete |
| Native Codex adapter | Account/catalog/quota metadata, explicit execution adapter, private versioned event receipts, result collection and opt-in factory-owned writer commits | Native subscription sign-in, revision-bound collection/reconnect and scoped source approvals; writer/Git qualification is described below. Broader automatic delivery and judgment quality remain unverified |
| Codex capacity snapshot | One-probe allowlisted account/plan, native pool windows and model/effort IDs; duplicate-pool conflict refusal and nullable unknown facts | Offline recorded/fake transport tests and a live metadata-only observation validate curation of the native shape. No scheduler, reservation, eligibility or broad readiness claim |
| Native Claude auth metadata | Independent fixed version/safe-restricted auth-status probe, strict schema-1 allowlisted native auth facts, curated environment and explicit config-directory preservation | Fixed sanitized socket-free process tests cover signed-in/signed-out results, all documented auth methods and refusal/privacy paths. Candidate metadata-only live observation, host full tests and exact-candidate independent QA/security remain foreman gates. No quota, subscription eligibility, model/tool confinement, execution readiness or route activation is established |
| Native Grok/Antigravity catalogs | Versioned metadata observations with actual model IDs, CLI identity, original stdout and collection duration | Native catalog commands establish model lists. Separate rows cover subsequent quota and lifecycle observations |
| Native Grok metadata | Retained cached OIDC metadata decoder; new native probes are held before launch | Historical fixed protocol/process tests and a live owner-script probe of agent 1.0.77: X Premium+, no access gate, four model choices and their reported effort values. Configuration inspection does not freeze executable integrations; no current probe readiness claim |
| Native Grok read-only baseline | Generic Rex launch, headless structured response and exact-assignment reconnect | On 2026-11-07, Grok 4.7 read an owned fixture and returned its exact contents and working directory in 12 seconds. The source stayed unchanged and replay reused the same exited pane and original output. Dedicated event collection, automatic capacity observation and broader lifecycle/recovery remain unverified |
| Managed Grok inspection | Retained command/receipt/event contracts and reconnect; new execution and result collection are held | Built-in sandbox enforcement may fail open and filesystem grants do not confine reads to the checkout. Custom profiles add grants; inspected executable configuration remains mutable. Historical fixtures remain inspection evidence. Offline tests verify refusal before child creation, allowance consumption or new result recording. A compatible boundary and live conformance remain required |
| Local operator view | Stored task facts, holds, allowances, report freshness, evidence references, explicit live process observations, Team native request metadata/saved Rex identities, authenticated read-only pane detail and pause/resume controls | Offline replay/lost-reply/conflict tests and an owned Chrome fixture exercise pause/resume, keyboard submission and visible history. No live worker was interrupted. Team/pane code at b2f4b6f passed 435 host tests and independent native QA/security source review. Direct-candidate authenticated HTTP and original owned Rex pane reads passed without changing task state; original-owner request metadata was recognized across application worktrees. Browser verification of these additions remains unavailable. Other controls and the full seven-view dashboard remain incomplete |
| Antigravity native usage | Metadata-only native `/usage` reports with original group labels, weekly percentages/reset timestamps and collection duration | Recorded/fake transport tests and a live 1.5.3 probe agree with its native quota panel: Gemini quota was available; the shared Claude/GPT group was exhausted. Account eligibility, model binding, reservations and automatic scheduling remain separate |
| Antigravity retained execution decoder | Pure `native_antigravity.parse_execution` for original 1.5.3 JSON lines, frozen model/cwd/report schema, conversation identity, ordered lifecycle and terminal structured report/response agreement | Fixed sanitized offline fixtures validate reports, nullable native usage/duration and rejection without rewriting inputs. Init model is only an echo. Caller owns immutable provenance and original process exit; no worker, dispatch, collection integration, confinement, entitlement or live lifecycle qualification is established. Execution remains held |
| Antigravity Gemini baseline | Generic Rex launch, exact-fixture read, structured response and exact-effect reconnect | On 2026-11-07 the initial invocation warned that slash-command disabling suppressed plan mode and wrote its own native brain artifact. One recorded correction removed that flag and denied file writes, commands, web and MCP tools; it used only `read_file`, returned exact fixture contents/cwd inline, left the source unchanged and reconnected without changing its task/output. Dedicated collection and interruption/recovery remain unverified; the Claude route was held on its observed zero quota |
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
establish complete task acceptance/integration.

On 2026-11-07, a fresh owned fixture through the merged closed backend produced
a real factory-staged SSH-signed commit after its mandatory scanner and installed
pre-commit hook. The writer took 264.6 seconds. A restarted controller collected
the original worker without launching a replacement, then correctly held the
old input plan as stale. After explicit selection of the produced candidate, an
independent native QA assignment approved its exact README contents and one-file
diff; the verification controller finished in 37.5 seconds. A separate mechanical
check verified the SSH signature with temporary allowed signers, without changing
global Git settings. QA explicitly left factory ownership, hook execution and
restart outside its observed coverage; the owner retains separate execution
evidence for those facts. This qualifies fixture delivery and handoff mechanics,
not general judgment quality, project task acceptance or release readiness.
Jev then returned resolved in a live shadow reassessment of the narrow decoder
question; no acceptance or review gate changed. Task schema 4
pins new admissions to an immutable Git-local repository marker. Ordinary clones
do not inherit it, and inode reuse cannot establish continuity. The spawn-directory wrapper was exercised by these native Rex launches. Older
admissions retain their history with an unknown pin and require operator-approved
new admission before repository-backed work.
Task schema 5 adds ordered operator-control requests without changing task limits
or evidence. Legacy holds migrate without invented request history. Native receipt
versions remain separate from the task store.
Task schema 8 combines the earlier provisioning and allowance owners. Migration
preserves existing provisioning intents, allowance additions, holds, inputs and
assignments; it adds only the missing empty field. Provisioning recovery uses the
saved intent and a unique Git worktree lock marker. Incomplete outcomes require
inspection rather than another creation attempt. Offline fixtures verify migration
from both earlier formats without granting attempts or adopting worktrees.
Filename regressions cover exact CR/LF names and destination filesystem case,
Unicode and directory equivalence. A private name-only probe rejects aliases
before intent; source validation rejects physical file aliases during recovery.
Schema compatibility and these fixtures do not establish live delivery.

On 2026-11-07, the optional Jev decision store evaluated the actual pending
provisioning exception in shadow mode. Native discovery exposed only aliases;
the request used `jev-latest` with expected resolved version `jev-1.14.0`, which
the response confirmed. The two answers were `demanding` and `operator`, with
789 input/110 output tokens and a 0.115-second evaluation request. Exact replay
created no provider client and left both the decision and original task bytes
unchanged. No worker, grant or acceptance effect followed. This verifies one
stored evaluation/replay, not representative routing quality, live uncertainty
resolution or total coordination overhead; catalog probes and local work are
additional overhead.

On 2026-11-07, engine `fa816bf` delivered a live bounded standby answer through
Codex 0.166.0 in its own Rex fixture. Jev returned `uncertain` for an unspecified
underlying assignment. The first native turn identified the missing input but
used the wrong report outcome and remains failed. One allowed correction, with
clarified report guidance and an explicit requirements revision, returned
`blocked` with a concrete request for assignment/scope/acceptance in 12.7 seconds
(25,402 input/330 output tokens). Collection placed that answer in the operator
inbox. Fresh-process reconnect/recollection launched nothing and left task,
receipt and event bytes unchanged. The fixture and existing provisioning task
stayed unchanged; the correction count remains 1/1 used. This qualifies that
carrier/replay path, not judge quality, automatic dispatch, accepted delivery or
cross-engine-path adoption.

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

1. **Operational store and scripted execution.** Qualify live ready-work execution and opt-in candidate handoff; add worktree provisioning and finer provider reservations. Add task acceptance and integration/merge recovery using the existing general coding/release policy.
2. **Jev routing evaluation.** Qualify live stored evaluation/replay, resolve retained uncertainty through bounded standby/judge assignments, and add approved recovery for failed requests. Build representative labeled and held-out evaluations before enabling consequential routing. Use recorded responses for replay, never fabricated live-success receipts.
3. **Rex and provider conformance.** Pin the discovered Rex binary/server identity and verify process launch, working directory, native session identity, input delivery, readiness and recovery for Claude, Codex, Grok and both Antigravity model families. Discover actual account eligibility, tools, supported thinking levels, model catalogs and shared allowance boundaries. Native subscription sign-in is not interchangeable with an API key.
4. **First live specialist loop.** Deliver an authorized repository task with the appropriate developer, independent verifier and domain specialists. Route findings directly; prove that investigator -> Jev can avoid a judge invocation. Exercise changed-input invalidation, one real exception and controller restart without duplicate effects.
5. **Dashboard and capacity scheduling.** Complete all seven views, controls and measurements in [the dashboard contract](dashboard.md) against operational facts/events. Show real work, resource freshness, subscription pools, decision inbox and efficiency drilldowns. Respect existing user authority for actions; do not ask workers for status prose.
6. **Learning and controlled rotation.** Record outcomes, version model/prompt/workflow choices and evaluate targeted changes. Explore within an explicit budget, compare similar task cohorts, audit confident errors and roll back regressions. Preserve specialist independence and task authority during exploration.

## Delivery checks

Before claiming a usable factory, demonstrate the actual selected native harnesses, known quota-sharing boundaries, live Jev evaluation behavior, interruption recovery, exact-revision verification, operator controls and observed dashboard events. Compare cycle time, quality, rework and coordination cost with representative prior work. Structural ACR validation, mock replays and a green process exit are separate evidence and must remain labeled separately.

Before publishing the plugin, create/verify its actual GitHub identity, commit the selected distribution, run ACR/native adapter checks and the selected release policy. Installing skills never silently starts the service or transfers existing Herdr work.
