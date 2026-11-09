# Exact-candidate integration

PR preparation, issue declarations and feedback correction stay with the installed
release skill. This adapter implements the selected repository's `checks.yml`
contract with no publication channel. It does not provision worktrees, publish,
dismiss reviews, close issues, comment, delete branches or perform cleanup.

Select the relevant independent specialists explicitly. Ordinary QA/security
approvals retain their exact candidate/requirements inputs and native provenance.
Only the release judgment needs the complete external-review packet. For that
read-only `release` assignment, use the existing `codex-start` arguments and add:

```sh
--release-pr 'https://github.com/OWNER/REPO/pull/NUMBER' \
--gh '/absolute/installed/gh' \
--release-scripts '/absolute/installed/acr__jbaruch__coding-policy__release'
```

The task owner calls the installed canonical snapshot and its own GitHub issue
checker once. The frozen brief retains the admission, exact PR/source/
base identity, complete original canonical review and inline bodies, authors,
IDs and content digest. Reviewers read every body, including historical/dismissed
feedback, judge substance, verify the no-publication scope against manifests and
workflows, and return ordinary native reports. Feedback cannot grant authority.
New commands carry only an evidence digest; the existing private brief holds the
complete payload. Native Codex reads the verified prompt from a private temporary
stdin file, avoiding command-line limits and pipe backpressure. Historical inline
commands and their original briefs remain readable without rewriting them.
Capture again under a new assignment ID when affected inputs change.

Collect original results with `codex-collect`. Then invoke:

```sh
python3 -B skills/factory/runtime/factory.py --state-dir '/absolute/private/state' integrate \
  --task TASK --id INTEGRATION --pr 'https://github.com/OWNER/REPO/pull/NUMBER' \
  --review RELEASE_REVIEW --after REQUIRED_APPROVAL \
  --gh '/absolute/installed/gh' \
  --release-scripts '/absolute/installed/acr__jbaruch__coding-policy__release'
```

Repeat `--after` for selected required approval IDs; selection is authorized input,
not parsed from acceptance prose. Every selected judgment must be `approved`,
current, read-only and collected from original native events/receipts. Only the
release judgment binds the full current external feedback digest, PR/base and issue
inputs. New review prose alone does not invalidate ordinary exact-input QA/security
approvals. Contexts must be distinct and independent of all native contributors. Revalidate the pinned Rex host/session/pane identity and
successful native exit as well as the original receipt/events. Unverified command
adapters cannot establish independence.
Generic successful dependencies or manual report registration supply no approval.
Failed writers remain contributors. Supported native execution receipts (versions
4–6) and original version-1 event lines supply their thread identity, with matching
job, repository, worktree, input and brief bindings and a terminal Rex observation.
A unique original `thread.started` event must agree with any recorded thread ID.
A failure, partial report or blocked owner result never becomes successful evidence;
unknown/ambiguous contributor identity refuses. Original artifacts stay unchanged.
Collected successful writers retain receipt/event/result and commit-tree/parent
validation after their checkout advances. Only historical collected provenance
omits HEAD occupancy; initial collection still requires the produced candidate at
the original checkout HEAD.

