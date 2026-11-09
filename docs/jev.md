# Jev HTTP adapter

`jev.py` compiles the versioned Choice catalog into the current TypeSafe HTTP
contract. It uses the Python standard library, batches selected questions in one
request and preserves explicit `uncertain` answers, distributions, confidence,
actual model identity and observed token usage. It selects no provider, launches
no worker and grants no acceptance or release authority.

The integration follows the official [System One API](https://docs.typesafe.ai/api/system-one),
[Choice contract](https://docs.typesafe.ai/primitives/choice) and
[model discovery API](https://docs.typesafe.ai/api/list-models).

## Compile and discover

```sh
python3 -B skills/factory/runtime/jev.py compile --question task-demand --question correction-disposition
python3 -B skills/factory/runtime/jev.py models
```

Compilation is offline and credential-free. Live commands read `TYPESAFE_API_KEY`
from their execution environment. Supply the variable through your environment
or secrets manager; the adapter does not read credential files. Credentials are
not printed. HTTP redirects are
refused, and error bodies are not echoed.

Use a model name returned by discovery. No model name, access entitlement or quota
is guessed. An empty catalog remains empty. TypeSafe access is separate from the
native subscriptions used for specialist execution.

## Shadow evaluation

```sh
python3 -B skills/factory/runtime/jev.py evaluate --state /path/to/relevant-state.json \
  --model '<discovered-model-name>' --evidence-revision '<source-revision>' \
  --question task-demand --question correction-disposition
```

For `finding-recipient` or `workflow-recipe`, supply `--candidates /path/to/candidates.json`.
The file maps question IDs to maps of actual eligible IDs and their descriptions:

```json
{"finding-recipient": {"qa-worker": "Owns acceptance coverage for this candidate"}}
```

State files contain a JSON string, object or array, as required by the System One
API. Candidate files contain question-to-ID/description maps. Both refuse duplicate
keys and non-finite numbers; malformed inputs identify the file to repair before
any request is made.

The generic `candidate` placeholder never reaches inference. Real options are
expanded alongside `no_match` and `uncertain`. A supplied empty candidate set has
only those fallback outcomes. Eligibility and authority remain code decisions;
provide the relevant candidate capabilities and source evidence in state.

Evaluation emits a `schema_version: 1`, `mode: shadow` JSON envelope with the
evidence revision, question versions, request digest and validated response.
Without `--store` it does not persist or cache results. A request digest is an audit aid, not proof
of evidence freshness or stable model aliases. Application caching must bind
actual model/version, relevant evidence, question/version and candidate set.

## Retained decisions and uncertainty inbox

```sh
python3 -B skills/factory/runtime/jev.py evaluate --state /path/to/relevant-state.json \
  --model '<discovered-model-name-or-alias>' --model-version '<expected-jev-version>' \
  --evidence-revision '<source-revision>' \
  --question task-demand --store /path/to/private/decisions
python3 -B skills/factory/runtime/jev.py pending --store /path/to/private/decisions
```

The store reuses a completed evaluation only for identical endpoint, requested
model, expected model version, evidence revision, question versions, instructions,
criteria, candidates and state. Select a model/alias from live discovery. Use
`--model-version` to pin its expected resolved `jev-MAJOR.MINOR.PATCH` identity;
a versioned request name can supply its own default. The response must report
that exact version. An alias alone cannot authorize reuse. Replaying an exact completed request needs no credentials and
changes neither its original usage nor timing. Callers must still verify source
freshness before acting: a cache hit is not a new quota observation or approval.

`jev.py` owns version-1 `<request-sha256>.json` decision records and corresponding
local locks. Records contain `kind: jev-decision`, `mode: shadow`, `id`, `request`,
`state`, `created_at`, `finished_at`, `request_seconds`, `response` and `error`.
The ID hashes the canonical request with `endpoint`, `evidence_revision`,
`model_version`, `question_versions` and the exact HTTP `payload`. The payload retains the selected
model, relevant state and compiled questions once; it contains no API credential.
The new store directory is private; records use mode 0600 and atomic writes.

Before HTTP, the owner saves `pending` with null result/timing/error fields.
Completion stores the validated response and original request duration, with
`state: completed` and null error. A known evaluation failure stores `failed`,
timing and the adapter's sanitized actionable diagnostic. A model mismatch also
retains its validated response for inspection; other failures have null response. A crash
or failed completion save leaves the original intent pending. A per-request lock
prevents concurrent duplicate calls. Pending, failed, malformed or unsupported
records never become cache misses and never trigger an automatic repeat.
Inspect original evidence and service/account status before deciding recovery;
this command adds no retry or reconciliation authorization. Preserve those
records rather than deleting them or selecting a new store to evade the hold.

This is a gate store under the stateful-artifact contract: its owner/reader accepts
only version 1, refuses other versions actionably, and never migrates an old
evaluation envelope into a successful decision. Missing directories have no
prior decisions. Unreadable or malformed records are unavailable evidence and
must not be interpreted as an empty queue.

`pending` derives a version-1 `jev-inbox`, also in shadow mode. Each item links its
original decision/evidence revision and question rather than copying the state.
Generic explicit uncertainty proposes `standby-reasoner`; `uncertain` or
`unresolved` on `investigation-resolution` proposes `judge`. Pending and failed
requests propose operator inspection and include the failed condition. Repeated
reads are offline and do not alter records. These are proposed owners, not
dispatch authority; there is no automatic reasoner invocation or queue resolution.

HTTP failures, invalid answers, missing questions and unavailable usage fail
visibly without retry. A selected Choice must have the maximum probability;
ties are accepted. HTTP diagnostics distinguish request repair, authentication,
rate limits and unavailable service without printing response bodies.
Without storage, the caller must retain pending work. With storage, the original
request and uncertainty remain available through the derived inbox above.

No confidence threshold has been calibrated or approved for consequential
routing. Evaluate representative labeled and held-out cases, including sampled
confident errors, before enabling answer-to-action mappings. Typed answers and
valid distributions do not establish judgment quality. Ordinary tests use fixed
fake transports; live evaluation must be invoked separately.

## Bounded native answers

An explicitly admitted `codex-start` assignment can add `--decision-store` with
an absolute store path, `--decision` with its SHA-256 ID and `--question` with one
unresolved question ID. Generic uncertainty requires read-only role
`standby-reasoner`; post-investigation uncertainty/unresolved requires `judge`.
Admission, native eligibility, current candidate/requirements, task limits and
Rex ownership remain binding. The ready-plan format does not yet select these
optional bindings automatically.

`coordination_evidence.py` owns the version-1 coordination section in the existing
frozen brief: `decision`, `question`, task `inputs`, original `request` and original
classifier `response`, plus `schema_version`. Its brief hash binds execution and
collection. A judge's original Jev state must include
`investigation: {task, assignment, result}` matching the exact current completed
investigator result in the admitted task. A claim that investigation happened is
insufficient. Select a highly capable judge at its maximum supported effort.

`config/coordination.json` selects one `judge_models` entry per harness; arbitrary
models refuse. A Codex judge launch uses the existing curated Codex capacity probe and
requires the selected model's maximum observed supported effort, independent of
catalog order. A Claude judge (`claude-start --role judge`) requires the configured
Claude model at effort `max`, Claude Code's highest level; Claude publishes no quota
metadata, so its frozen evidence carries `judge_harness: "claude"` and no capacity. Missing, malformed, stale or unknown-effort evidence refuses.
The owner freezes `judge_model` and schema-1 `judge_capacity` in the existing
brief; the worker rechecks freshness before its native turn. Collection checks
against the original launch time, so completed judgments do not expire merely
while waiting for collection. Initial model selection is not a quality claim;
refresh it monthly against the live catalog and judgment evaluations.

The version-6 native report adds `answer: {decision, question, choice, missing_input}`.
For a standby reasoner, `completed` selects a non-uncertain/non-unresolved original option with null
missing input. `blocked` supplies a concrete missing input and a null, uncertain
or unresolved choice. The existing `codex-collect` records that original report;
it grants no specialist approval, task acceptance, release permission or retries.

A judge also requires `state.dispute: {question, options}` with at least two
original ruling options. Its `answer` carries `decision`, `question`, `ruling`,
`consequences` and `missing_input`: a completed ruling selects an original option
and supplies concrete consequences. A blocked ruling has null ruling, empty
consequences and a concrete missing input. It does not re-answer the classifier's
resolved/unresolved labels. Original artifact references supply its evidence basis.

```sh
python3 -B skills/factory/runtime/jev.py pending --store /path/to/private/decisions --task-state-dir /path/to/tasks
```

This optional view uses schema 2 with `items` and `resolutions`. Already collected
current native answers close only the matching question. Missing inputs, stale
results, uncollected effects and conflicting attempts remain operator requests.
The view verifies original events and frozen bindings without contacting Rex or
writing a second resolution record. Original classifier records stay unchanged.
Archived answers survive worktree removal if the original repository marker and
collected evidence remain available. Without `--task-state-dir`, the schema-1
classifier-only inbox is unchanged. These are shadow judgments until routing
calibration is approved; callers must still verify actual source freshness.

The native engine path is part of command identity. A relocated/retired engine
produces `engine_unavailable` for operator inspection, never silent adoption or
a new standby proposal. Cross-engine-path adoption is not implemented. Earlier
attempts on stale task inputs remain in history; only current-input attempts can
resolve the current phase.

The authenticated loopback dashboard accepts an optional `--decision-store` and
reads `pending_decisions`/`read_decision` and collected native evidence offline.
Omission means unknown/not configured; a selected absent directory retains the
owner's first-use empty meaning without creating it. Inaccessible, unsupported,
damaged or dangling sources remain unavailable. No Rex/provider query is needed
to read decisions, and malformed saved reasoner arguments do not echo argv.
The dashboard checks source identities/content before and after owner reads;
changes during collection make the inbox explicitly unavailable until refreshed,
without claiming current native answers from a mixed view. No snapshot is stored.

The dashboard shows original question IDs/versions and option descriptions,
evidence references/revisions, and the original dispute question/options for
judging. Generic question instruction contents, raw state, payloads, commands and
native argv stay private. Only explicit investigator/native task bindings to IDs
under the selected task directory can resolve a task; generic state labels/paths
are not interpreted as task records. Generic evidence freshness stays unknown.
Unbound/unknown conditions become operator planning, with original shadow owner
proposals retained separately from current native answers. A missing, blocked,
changed or stale current investigation blocks judge availability even if a native
ruling was previously collected. No original investigation result is rewritten.
These are views/proposals, never automatic routing, dispatch or task acceptance.
