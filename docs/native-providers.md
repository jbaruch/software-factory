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
Duplicate comparison uses curated pool facts, so omitted/null unknowns and
irrelevant private metadata do not create extra pools or false conflicts.
An ID-less default alongside a nonempty ID-keyed view refuses curation; its
relationship to those pools is unknown and cannot supply extra capacity.
Pools also preserve nullable `rate_limit_reached_type` and boolean
`spend_control_reached`. Positive window capacity and `ordinaryUsageAllowed`
do not override these per-pool constraints. Their conflicts refuse duplicate
curation; false remains distinct from unknown. Non-null `individualLimit`
constraints are unsupported and refuse curation with update guidance rather
than exposing spending amounts or dropping a constraint.

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

The metadata command applies the same native configuration/trust and executable-
customization checks as the [inspection worker](#explicit-grok-inspection-assignments)
before starting its ACP agent. Unsupported profiles remain held without a model turn.

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

`native_grok.parse_execution` decodes the observed Grok 1.0.77 headless event
format after strict JSON decoding. It requires one final `end` event with
`end_turn`, the frozen native session/directory, a request identity and a turn
count within the declared bound. Only `structuredOutput` supplies the report;
provisional text and tool telemetry never supply completion or acceptance.
Native usage and reported model identities remain separate from requested model
selection, quota and monetary spend. Missing usage stays unknown. This decoder
is used by the explicit worker and collector below.

## Explicit Grok inspection assignments

Use an admitted task, selected candidate/requirements and an actual discovered
model/effort, as with [native launcher commands](native-rex.md):

```sh
python3 factory.py --rex '/absolute/path/to/rex' --server 'unix:///explicit/socket' \
  grok-start --task example --id inspect --role qa --goal 'Inspect the selected candidate' \
  --cwd '/absolute/path/to/clean/worktree' --purpose work \
  --grok '/absolute/path/to/native/grok' --model 'discovered-model-id' \
  --effort 'discovered-supported-effort' --seconds 600 --turns 30
python3 factory.py grok-collect --task example --id inspect
```

This slice permits only the built-in `read_file` tool, with explicit write, edit,
shell, MCP, web, workflow and subagent denies and the native `strict` sandbox.
Source reads are explicitly allowed; model file reads of the default and selected
Grok profile directories are denied. It disables native subagents, web search and automatic
updates, preserves native subscription configuration and excludes host credential
environment variables. API-key overrides, including empty ones, refuse. Metadata
preflight requires cached OIDC sign-in, a reported subscription, no access gate,
and the requested model/effort in the actual catalog. Before a model turn,
`grok inspect --json` identifies native configuration layers. The worker reads
those TOML layers and refuses authentication, model, endpoint, experiment,
campaign and version overrides rather than reproducing native precedence.
Opaque/unreadable layers and API, endpoint, auth-provider or configuration-overlay
environment overrides also refuse. All inherited `GROK_*` variables except
`GROK_HOME` and the runtime-controlled worktree-GC setting refuse, including
empty issuer/client/proxy overrides. `GROK_HOME` reaches both native children;
the recorded configuration includes its resolved root and layer paths/hashes,
without configuration contents or credentials. Native discovery must report
the assigned project trusted and no executable hooks, enabled plugins, MCP or
LSP servers. This check precedes the metadata agent as well as the model child;
project/compatibility discovery cannot silently add executable tools. The command
holds unsupported profiles rather than changing the user's global configuration.
Unknown quota stays unknown;
these explicit commands do not add automatic Grok scheduling to the ready runner.

`--seconds` accepts 1–86400 and `--turns` accepts 1–1000. Invalid bounds refuse
before task locks; worker deadlines interrupt only the owned child. A local exit
does not prove a provider request was cancelled. Inspect the original native
session, receipt and stream before authorizing a new attempt after an unknown
outcome. The native CLI installation must remain outside the checkout/Git metadata.

`grok_worker.py` owns version-2 `*.grok.json` receipts and version-1 `*.grok.jsonl` streams.
Every stream record has `schema_version: 1`, `kind: grok-event-line` and `data`,
the unchanged native line including its newline. Invalid bytes stay escaped
inspection evidence and refuse collection. Native lines are limited to 1 MiB.
The event reader accepts only version 1; unversioned captures and unsupported versions
remain inspection-only. Neither reader migrates or rewrites old evidence.

The exclusively claimed receipt records job/input revisions, repository marker,
worktree Git directory, cwd, role/brief digest, resolved binary, requested model/
effort, native session, declared seconds/turn bound, preflight discovery snapshot,
version-2 subscription-configuration observation and its probe duration,
timestamps/duration, exit code, final report/request identity, native usage/model
usage, actual turn count, candidate, stream path and error. Before completion,
result/timing/exit fields are null. State is `starting`, `completed` or `failed`.
The discovery retains its own native metadata schema version and probe duration.
No quota or monetary spend is inferred from native token/cost telemetry.

Collection requires successful owned worker exit, a completed version-2 receipt
with finite nonnegative timing and null error, exact frozen command/input/role
bindings, and agreement with the original final event. It revalidates repository
marker and worktree Git directory; replaced linked checkouts refuse. Reports stay
bound to their assessed candidate when current task inputs change. Repeated
collection reuses the immutable result. Task records keep their existing schema;
generic historical Grok commands are not adopted as managed worker evidence.
Version-1 execution receipts and earlier version-2 receipts containing a version-1
configuration observation predate these capability checks. They remain unchanged
and inspection-only, without being upgraded into capability evidence.

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
Owned workers disable both native multi-agent features for these leaf assignments.
The native child receives only the locale/path/temp and subscription-configuration
settings allowed by `native_environment()`; host credential variables are not forwarded.

Writer CLI installations must remain outside the assigned checkout and Git
metadata; the resolved executable is reused for later sandbox commands. Writer
mode requires a native CLI binary, a Python runtime outside writer-controlled
paths, and no checkout/Git metadata entries in PATH. Script-form CLI wrappers
refuse; select their installed native binary directly.

New writer assignments require the selected role catalog entry's `write_allowed`
permission. The worker checks the role and bound brief before creating artifacts
or starting the native CLI, including with an alternate Python interpreter.
Read-only verification roles refuse writer execution; historical read-only
assignments retain their original reconnect and collection behavior.

For a factory-owned writer commit, supply `--write --commit-message` with the
intended commit message. The specialist edits/tests within its workspace sandbox
and leaves Git staging/commit to the runtime. Only a successful native run with a
`completed` report can enter this step. The runtime retains an intent before
status/staging can execute a configured filter, copies shared regular outputs to
independent files without changing their other links, rejects non-regular files,
records the staged tree, scans its filenames, raw blobs, commit message and
effective author/committer identities with native Gitleaks, then runs one
local Git commit with the repository's supported hooks, signing and author settings.
Install Gitleaks outside the checkout, Git metadata and temporary directories.
Use the version pinned in `.pre-commit-config.yaml`; refresh the native tool when
the weekly Dependabot hook update changes that pin.
Preflight verifies that version before launching the writer. Unstamped upstream
Go builds require native Go to verify the embedded module/version metadata.
The mandatory scan uses built-in rules through a generated configuration; source
configuration, ignore files and `gitleaks:allow` comments cannot disable it.
It runs even without a pre-commit hook. Missing tools, scanner errors or findings
retain the intent and refuse the commit. Scanner output is redacted and never
copied into the receipt. Ordinary tests inject a fake scanner.
Validated inherited Git configuration also applies to status/staging/commit.
The operator-approved exception in `AGENTS.md` permits configured helpers to load
writer-edited source. The initial automatic mode supports an optional pre-commit
hook in the common/worktree Git hooks directory, unsigned commits or file-based
SSH signing with the operating system's signer. Active external hook directories,
clean/smudge/process filters, other executable hook types, custom
signing programs and partial clones refuse visibly; use an inspected explicit
local commit for those configurations. Required checks/signatures are never
silently disabled.
Factory commits use UTF-8 and refuse in-progress merges, cherry-picks, reverts,
rebases and bisects before native launch and again before staging. Finish or
abort the existing Git operation in the assigned checkout before retrying.

Staging and the final signed commit run with executable Git callbacks disabled.
Pre-commit runs separately with no signing agent or inherited Git auth settings.
It receives a disposable Git view with a private staged index and sanitized
configuration. Original Git configuration and shared metadata are denied; shared
objects and hook programs are readable. Only its own temporary directory is
writable under Git metadata. Changes to that private index refuse rather than
silently bypassing a hook's staging decision. Hook source edits remain subject to
final candidate validation. Only the callback-free commit phase receives the
short-lived signer; callback processes cannot connect even to a known socket.
The agent holds only the configured key, expires it after five minutes, closes
with its owner and cannot load PKCS#11/FIDO libraries. The user's agent is unchanged.

Each Git invocation uses a fresh native CLI permission profile, with network
access disabled, an allowlisted validated environment and read-only configuration
and caches. The standalone CLI itself and required npm vendor files are readable;
its arbitrary parent directory is not granted. Pre-commit uses a dedicated virtual
environment or the factory Python installation and a dedicated cache named
`pre-commit`. Supplemental grants reject broad home/temp roots and source/Git
metadata overlap. Other Python runtimes expose only their executable, standard
library and libpython files. Unsupported access fails with
retained intent and no host fallback. Launch/verification guards compare the index
and raw source bytes without worktree status, filters, external diffs or text
conversion. macOS additionally receives its native socket option for the signer;
Linux uses the filesystem socket grant without that macOS option.

Run the separate local conformance fixture explicitly:

```sh
python3 scripts/smoke_git.py --codex '/absolute/path/to/codex'
```

It creates only temporary repositories and test signing keys, verifies a real
signed commit, tests arbitrary signing, sibling-index mutation and Git auth
exposure attempts in the callback phase, and refuses a generated credential fixture
without a hook. It then removes its fixtures. It makes no model turn or Rex call
and does not qualify a native assignment.
`tests/test_smoke_git.py` covers orchestration, JSON output and failure cleanup
with fake native transports and disposable Git repositories. The live command
remains a separate explicit check.

Native model commands retain their declared sandbox. Writers may
start from an admitted subdirectory; source checks still use the worktree root.
No changes produce no empty commit. Commit errors or lost responses preserve the
intent and worktree for inspection; they never retry the native job or commit.
The worker disables Python bytecode generation for its child and Git helpers. Other disposable
test/build outputs belong outside the source checkout. This step never pushes,
accepts the task or satisfies independent verification/release requirements.
Host wrappers/workers disable bytecode writes; new worker commands also ignore
Python environment/startup overrides. Historical
commands and version-4 receipts remain reconnectable/collectable without rewriting.

For an admitted writer task, replace the paths and discovered model/effort below
(see [operator-approved exceptions](../AGENTS.md#operator-approved-exceptions)):

```sh
python3 factory.py --rex '/absolute/path/to/rex' --server 'unix:///explicit/socket' \
  codex-start --task example --id writer-1 --role documentation \
  --goal 'Update the admitted documentation and report original evidence' \
  --cwd '/absolute/path/to/isolated/writer/worktree' --purpose work \
  --codex '/absolute/path/to/discovered/codex' --model 'discovered-model-id' \
  --effort 'discovered-supported-effort' --write --commit-message 'docs: clarify writer invocation'
python3 factory.py codex-collect --task example --id writer-1
```

Inspect the retained receipt, stream and worktree before authorizing any retry.

`codex_worker.py` owns version-5 `*.codex.json` execution receipts and their
private JSONL streams. Each stream record has `schema_version: 1`,
`kind: codex-event-line` and `data`, the unchanged original native line including
its newline. Native fields remain inside that line; the local version belongs
to the worker. Native event lines support up to 1 MiB, including verbose command
results. Larger lines fail the attempt with a recovery diagnostic; inspect the
existing pane/retained stream before authorizing another attempt. The stream reader
accepts only version 1. Earlier unversioned raw streams
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
Version 5 adds nullable `commit` evidence. A factory-owned commit records `state`
(`preparing`, `committing`, `created` or `unchanged`), input `parent`, requested
`message`, nullable `staged_tree` and nullable produced `candidate`. Intent is saved
before staging and again before committing. Pre-commit runs against a disposable
index; changes to that private index refuse the commit. The candidate stays bound
to the factory-recorded staged tree, and the clean source check binds the output
assessed by later verification. A completed receipt requires a terminal commit state matching
its frozen assignment, the candidate's actual tree equal to `staged_tree`, and,
for a new commit, the actual Git parent. Collection revalidates the retained
assigned checkout's repository marker and frozen worktree Git directory for both
receipt versions, and requires its exact HEAD to equal the candidate for every
writer, including writers without a factory commit. Keep that checkout until
collection completes.

The schema file beside the receipt requires only `outcome` and original
`artifacts` from the specialist; runtime identity and Git facts are gathered by
code. Schema changes belong to the worker. Receipt readers accept versions 4 and 5;
version 4 has no factory-owned commit and remains collectable unchanged with its
original command. Version 5 requires the nullable `commit` field. Neither version
is migrated or rewritten by collection. Versions below 4 remain inspection-only:
version 1 lacks the repository pin and version 2 uses a reusable filesystem pin.
Version 3 binds the repository but not the individual worktree.
Existing commands reconnect unchanged; they cannot acquire
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