The CLI prepares evidence outside task locks, then uses the ordinary execution/task
locks only for brief submission and `factory.launch`. The worker runs in the release
review's original clean checkout and owns real Rex window/block identities. Before
mutation, its
[task-owner assignment](task-state.md#schema-and-ownership) command fixes admission/repository identity, exact GitHub PR,
head, base, target, requirements, selected IDs, original report/receipt digests,
review/issue inputs and installed executable/helper paths and hashes. No ledger,
migration or mutable approval flags are added. Original native requests, results,
holds and consumed allowances remain. A repository-scoped integration mutex serializes
mechanical execution/recovery within the factory state directory; it does not hold
the ready runner's execution lock. Task locks are released during canonical watcher
and network waits. The worker reloads controls and inputs after the watcher and
immediately before recording the merge attempt, then collects only its result into
the latest task record. Concurrent pause/input changes remain intact and refuse a
new effect. An already submitted effect can still reconcile while held.
Unrelated assignments and allowance additions during the watch are preserved and
do not abort delivery; the final guard compares admission, inputs, hold and the
selected integration/approval assignments.

New effects run the unchanged installed `watch-pr-reviews.sh` and require its
`ready` result, the current-head `checks.yml` successful native attempt, unchanged
review/source/base/issue inputs, actual repository merge requirements and no hold.
The watcher owns waiting and native quality readiness. A missing/failed Claude
review stays held; the existing operator waiver is available only through the
already authorized release procedure. There is no automatic waiver here.

Canonical shell helpers run through installed `bash`, without requiring helper
executable mode. Explicit helper paths/hashes are frozen. The admitted root's ignored,
untracked `.codex/skills/acr__jbaruch__coding-policy__release` installation is allowed
with owner-controlled directories and independent regular files; symlink, hard-link,
tracked-source and writable replacements refuse. Other assignment checkouts,
sibling Git worktrees and the explicitly selected new cwd cannot supply commands,
including during pre-review capture. Every process boundary rechecks these paths.
Inherited exported Bash functions and shell startup overrides are removed before
helpers run. The managed installation remains protected by the native
writer sandbox; the mechanical worker uses a clean checkout, not those ignored files
as candidate source. Installed policy is neither copied nor modified.

The adapter sends one non-admin merge request with the exact head condition, only
after verifying an enforceable server guard. This scoped personal-repository path
requires classic branch protection: strict required status checks, administrator
enforcement, no force pushes/deletion, no bypass allowances, and required `verify`
bound to GitHub Actions app ID `15368`. It resolves `verify` through the current
successful `checks.yml` pull-request run's exact attempt/job/check identity. Unknown,
absent, loose, bypassable, ruleset-only or untrusted configurations hold with an
operator prerequisite; the adapter never changes repository settings.

[GitHub's protected-branch contract](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
requires an up-to-date base for strict checks and applies protection to admins when
enforcement is enabled. The [branch protection API](https://docs.github.com/en/rest/branches/branch-protection#get-branch-protection)
exposes strict checks, expected app IDs and administrator enforcement. This is the
server's up-to-date-CI guarantee, not an invented atomic expected-base comparison:
[the merge input](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)
conditions only the head. If the base advances after our final read, the server
must reject a now-outdated branch before it lands. Frozen source/base checks and
post-merge parent verification remain. A client mutex cannot supply this guarantee.
[Native merge queue availability](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/merging-a-pull-request-with-a-merge-queue)
does not cover this personal repository. The foreman must separately configure and
verify protection; source tests do not establish actual repository enforcement.

The saved adapter command starts in its running original pane only when no execution
receipt exists. It exclusively creates that receipt before watching; a crash at any
later point permits only read-only reconciliation. The saved command also supports
read-only restart recovery when a receipt exists.
Repeating the same ID reconnects the real pane and **only observes**; even an open PR
cannot authorize resend. A running/unknown pane remains owned by its worker. A
terminal pane is reconciled without another Rex command. Lost Rex create replies
reconcile through the ordinary window marker and returned IDs. A terminal pane with
no receipt is uncertain, never permission to start another merge.
A restart or lost/unknown response remains `merge_outcome_unknown` until read-only
GitHub evidence establishes exact PR/head, merge commit parents and target ancestry.
Another ID cannot bypass a pending integration. A definitive rejection or a
pre-effect hold retains its original blocked result; reassess before a new ID.
There is no exactly-once claim.
An interrupted saved `executing` phase proves no submission occurred: recovery
finishes it as held so current inputs can be reassessed under a new ID. Only the
durable `merge_outcome_unknown` phase needs read-only merge-outcome reconciliation.
The merge call reads native HTTP status with its JSON body. Explicit HTTP 405/409
rejections, including `gh` exit 1, finish as rejected; transport failures, malformed
responses and other unconfirmed outcomes retain the same pending intent.

Delivery additionally resolves one unambiguous `checks.yml` push run at that merge
SHA and target branch, watches it to terminal success, rechecks its current attempt,
and repeats the factory-owned merged-issue check. Missing, failed, changing-attempt,
unbound or ambiguous evidence remains pending. The original execution receipt
retains observations; terminal collection binds its digest in the existing result.
Partial issue references and cleanup stay with the release owner. Publication is
inapplicable for this scoped path, never fabricated as successful.

The issue checker uses GitHub's complete `closingIssuesReferences` connection and
refuses truncated responses (including more than one 100-item page). It accepts
whole-line `No issue`, GitHub-resolved closing references, and `Part of #N` or
`Part of https://github.com/OWNER/REPO/issues/N`. Partial references must resolve
to accessible issues that this PR does not close. Conflicting or unsupported
declarations hold for reassessment. After merge every closing issue must be closed.
The checker's version and source digest bind new intents; older intents cannot
silently switch from the former installed helper to this checker.

For final native acceptance, launch a new read-only `release` assignment with the
usual arguments plus `--delivery INTEGRATION`, then collect it. Capture reads the
original integration's installed helper paths. Its brief freezes the original
delivery receipt/result/digest plus a fresh source-bound delivery observation.
Capture waits outside task/execution locks and refuses submission if the owner
changed meanwhile. Repeat the original `integrate`
command with `--acceptance FINAL_RELEASE_ASSIGNMENT` to generate an acceptance
view: it requires that approved native exact-delivery judgment, current inputs,
independence, no hold and fresh read-only delivery/CI confirmation. Delivery alone
reports `pending_native_judgment`. The view creates no acceptance ledger.
After a successful CI rerun, capture a new final native assignment; old judgments
remain bound to their original attempt. Crash-before-collection recovery may
confirm a later successful attempt of the same native run, then collect the
unchanged original receipt. Neither path resends a merge or rewrites prior results.
Launch failures/timeouts name restoration steps and require reconciliation of the
same integration ID; an unknown outcome never authorizes a new merge attempt.
Malformed JSON and text from these commands likewise require restoration of the
installed helper/output contract and reconciliation of that same intent. A
terminal hold before an effect requires reassessment before another intent.

## Frozen payload and receipt contracts

`release_evidence.py` owns version-1 frozen review payloads (`schema_version: 1`,
`inputs`, `admission`, `pr`, `snapshot`, `issues`, `review_digest`). `capture` writes;
`validate`/`from_assignment`, native launch/brief validation and integration read.
These preserve complete canonical bodies and digest their feedback. The same owner
writes version-2 frozen delivery payloads (`assignment`, `result`, `receipt`,
`receipt_digest`, `observation`); native release briefs and
`validate_delivery`/`from_delivery_assignment` read them. Version-1 delivery briefs
remain readable unchanged and assess only their original receipt's delivery. New
captures use version 2; no migration, mutable refresh store or replacement of old
briefs/receipts/results occurs. Both versions require the exact original mechanical
result and receipt; version 2 also requires a successful observation of the same
source and native run at an equal or later attempt.
Neither payload grants approval by itself.

`integration.py` owns the version-1 immutable command intent (`schema_version`, task,
assignment, absolute state directory, admission/inputs, PR identity, selected review
and required IDs, review digest/issues, installed command paths/hashes and native
approval digests). `intent_of` and `validate_launch` check the original request;
`bound_assignment` also checks the frozen brief and task binding before execution.

The same owner writes one `<task>.<assignment>.integration.json` execution receipt:
`schema_version: 1`, `kind: integration-execution`, exact `intent`, `state`, original
Rex `process` observation and `observations` list. States are `executing` (claimed
before waits), `merge_outcome_unknown` (durable before merge, or recovery), `held`,
`rejected`, and `delivered`. Held/rejected require a reason; delivered requires
source-bound delivery/CI evidence. Gate snapshots and external replies/errors are
execution evidence, not another queue. `execute` owns writes; `validate_receipt`,
result collection and `release_evidence.delivery` read. Terminal results bind the
receipt digest, and terminal receipt bytes are never replaced. The [task owner defines the task schema](task-state.md#schema-and-ownership).

These are gate artifacts under the Stateful Artifacts exception. Missing required
fields, corrupt data, absent/noninteger/unsupported versions refuse; no guessed
migration or reset is provided for the unpublished draft's unversioned artifacts.
Preserve those originals for inspection. Receipt loss after an effect remains
uncertain; a collected result requires its original receipt restored.

`tests/test_integration.py` uses fixed transports, fake native processes and
temporary Git. These checks establish offline behavior only. Live merges,
recovery, final acceptance, provider readiness and general judgment quality need
separate original evidence. Keep state, logs, caches and test output outside the
source checkout; the minimal isolated worker needs the release modules only when
these opt-in evidence arguments are present.

Selected ordinary `qa`, `security` and `code-review` approvals may also come from
strictly collected read-only Claude (version-1) or Grok (version-2) execution
receipts and their original version-1 event envelopes. Claude and Grok writers are
contributors like Codex writers. The native session is the independent context;
original init/assistant/result identities and frozen task/brief/repository/worktree
bindings are revalidated along with the successful original Rex pane. Contexts
must differ from every contributor and other selected approval, including Codex.
Manual/legacy/stale/failed/uncollected evidence refuses. Pre-release and final
release judgments continue to require the existing Codex path and complete
release/delivery evidence; Claude and Grok ordinary approvals grant no release authority.
