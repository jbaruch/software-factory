# Native provider observations

`native_codex.py` reads metadata from the installed Codex CLI through a private
stdio app-server child. It preserves native sign-in and configuration. It does
not read auth files, start a thread or model turn, edit settings, log out, spend
reset credits or send account messages.

```sh
python3 native_codex.py --codex '/absolute/path/to/codex'
```

The probe initializes its own channel, reads the account and rate limits, pages
through the picker model catalog and stops only its own child. Each RPC has a
20-second deadline; catalog traversal is bounded to 20 pages. It emits one
version-1 JSON observation with the resolved binary, native initialization
identity, account response, raw limit constraints and model entries. Retrieval
time uses the wall clock; probe duration uses a monotonic clock. Ordinary tests
inject fake processes and clocks and never start Codex.

The source contract is the official [Codex app-server documentation](https://learn.chatgpt.com/docs/app-server).
The installed native response is authoritative for actual models, supported
efforts, field presence and account observations. A cached catalog is not an
entitlement test. The adapter reports readiness as `unverified` until execution
and lifecycle conformance establish more.

Retain nullable and overlapping constraints. `rateLimitsByLimitId` groups actual
metered buckets; the compatibility `rateLimits` field may repeat one of them.
Never add those duplicate views or overlapping reset windows as extra capacity.
Fresh retrieval does not prove that every upstream field was freshly measured.
Missing usage stays unknown; no percentage, token allowance or dollar value is
invented. The probe itself has measurable coordination overhead.

This command does not write or migrate provider catalogs or operational state.
If its JSON is retained, keep account metadata private and preserve its schema
version, native payload and observation time. Revalidate observations before
using them to schedule work. No local model list establishes cross-route quota
sharing, supported tools or subscription eligibility for other harnesses.

Account, catalog and allowance observations are distinct from native worker
lifecycle and subscription eligibility. Do not substitute API-key billing for
subscription routes. See the [implementation inventory](implementation.md) for
provider coverage.

## Safe Codex capacity snapshot

```sh
python3 codex_capacity.py --codex '/absolute/path/to/codex'
```

`snapshot(binary)` calls `native_codex.probe` once, with no retry or local state.
`curate(observation)` accepts an existing observation without provider access.
Only native observation schema 1 is accepted; missing or unsupported versions
refuse rather than interpreting a different format as capacity.
The version-1 JSON allowlists `observed_at`, resolved `binary`, native `route`,
account `type`/`plan`, `ordinaryUsageAllowed`, `pools` and actual `models`.
Account IDs, email, tokens, raw initialization and credit/billing data are omitted.
Plan comes from native `planType` metadata on the account response, account or
rate-limit response/snapshots when supplied; conflicting plans refuse curation.

Each pool retains its native `limit_id` and separate `primary`/`secondary`
windows: `used_percent`, `remaining_percent` (100 minus observed use),
`duration_minutes` and integer Unix `resets_at`. The windows overlap and cannot
be added; each observed constraint applies to scheduling. Matching default
`rateLimits` and `rateLimitsByLimitId` snapshots are deduplicated by native
identity. Conflicting duplicates, map/identity mismatches and malformed known
fields fail with inspection/update guidance and no partial CLI output.

Missing/null facts remain JSON null, including a null secondary window. Missing
pools or model catalogs remain null, distinct from explicitly empty collections.
Model entries retain the native `model`, optional picker `id`, supported
`effort_ids` and `default_effort_id`; unknown effort choices remain null.
Unknown usage is never unlimited. No credits become quota or dollars, and no
metadata establishes model eligibility, execution readiness or observation
freshness. Scheduling, reservations and views must validate observations for
their own decisions. The implementation inventory owns verification status.

## Grok and Antigravity catalogs

```sh
python3 native_catalog.py grok --binary '/absolute/path/to/grok'
python3 native_catalog.py antigravity --binary '/absolute/path/to/agy'
```

The adapter invokes only `--version` and `models`, with closed stdin and a
20-second deadline per command. It uses native sign-in/configuration, leaving
their contents with the CLI. No prompt, login, worker, settings change or retry
is requested. The parser consumes Grok's model bullet list and Antigravity's
tab-separated ID/name rows. Unexpected rows and duplicate IDs refuse discovery with
inspection/update guidance. An empty list supplies no candidates.

`native_catalog.py` owns its transient version-1 `native-model-catalog` envelope:
harness, resolved binary, reported CLI version, observation time/duration,
ordered `models` (`id`, `name`) and original command stdout in `native_output`.
Native stderr is omitted. Readiness remains `unverified` and
`quota_observation` remains null. This is model discovery, not an account
eligibility, supported-effort or capacity test. The adapter has no persistence
or migration reader; keep retained observations private and refresh before use.

Native usage follows [Grok's CLI reference](https://docs.x.ai/build/cli/reference) and
[Antigravity's model listing](https://antigravity.google/docs/cli/headless/#select-a-model-effort-or-agent).

## Grok authenticated metadata

```sh
python3 native_grok.py --grok '/absolute/path/to/grok' --cwd '/absolute/project/path'
```

The owner script starts one private ACP stdio child and requests only
`initialize` and `authenticate(cached_token)`. It uses the initialization model
state without creating a native conversation or requesting a model turn. The
native catalog reports effort choices per model; missing effort metadata remains
unknown. Native catalogs may be cached and do not prove inference eligibility.

The child disables automatic updates, subagents, web search, the shared leader
and automatic worktree cleanup (`GROK_WORKTREE_AUTO_GC=0`). API-key environment
overrides refuse the probe, including the legacy `GROK_CODE_XAI_API_KEY` name.
Cached authentication must report `Oidc`; interactive login and API-key fallback
are not requested. Only the probe's own child is terminated on completion/failure.

`native_grok.py` owns its transient schema-version-1 `grok-native-metadata`
envelope: route, resolved binary, requested working directory, agent version,
observation time/duration, curated `authentication`, original model entries,
current model ID and native capability metadata. Authentication includes the
chosen method, native mode, nullable subscription tier, access gate and the
native backend-billing flag. A gate contains only its message and optional label.
Email, team identifiers and login URLs are excluded. See the upstream
[auth metadata contract](https://github.com/xai-org/grok-build/blob/2bdd1d6a6369de0e8c68132ea4539e9abd9e14a8/crates/codegen/xai-grok-login/src/meta.rs)
for field meanings; the backend-billing flag is not a monetary usage amount.

Readiness remains `unverified` and `quota_observation` stays null. A clear access
gate and a subscription label supply no quota headroom, pool independence or
worker-lifecycle proof. Consumers use reported model entries, rather than the
current/default model alone, as candidates. Unsupported versions or malformed
metadata require a fresh compatible observation, never inferred eligibility.
The adapter has no persistence/migration reader; keep retained output private.

Tests use fixed ACP replies and fake processes. Live discovery is recorded in
the [implementation inventory](implementation.md).

## Explicit Codex assignments

`factory.py codex-start` builds one native `codex exec` invocation in an admitted
task's Rex pane. Select an actual model and supported effort from fresh native
observations. Existing project policy and operator authority must permit native
delegation before execution. The [implementation inventory](implementation.md)
records execution evidence and eligibility limits.

```sh
python3 factory.py --rex '/absolute/path/to/rex' --server 'unix:///explicit/socket' \
  codex-start --task example --id qa-1 --role qa --goal 'Review the selected candidate' \
  --cwd '/absolute/path/to/clean/worktree' --purpose work \
  --codex '/absolute/path/to/codex' --model 'discovered-model-id' --effort high
python3 factory.py codex-collect --task example --id qa-1
```

Admission and input selection use the [native launcher commands](native-rex.md).
The default worker sandbox is read-only. Inspection commands must work without
creating temporary files; use direct reads such as `sed`/`rg` or `python -c`
instead of shell heredocs. An explicit `--write` selects
`workspace-write`; writers need their own worktree. `--seconds` declares the
execution allowance, initially 600 seconds and bounded to 1–86400 seconds.
Invalid allowances refuse before task reads, locks or attempts. Expiry interrupts only the owned
native child and records failure. An uncertain attempt never retries itself.
The adapter rejects API-key environment overrides and preserves native config;
it does not establish authentication type or eligibility from that check alone.

`codex_worker.py` owns version-4 `*.codex.json` execution receipts and their
private JSONL streams. Each stream record has `schema_version: 1`,
`kind: codex-event-line` and `data`, the unchanged original native line including
its newline. Native fields remain inside that line; the local version belongs
to the worker. The stream reader accepts only version 1. Earlier unversioned raw streams
are legacy version 0 and remain available for inspection, but refuse collection;
they are never rewritten or used as permission to rerun an existing effect.
The observed native CLI can repeat `id` inside web-search telemetry items. The
reader discards both ambiguous IDs from its derived observation and retains the
original line unchanged. Those IDs supply no result or authority. Other duplicate
fields, local envelope keys, lifecycle data and final reports remain strict.
Before starting a child the worker exclusively claims
the stream and writes a `starting` receipt. Existing artifacts refuse a new
execution. A receipt binds the job, input revisions, brief hash, working
directory, frozen Git-local repository marker identity, worktree-specific absolute
`worktree_git_dir`, resolved CLI, requested model/effort
and writer mode. The worker verifies that repository identity before and after
execution. Its native child inherits the verified working directory and uses
`--cd .`, avoiding a second open of a replaceable checkout pathname. It records wall
timestamps, monotonic duration, exit code, native thread ID, compact report,
nullable native usage, resulting Git candidate, stream path and error. A
completed receipt requires finite nonnegative timestamps/duration, a finish at
or after creation, and a present null error. Missing or malformed metadata refuses
collection. Model and effort must be nonblank before a launch can reserve an attempt.
Native events follow the official [non-interactive contract](https://learn.chatgpt.com/docs/non-interactive-mode).
Collection requires thread start, one turn start, reports inside that active turn,
then turn completion in that order. Reordered or repeated lifecycle records refuse
evidence; unrelated telemetry cannot supply a report or completion.
The observed CLI also emits nonfatal `item.completed` error diagnostics between
thread start and turn start. These are retained telemetry and supply no lifecycle
or report evidence; top-level errors and failed turns still refuse collection.
The schema file beside the receipt requires only `outcome` and original
`artifacts` from the specialist; runtime identity and Git facts are gathered by
code. Schema changes belong to the worker. Receipt readers accept only version 4;
version 1 lacks the repository pin and version 2 uses a reusable filesystem pin.
Version 3 binds the repository but not the individual worktree. These versions
remain inspection-only, without migration or a new launch. Existing commands reconnect unchanged; they cannot acquire
retroactive execution evidence.
New native commands freeze the worktree Git directory in their existing command
arguments before launch. Preflight, actual-directory checks, postflight and
collection use that same binding; another linked worktree cannot substitute even
at the same commit. Task records keep their existing schema and command contract.
Provider and event JSON reject duplicate keys, nonstandard numeric constants and
numbers outside the supported finite range, including large integers and otherwise unused fields.

The launcher freezes the resolved native binary path after checking it is accessible and executable
before saving a new assignment or consuming an attempt. Reconnection to an
existing assignment preserves its original command even if that binary is
subsequently removed.

`codex-collect` is the read-only receipt/stream consumer and task-result writer. It
requires the exact owned worker/interpreter command and Rex block to exit successfully, checks the frozen binary and assignment binding,
reparses the original events and compares the resulting report, thread and
usage. Missing, unsupported, damaged or conflicting evidence refuses
collection. The task store remains governed by its own schema. Repeated
collection reuses the same immutable result. Changing task inputs leaves an
older result visible but stale. A native exit or specialist approval does not
accept the task, establish independent verification or satisfy release gates.

See the [implementation inventory](implementation.md) for mechanical integration
observations and the remaining live specialist verification.
