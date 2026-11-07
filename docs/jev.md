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
python3 jev.py compile --question task-demand --question correction-disposition
python3 jev.py models
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
python3 jev.py evaluate --state /path/to/relevant-state.json \
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
python3 jev.py evaluate --state /path/to/relevant-state.json \
  --model '<discovered-model-name-or-alias>' --model-version '<expected-jev-version>' \
  --evidence-revision '<source-revision>' \
  --question task-demand --store /path/to/private/decisions
python3 jev.py pending --store /path/to/private/decisions
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
