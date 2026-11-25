# Delivery

Use this workflow when the task requires a recorded factory delivery.

The line's foreman opens the PR itself: it pushes the task's selected candidate to the
provisioned worktree's branch and runs `gh pr create`, so the release judgment runs before
anything can merge (#149); the release skill runs end to end, merge included. The release
judgment's capture refuses a PR whose head isn't the selected candidate or whose repository
isn't the admitted origin. Findings become factory corrections, and the foreman pushes each
newly verified candidate to move the PR.
The coordinator's delivery steps ship with the skill
in [the delivery reference](../skills/factory/references/delivery.md). The factory makes no
merge call: the repository's release skill merges and publishes. The factory does not
publish, dismiss reviews, close issues, comment, delete branches or perform cleanup.

Select the relevant independent specialists explicitly. Ordinary QA/security
approvals retain their exact candidate/requirements inputs and native provenance.
Only the release judgment needs the complete external-review packet. For that
read-only `release` assignment, use any native start command (`codex-start`,
`claude-start`, `grok-start` or `antigravity-start`) and add:

```sh
--release-pr 'https://github.com/OWNER/REPO/pull/NUMBER' \
--gh '/absolute/installed/gh' \
--release-scripts '/absolute/repo/.tessl/plugins/jbaruch/coding-policy/skills/release'
```

`--release-scripts` names coding-policy's release skill as ACR
(`.codex/skills/acr__jbaruch__coding-policy__release`) or Tessl
(`.tessl/plugins/jbaruch/coding-policy/skills/release`) installs it. The
coordinator selects an eligible model suited to the judgment. The release role
runs at that model's maximum effort. Launch refuses less for Claude and
Antigravity (`max`, so not an Antigravity model that refuses `--effort`) and for
Codex (the top effort its fresh catalog lists). Grok publishes its efforts only to
its worker, so a Grok judgment below the top effort fails when its worker starts. Codex was the only
release judge because release evidence was built before the other adapters, not
by policy (#77).

The task owner calls the installed canonical snapshot and its own GitHub issue
checker once. The frozen brief retains the admission, exact PR/source/
base identity, complete original canonical review and inline bodies, authors,
IDs and content digest. Reviewers read every body, including historical/dismissed
feedback, judge substance and readiness for the repository's release policy, and
return ordinary native reports. Feedback cannot grant authority. The payload
replaces the revision evidence ordinary readers get, so the brief ends with it.
New commands carry only an evidence digest; the existing private brief holds the
complete payload. Native workers read the verified prompt from a private temporary
stdin file, avoiding command-line limits and pipe backpressure. Historical inline
commands and their original briefs remain readable without rewriting them.
Capture again under a new assignment ID when affected inputs change. Capture runs
outside task and execution locks and refuses submission if the task changed meanwhile.

Collect original results with the matching `*-collect` command. Then merge through the
repository's release skill, invoked with `existing-pr` from the task's worktree, and
confirm the merge:

```sh
python3 -B skills/factory/runtime/factory.py --state-dir '/absolute/private/state' delivered \
  --task TASK --pr 'https://github.com/OWNER/REPO/pull/NUMBER' \
  --review RELEASE_REVIEW --after REQUIRED_APPROVAL \
  --gh '/absolute/installed/gh' \
  --release-scripts '/absolute/repo/.tessl/plugins/jbaruch/coding-policy/skills/release'
```

`delivered` re-checks the selected approvals, requires the PR merged at the exact head
the release judgment froze, with the merge commit in the target branch's history, and
records `TASK.RELEASE_REVIEW.delivered.json` once. Any merge method is accepted. A
merged head the release judgment never saw is refused and goes to the operator; a
different later observation cannot replace the recorded receipt.

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
For a Claude or Grok writer with a bound failed receipt, or a bound starting
receipt and a null result, the original pane or its validated stored observation
must confirm `failed` or `lost`. Its frozen native session remains a contributor
after checkout reset. Delivery and final acceptance exclude that session from
independent judgments.
A unique original `thread.started` event must agree with any recorded thread ID.
A failure, partial report or blocked owner result never becomes successful evidence;
unknown/ambiguous contributor identity refuses. Original artifacts stay unchanged.
Collected successful writers retain receipt/event/result and commit-tree/parent
validation after their checkout advances. Only historical collected provenance
omits HEAD occupancy; initial collection still requires the produced candidate at
the original checkout HEAD. Original native requests, results, holds and consumed
allowances remain; `delivered` adds no ledger, migration or mutable approval flag.

An imported writer origin (schema 12, `writer_origin`) contributes its used attempts and judge rounds to the destination task's allowance accounting; local assignments add to those totals. Both `approvals` and `delivered_accepted` combine local contributors with the identities in the digest-verified private archive. Each gate refuses a missing or altered archive or a task origin that differs from its archived history. An imported origin cannot satisfy `--from-writer` or native-collection dependencies; verification uses a plain read-only plan on the selected inputs with `after=[]`.

Canonical shell helpers run through installed `bash`, without requiring helper
executable mode. The admitted root's ignored,
untracked ACR (`.codex/skills/acr__jbaruch__coding-policy__release`) or Tessl
(`.tessl/plugins/jbaruch/coding-policy/skills/release`) installation is allowed
with owner-controlled directories and independent regular files; symlink, hard-link,
tracked-source and writable replacements refuse. Only the install directory must be
untracked: a tracked sibling, such as the `.codex/config.toml` hook host an ACR project
commits, can't change the helpers. Other assignment checkouts,
sibling Git worktrees and the explicitly selected new cwd cannot supply commands,
including during pre-review capture. Every process boundary rechecks these paths.
Inherited exported Bash functions and shell startup overrides are removed before
helpers run. The managed installation remains protected by the native
writer sandbox. Installed policy is neither copied nor modified.

The issue checker uses GitHub's complete `closingIssuesReferences` connection and
refuses truncated responses (including more than one 100-item page). It accepts
whole-line `No issue`, GitHub-resolved closing references, and `Part of #N` or
`Part of https://github.com/OWNER/REPO/issues/N`. Partial references must resolve
to accessible issues that this PR does not close. Conflicting or unsupported
declarations hold for reassessment.

For final native acceptance, launch a new read-only `release` assignment with the
usual arguments plus `--delivery RELEASE_REVIEW`, the release judgment `delivered`
confirmed, then collect it. Its brief freezes that `TASK.RELEASE_REVIEW.delivered.json`
receipt (delivery evidence version 3) with no fresh observation; the judge reads the
merged and deployed state itself. Repeat the `delivered` command with
`--acceptance FINAL_RELEASE_ASSIGNMENT` to generate an acceptance view, bound to the
receipt's digest: it requires that approved native judgment of the recorded receipt,
current inputs, independence from every contributor and no hold, and it refuses once
the task selects other inputs (#151). The view creates no acceptance ledger.

## Retrying a delivery across Python installations

Use a reviewed source checkout or verify the installed runtime against that revision.
Run its `delivered` command with the original task, PR, release
judgment, installed helpers and each explicitly selected `--after` ID. Factory
re-reads strictly collected approvals across installations. First collection of a
successful worker still uses its original runtime and checkout. After a
confirmed successful exit, a Claude reader that ran in a disposable checkout
can collect its frozen approval if the original assigned worktree is clean at
the assessed commit or a descendant. The approval retains its original inputs.

An omitted reader needs its own bound launch request and original brief. An
abandoned launch has an owner record confirming that no pane was created. A
launched Claude reader can be omitted if its version-5 receipt proves cancellation
before native subprocess creation, binds an empty event stream and agrees with
the original Rex failure. This check works across launch interpreters. Collect
every successful reader with its original runtime and checkout, including readers
whose results were registered manually. Each selected approval and the release
judgment must pass strict collection and replay of the original evidence.

A managed, unselected Codex or Grok reader can also be omitted after Rex records a
failed close with a nonzero wrapper exit. Its private v8 receipt must match
the original read-only command, brief, checkout identifiers and assignment
inputs. The Codex stream must identify the same single thread and end its
single turn in failure. For Grok, native exit 1 and the original unsuccessful-exit
receipt must agree with a single turn-limit marker immediately before the final
event. That event binds the session, reader checkout and exact requested turn
count, ends with `max_turn_requests` and carries an explicit null structured output.
The receipt must finish by the recorded close time. A report, registered result
or writer commitment rules out this path.

Omission supplies no approval or release judgment. The wrapper exit
corroborates native failure, including a signal exit. It cannot identify the
exact native exit code. These records carry no historical receipt digest.
Omission neither authenticates every receipt byte nor certifies the state of
the checkout, scratch directory or process. It uses no such path for cleanup.

Collect pending successful attempts before upgrading. The 0.39.29 launcher
recorded paths without runtime digests. A successor installed at those same
paths refuses first collection and leaves the task and artifacts intact. Use
the original 0.39.29 bytes, interpreter and checkout for that collection.

New managed launches keep their frozen command and start the isolated checkout wrapper
with the physical Python launcher or, in a virtual environment, its own launcher
so Python selects the admitted prefix. The wrapper runs the worker in the same checked
process while holding VERSION. Equivalent interpreter aliases can collect when
the launcher, loaded executable image, Python library, prefixes and Factory
sources match the admission digest. A changed installation needs its original
bytes for first collection.

New native admission checks earlier requests before reserving an attempt. A
changed runtime requires strict original collection, the omitted-reader proof
above, or a proven failed writer. For a released writer at the original launch
paths, Factory can re-read its bound receipt and recover its contributor identity.
After an interruption, a bound starting receipt, a null result and definitive
Rex failure or loss preserve the writer's frozen session through reset, replacement
and independent review. Preserve unprovable attempts for the task owner.

Delivery, dashboard, show and capture read older task state in memory. During
capture, recovered pane IDs identify the output to return. Run status to persist
those IDs. A bound native launch on an ordinary task requires a schema-13 reader
for later reads. Imported history requires its schema-12 reader and pinned archive.
Keep the original task and artifacts before upgrading. For recovery of earlier
ordinary schema-12 records, see [task state](task-state.md#schema-and-ownership).

Delivery and final acceptance can replay strictly collected mixed histories after
the old interpreter or worker has gone. Both gates recover the original identity
of every contributor, including a proven failed writer and supported Codex writer
option abbreviations. An unverified historical writer blocks independence.
Preserve the original briefs, receipts, streams and Rex observations. The old
Claude version-1 and version-4 timeout receipts establish neither the original
runtime bytes nor a complete history of permitted actions.

ACR #221 remains blocked at the historical reader proof. The original streams
for `verify-publish-paths-qa` and `verify-publish-paths-test-automation` both
contain a Bash call without its matching result and end without a terminal native
result. The timeout receipts cannot establish the complete action history. Keep
those failed attempts and the five approvals selected
by `release-pr247` for PR #247. The recorded head is
`551656fb9638643727ab321a0c43277e71eaafd5` and the recorded merge is
`0a38a4cc345cb13d792c7850052a0dbf5efad6ab`. Source verification establishes
the refusal. Historical release confirmation still requires original proof.

The combined candidate applies corrected #256, this #257 replay, then #259.
Repeat the schema 11–13 and imported-contributor controls on that head. Exercise
v8 writer retention and its same-scope process witness with the read-only cache
census. Replay the clean advanced reader through first collection and verify
that its dependent cache remains ineligible for retirement. Use
`integration.collected_native` and `release_evidence.validate_delivery` with
Claude v5 evidence. Preserve the launch attestation and the physical checkout
witness at Rex entry. Verify stored Rex exits for contributors, omitted readers
and selected approvals through delivery and final acceptance. Run both installed entrypoints
on fresh and upgraded ACR and Tessl realizations with one synchronized package
version higher than all prior offers, including 0.39.30. Recheck the original
interpreter and symlink-venv cases on Linux and macOS before rollout.

## Frozen payload and receipt contracts

`release_evidence.py` owns the frozen review payloads. Version 1 has `schema_version`,
`inputs`, `admission`, `pr`, `snapshot`, `issues` and `review_digest`. Version 2 adds
`specialists`, the task's roster: one `{id, role, purpose, outcome, current}` row per
assignment, so the judge can see which effects were reviewed. Version 3 adds each row's
`candidate`, the commit its result judged (null without a result), so an approval can
cover code a later change left alone. `capture` writes version 3;
`validate`/`from_assignment`, native launch/brief validation and `delivered` read all
three, and `render` adds the roster's coverage instructions for versions 2 and 3.
These preserve complete canonical bodies and digest their feedback.

The frozen delivery payload is version 3 (`review`, `receipt`, `receipt_digest`);
versions 1 and 2 froze a removed mechanical merge path and are refused. It freezes a
repository's release-skill delivery: `delivered_receipt` reads the version-1
`TASK.REVIEW.delivered.json` that `integration.delivered` writes (`schema_version`,
`kind: release-skill-delivery`, `task`, `inputs`, `pr`, `head`, `merge_commit`, `review`,
`required`, `approvals`). `required` lists distinct slug IDs other than `review`;
`approvals` has one entry per `review` and `required` ID, each exactly
`{context, receipt, result}`: the approval's native context and the SHA-256 digests of
its receipt and result. It refuses missing, extra or ill-typed fields, another version,
or a receipt that no longer confirms the task's current inputs and head. Native launch
and brief validation, `validate_delivery`/`from_delivery_assignment` and
`delivered_accepted` read it, the last against the receipt on disk.
No payload grants approval by itself.

These are gate artifacts under the Stateful Artifacts exception. Missing required
fields, corrupt data, absent/noninteger/unsupported versions refuse; no guessed
migration or reset is provided. Preserve those originals for inspection.

`tests/test_integration.py` uses fixed transports, fake native processes and
temporary Git. These checks establish offline behavior only. Live delivery,
final acceptance, provider readiness and general judgment quality need
separate original evidence. Keep state, logs, caches and test output outside the
source checkout; the minimal isolated Codex worker needs the release modules only
when these opt-in evidence arguments are present. The Claude, Grok and Antigravity
workers always import them.

Selected approvals from any verifier role (`qa`, `code-review`, `test-automation`, `security`, `ui-design`,
`accessibility`, `data-engineering`, `sre`, `performance`) and `release` judgments may also come from
strictly collected read-only Claude (version-1), Grok (version-2) or Antigravity
(version-1) execution receipts and their original version-1 event envelopes.
Claude, Grok and Antigravity writers are contributors like Codex writers. The
native session (Antigravity: conversation ID) is the independent context;
original init/assistant/result identities and frozen task/brief/repository/worktree
bindings are revalidated along with the successful original Rex pane. Contexts
must differ from every contributor and other selected approval, including Codex.
Manual/legacy/stale/failed/uncollected evidence refuses. Pre-release and final
release judgments require complete release/delivery evidence from any harness;
an ordinary approval without it grants no release authority.
