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

The task owner calls the installed canonical snapshot and issue-declaration helper
once. The ordinary command and frozen brief retain the admission, exact PR/source/
base identity, complete original canonical review and inline bodies, authors,
IDs and content digest. Reviewers read every body, including historical/dismissed
feedback, judge substance, verify the no-publication scope against manifests and
workflows, and return ordinary native reports. Feedback cannot grant authority.
Capture again under a new assignment ID when affected inputs change.

Collect original results with `codex-collect`. Then invoke:

```sh
python3 factory.py --state-dir '/absolute/private/state' integrate \
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

The CLI prepares evidence outside task locks, then uses the ordinary execution/task
locks only for brief submission and `factory.launch`. The worker runs in the release
review's original clean checkout and owns real Rex window/block identities. Before
mutation, its
schema-7 assignment command fixes admission/repository identity, exact GitHub PR,
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
tracked-source and writable replacements refuse. Other assignment checkouts cannot
supply installed commands. The managed installation remains protected by the native
writer sandbox; the mechanical worker uses a clean checkout, not those ignored files
as candidate source. Installed policy is neither copied nor modified.

The adapter sends one non-admin merge request with the exact head condition.
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

Delivery additionally resolves one unambiguous `checks.yml` push run at that merge
SHA and target branch, watches it to terminal success, rechecks its current attempt,
and runs the installed merged-issue check. Missing, failed, changing-attempt,
unbound or ambiguous evidence remains pending. The original execution receipt
retains observations; terminal collection binds its digest in the existing result.
Partial issue references and cleanup stay with the release owner. Publication is
inapplicable for this scoped path, never fabricated as successful.

For final native acceptance, launch a new read-only `release` assignment with the
usual arguments plus `--delivery INTEGRATION`, then collect it. Its original brief
freezes the confirmed delivery receipt and digest. Repeat the original `integrate`
command with `--acceptance FINAL_RELEASE_ASSIGNMENT` to generate an acceptance
view: it requires that approved native exact-delivery judgment, current inputs,
independence, no hold and fresh read-only delivery/CI confirmation. Delivery alone
reports `pending_native_judgment`. The view creates no acceptance ledger.

## Frozen payload and receipt contracts

`release_evidence.py` owns version-1 frozen review payloads (`schema_version: 1`,
`inputs`, `admission`, `pr`, `snapshot`, `issues`, `review_digest`). `capture` writes;
`validate`/`from_assignment`, native launch/brief validation and integration read.
These preserve complete canonical bodies and digest their feedback. The same owner
writes version-1 frozen delivery payloads (`assignment`, `result`, `receipt`,
`receipt_digest`); native release briefs and `from_delivery_assignment` read them.
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
receipt digest, and terminal receipt bytes are never replaced. Task schema remains 7.

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
