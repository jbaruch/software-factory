# Native provider observations

## Execution policy

Every native agent runs unattended, without approval prompts or a harness
sandbox: Codex with `--dangerously-bypass-approvals-and-sandbox`, Claude Code and
Grok with `--permission-mode bypassPermissions`, Antigravity with
`--dangerously-skip-permissions`. A factory nobody sits in front
of cannot wait on permission dialogs, and a sandbox that refuses ordinary build,
test and network commands turns every assignment into an approval queue. The
boundary is structural instead:

- each writer edits only its own provisioned worktree, and each reviewer reads a
  clean one at the selected candidate. Every reviewer works in a disposable
  worktree of the candidate beside its receipt (`TASK.ID.HARNESS.checkout`),
  removed after the run with every ignored file, and may run tests and other
  commands there: Codex, Antigravity and Grok reviewers get every tool, and Claude
  reviewers get Read, Glob, Grep and Bash. Grok also loads the operator's MCP
  servers whatever `--tools` says, and one of them wrote into a shared writer
  worktree (#85). Every Grok assignment also runs with `--deny mcp__*`: Grok
  reaches MCP tools through `use_tool` even under `--tools read_file`, and the
  deny rule refuses those calls, as `--disallowedTools mcp__*` does for Claude. Tests, installs and MCP output never dirty the shared checkout;
- a writer's ignored outputs (test caches, bytecode, installs) and the empty
  directories Git cannot track are discarded before the factory commit and listed
  in the receipt's `commit.discarded`;
- a writer that fails after editing leaves its worktree dirty, so
  `factory.py reset --task TASK --id ID` saves the leftover edits as
  `TASK.ID.HARNESS.reset.patch` beside the receipt and restores the candidate.
  The failed receipt stays failed; a new writer can cite the patch. A writer
  that failed after its own factory commit keeps that commit: reset clears only
  leftovers, and the coordinator selects the commit with `inputs` and verifies it
  with a plain plan. A failed reader that left files in a shared checkout resets
  the same way and reruns under a new ID;
- agents never stage, commit, push or merge. `factory_commit.py` stages, scans for
  secrets, runs hooks and signs, and integration stays with the runtime;
- only a structured report counts as a result. Text, tool telemetry and
  permission labels never supply completion, acceptance or authority.
- a read-only verifier's verdict needs at least one tool call behind it. A
  reader that returns any outcome without calling a tool (Claude's
  `StructuredOutput` carries the report and doesn't count) is recorded `failed`.
  A release judgment is exempt: its evidence is the frozen payload in its brief.
  Two Grok qa runs approved, then blocked, after one turn of thought and no reads.

A reviewer's tools are its interface, not a fence: verifying a change means running
it, so reviewers run commands in their disposable checkout. Claude coordination
answers keep Read only. Native user configuration that a CLI imports, such as
plugins, hooks, MCP and LSP servers, is the operator's environment. It is recorded,
not refused. Receipts and streams recorded under the earlier confined modes still
decode and collect.

`native_codex.py` reads metadata from the installed Codex CLI through a private
stdio app-server child. It preserves native sign-in and configuration. It does
not read auth files, start a thread or model turn, edit settings, log out, spend
reset credits or send account messages.

```sh
python3 -B skills/factory/runtime/native_codex.py --codex '/absolute/path/to/codex'
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

## Sanitized native Claude authentication

```sh
python3 -B skills/factory/runtime/native_claude.py --binary '/absolute/path/to/claude' --cwd '/absolute/trusted/directory'
```

`probe(binary, cwd)` invokes only `--version` and
`--safe-mode --restricted auth status --json`, with closed stdin, the explicit
accessible executable and trusted cwd, and a 20-second timeout per command.
There is no retry, prompt, model turn, login/logout, paid API fallback or settings
change. Our code opens no auth/config files; the CLI owns native authentication.
`curate(output, returncode=...)` is pure and accepts original JSON text.

The minimum CLI version is **2.1.317**. After strict version-response validation,
the adapter compares numeric version components and refuses older versions with
update guidance before invoking auth status. The primary
[2.1.317 changelog](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md#21317)
records the fix for auth status exiting before a slow token refresh was saved,
which could sign out the next run. The adapter never updates the CLI or repairs
credentials. Renew compatibility review and candidate-bound metadata conformance
when CLI changes affect command flags, auth-status fields/exits or authentication
persistence; a newer version number alone does not establish compatibility.

The primary [Claude CLI reference](https://code.claude.com/docs/en/cli-reference)
defines exit 0 as signed in and exit 1 as signed out. Both are successful metadata
observations when JSON agrees. Supported `authMethod` values are `none`,
`claude.ai`, `oauth_token`, `api_key`, `api_key_helper` and `third_party`.
Signed out requires `none`; signed in requires a non-`none` method. A genuine
API-key/helper or third-party sign-in remains a sign-in fact and supplies no
subscription eligibility. The conservative `apiProvider` allowlist is
`firstParty`, `bedrock`, `vertex`, `foundry`; changed values require adapter review.
Missing/wrong types, unknown methods/providers, contradictory status/exit,
duplicate keys, nonfinite numbers (even in omitted fields), malformed JSON,
unsupported exits and invalid UTF-8 refuse with sanitized recovery guidance.

Safe mode suppresses customizations while preserving native auth and permissions;
restricted mode ignores user/project/local settings. Managed policy still applies,
including permitted policy-configured commands. These flags do not establish
model-tool confinement or suppress every policy effect. `--bare` skips OAuth and
keychain authentication and is unsuitable here; `--print` is never requested.
CLI 2.1.319 flag compatibility and a signed-out native status were supplied as
admission evidence; they do not qualify execution or this candidate's live probe.

Children receive the existing curated `native_environment()`. Claude/Anthropic
and cloud provider environment selectors are refused, including empty values,
before either child starts; remove overrides only when native account observation
is intended. An explicit `CLAUDE_CONFIG_DIR` must be an accessible absolute
directory and is forwarded unchanged, never silently replaced with the default.
Assignment workers also get `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`. A background
shell still running when the agent returns its result wakes the session again, so
one run ends with two results that the worker cannot settle.
Only path metadata is checked, not directory contents. Other credentials and
runtime injection variables are not forwarded. Path/timeout/process failures
emit no native diagnostics and no partial observation JSON.

`native_claude.py` owns the transient schema-1 `claude-native-auth` envelope:
`harness`, resolved `binary`/`cwd`, validated `cli_version`, wall-clock
`started_at`/`observed_at`, monotonic `probe_seconds` and `auth` containing only
native `loggedIn`, `authMethod`, `apiProvider`. `readiness` stays `unverified`;
`quota_observation` and `subscription_eligibility` stay null. No model list,
subscription tier or allowance pool is inferred. Raw stdout/stderr, email,
account IDs, credential/token fields and auth-payload paths are omitted.
The adapter persists/migrates nothing and changes no task/route state. If retained,
keep observation provenance private and refresh before any independent eligibility
or scheduling decision. The [inventory](implementation.md) owns verification status.

## Safe Codex capacity snapshot

```sh
python3 -B skills/factory/runtime/codex_capacity.py --codex '/absolute/path/to/codex'
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
python3 -B skills/factory/runtime/native_catalog.py grok --binary '/absolute/path/to/grok'
python3 -B skills/factory/runtime/native_catalog.py antigravity --binary '/absolute/path/to/agy'
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

## Antigravity native quota

```sh
python3 -B skills/factory/runtime/native_antigravity.py --binary '/absolute/path/to/agy' --cwd '/absolute/path/to/worktree'
```

This runs only `--version` and `--print /usage`, which the native CLI handles
without a model turn. Version-1 `antigravity-native-usage` observations retain
the binary, cwd, CLI version, timestamp, probe duration, original usage rows and
native group labels, weekly percentages remaining and reset timestamps. The
reader requires the observed four-column format, unique groups, percentages in
0–100 and timestamps with timezones. Missing or changed data refuses rather than
becoming unlimited capacity. It infers no five-hour window, model-to-group map,
account entitlement, reservation or worker readiness.

API/ADC environment overrides refuse, including empty values. Native children
receive the shared curated environment; process/format failures report a recovery
action without echoing native diagnostics or retrying. The command writes
version-1 snapshots for inspection; it adds no persisted-observation reader or
scheduler. Old catalog observations keep their original unknown quota and remain
unchanged.
The native [headless command guide](https://antigravity.google/docs/cli/headless/)
describes standalone slash-command reports; the
[usage guide](https://antigravity.google/docs/cli/commands/usage/) describes quota groups.

## Antigravity retained execution decoder

`native_antigravity.parse_execution(events, expected_model=..., expected_cwd=...,
expected_schema=...)` consumes original JSON lines without I/O. Supply an explicit
model, absolute cwd and frozen schema for exactly `{outcome, artifacts}`: required
string outcome enum (a nonempty subset of factory outcomes), string-array artifacts
and `additionalProperties: false`. It supports the retained CLI 1.5.3 shapes only.

Require one first `init`, one last `result`, matching conversation IDs, init
model/cwd/schema, terminal schema, `SUCCESS`, one DONE user-input step, ordered
contiguous steps ending in DONE finish, and a supported `structured_output`.
When present, `response` must strictly decode to the same report. Duplicate JSON
keys, invalid/nonfinite numbers, failed/malformed/reordered/duplicate events and
trailing data refuse with inspection guidance. Warnings are never stripped.
Text, tool parameters and permission labels supply no report or authority.

The derived result contains `report`, `conversation_id`, `init_model`, `cwd`,
`turns`, terminal `usage` and `duration_seconds`. Missing/null usage or duration
stays null; absent counters stay unknown. Token counters must be nonnegative
integers or null; duration must be finite and nonnegative or null. Step usage is
validated but never summed into terminal usage. Init model is an echo, not actual
inference identity. No dollars, quota, entitlement, confinement, acceptance or
launch/process success is inferred. The caller must retain immutable original
streams/provenance and independently require the original process exit.

This additive parser changes no CLI, usage snapshot, task or receipt format and
migrates no stored data. Rollback is removal of the decoder/version change by the
integration owner; no irreversible data effects occur. Keep historical failures
unchanged. The canonical [inventory](implementation.md) records qualification
limits. The [Antigravity worker](#explicit-antigravity-assignments) uses it.

## Explicit Antigravity assignments

Antigravity assignments run under the [execution policy](#execution-policy) with
every native tool. They use an admitted task, selected candidate/requirements and
a discovered model and effort (`low` to `max`), as with
[native launcher commands](native-rex.md):

```sh
python3 -B skills/factory/runtime/factory.py --rex '/absolute/path/to/rex' --server 'unix:///explicit/socket' \
  antigravity-start --task example --id inspect --role qa --goal 'Inspect the selected candidate' \
  --cwd '/absolute/path/to/clean/worktree' --purpose work \
  --agy '/absolute/path/to/agy' --model 'discovered-model-id' --effort high --seconds 600
python3 -B skills/factory/runtime/factory.py antigravity-collect --task example --id inspect
```

`antigravity_worker.py` runs `agy --print= --input-format stream-json
--output-format stream-json --json-schema SCHEMA --model MODEL --effort EFFORT
--dangerously-skip-permissions` and writes the frozen brief to stdin as one
`{"event":"user","message":{"role":"user","content":[{"type":"text",...}]}}`
line, so briefs never meet argument-length limits. Some models refuse `--effort`:
`agy` 1.5.3 rejects it for `claude-opus-4-6-thinking` ("--effort is not supported
for model") while the Gemini models accept it, and `agy models` does not say which
is which. Omit `--effort` from `antigravity-start` (or set `"effort": null` in a
ready plan) and the worker runs the model without the flag; the receipt records a
null requested effort. The factory does not guess this from model names. The CLI names the conversation
at `init`; there is no session flag or turn bound. Gemini API and ADC environment
overrides refuse. Before the model turn the worker records a fresh
[usage observation](#antigravity-native-quota). The strict decoder above reads
the original lines; collection re-reads them, binds the receipt and requires the
original successful Rex exit.

Code review requires `--base`, whose evidence is embedded in the frozen brief.
`--write --commit-message MESSAGE --sandbox /absolute/path/to/codex` makes the
assignment an implementation specialist for a writing role: the brief carries the
task goal plus the shared writer contract, and `factory_commit.py` stages, scans,
signs and commits a completed report. Receipts are version 3 for reviewers
(`checkout`: the disposable worktree they ran in, which collection decodes against)
and version 2 for writers (`writer`, `sandbox`, nullable `commit`); version-1
reviewer receipts, which reviewed in place, still collect. A completed run copies each artifact
that is a file in agy's per-conversation brain directory into a private
`TASK.ID.antigravity.artifacts/` beside its receipt, because agy does not keep
that directory; the receipt and report stay unchanged. Events are
version-1 `antigravity-event-line` envelopes holding each original line.
Integration counts collected `qa`, `security` and `code-review` approvals under
their conversation ID and treats writers as contributors; a writer that failed
before `init` is identified by its assignment.

On 2026-11-09, live 1.5.3 runs in this mode with Gemini 3.8 Flash (High) went
through `antigravity-start`/`antigravity-collect`. A reviewer approved a fixture
candidate. A writer ran the tests with `run_command`, fixed the code with
`edit_file` and left Git alone. The factory then committed the same tree the
Claude and Grok writers produced, and both panes closed on collection.

## Grok authenticated metadata

The metadata command applies the same native configuration checks as the
[inspection worker](#explicit-grok-inspection-assignments) before starting its
ACP agent in `bypassPermissions` mode with only the `read_file` tool.

```sh
python3 -B skills/factory/runtime/native_grok.py --grok '/absolute/path/to/grok' --cwd '/absolute/project/path'
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

Grok assignments run under the [execution policy](#execution-policy). The native
`strict` sandbox blocked the CLI's own startup on 1.0.77, so reviewers run in
`bypassPermissions` mode with every built-in tool, in their disposable checkout. The command interface uses an admitted task, selected
candidate/requirements and discovered model/effort, as with
[native launcher commands](native-rex.md):

```sh
python3 -B skills/factory/runtime/factory.py --rex '/absolute/path/to/rex' --server 'unix:///explicit/socket' \
  grok-start --task example --id inspect --role qa --goal 'Inspect the selected candidate' \
  --cwd '/absolute/path/to/clean/worktree' --purpose work \
  --grok '/absolute/path/to/native/grok' --model 'discovered-model-id' \
  --effort 'discovered-supported-effort' --seconds 600 --turns 30
python3 -B skills/factory/runtime/factory.py grok-collect --task example --id inspect
```

For `--role code-review`, also supply `--base` with the full base commit ID.
The factory never guesses a review range. Before a new launch, `review_evidence.py`
adds a version-1 JSON section to the existing frozen brief: `candidate`,
`requirements`, nullable `base`, the candidate's tracked `paths`, and nullable
`changed_paths`/`diff`. An explicit base produces both changed paths and the full
binary-capable patch from that base to the selected candidate. Git external diff,
text conversion, hooks and fsmonitor callbacks are disabled for this read. The
brief's existing SHA-256 covers the evidence; no second task store is created.
The worker validates its revision binding and passes the brief with native
`--prompt-file`, avoiding command-argument size limits. Git/encoding failures
refuse before a model launch. Evidence gathering uses local objects and never
chooses the base or judges whether the change is correct.

Reconnecting an existing effect preserves its original brief and range. Historical
code-review assignments without a base remain inspection-only; they cannot gain
review evidence retroactively. Historical non-review briefs stay unchanged.

`--write --commit-message MESSAGE --sandbox /absolute/path/to/codex` makes the
assignment an implementation specialist for a role whose catalog entry permits
writing. The brief carries the task goal instead of review evidence, and `--base`
is refused. Grok then runs with every built-in tool. After a completed report,
`factory_commit.py` stages, scans, runs hooks, signs and commits the work exactly
as it does for any other writer. A writer's receipt is version 3: version 2's
fields plus `writer: true`, the `sandbox` path and nullable `commit` evidence.
Collection validates that commit, binds the result to the produced candidate and
refuses reviewer receipts presented as writers or the reverse.

A reviewer gets every built-in tool in `bypassPermissions` mode, in its
disposable checkout. It disables native subagents, web search and automatic updates, preserves native subscription configuration and excludes host credential
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
without configuration contents or credentials. Imported hooks, plugins, MCP and
LSP servers are accepted and recorded as `no_executable_customizations`; malformed
discovery reports refuse. Project trust is not required. Inspection precedes the
metadata agent and model child, but does not freeze the configuration either child
consumes. It changes no global configuration.
Unknown quota stays unknown;
these explicit commands do not add automatic Grok scheduling to the ready runner.

`--seconds` accepts 1–86400 and `--turns` accepts 1–1000. Invalid bounds refuse
before task locks; worker deadlines interrupt only the owned child. A local exit
does not prove a provider request was cancelled. Inspect the original native
session, receipt and stream before authorizing a new attempt after an unknown
outcome. The native CLI installation must remain outside the checkout/Git metadata.

`grok_worker.py` owns version-2 reviewer and version-3 writer `*.grok.json` receipts and version-1 `*.grok.jsonl` streams.
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
Execution and collection share validation of discovery metadata and configuration
evidence, including each layer's absolute path, nonempty role and SHA-256 digest,
the CLI version and finite nonnegative observation timing. Collection validates
the retained observation without rereading mutable profile files.
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
python3 -B skills/factory/runtime/factory.py --rex '/absolute/path/to/rex' --server 'unix:///explicit/socket' \
  codex-start --task example --id qa-1 --role qa --goal 'Review the selected candidate' \
  --cwd '/absolute/path/to/clean/worktree' --purpose work \
  --codex '/absolute/path/to/codex' --model 'discovered-model-id' --effort high
python3 -B skills/factory/runtime/factory.py codex-collect --task example --id qa-1
```

Admission and input selection use the [native launcher commands](native-rex.md).
Every worker runs with `--dangerously-bypass-approvals-and-sandbox` under the
[execution policy](#execution-policy). An explicit `--write` makes the assignment a
writer; writers need their own worktree. `--seconds` declares the
execution allowance, initially 600 seconds and bounded to 1–86400 seconds.
Invalid allowances refuse before task reads, locks or attempts. Expiry interrupts only the owned
native child and records failure. An uncertain attempt never retries itself.
The adapter rejects API-key environment overrides and preserves native config;
it does not establish authentication type or eligibility from that check alone.
Owned workers disable both native multi-agent features for these leaf assignments.
The native child receives only the locale/path/temp and subscription-configuration
settings allowed by `native_environment()`; host credential variables are not forwarded.
Read-only assignments (verifiers and judgments) never get the operator's GitHub
identity, after a release judgment merged a PR and deployed it: `GH_CONFIG_DIR`
points at a factory directory whose `hosts.yml` (mode 0600) holds the read-only
token from `~/.config/software-factory/github-read-token` (mode 0600) when it
exists, so no token sits in the environment for an `env` dump to log, and pushes
to GitHub are rewritten to an invalid host. This stops a misbriefed agent, not a
hostile one: an unconfined agent can still reach the operator's credentials on
disk. Create
that token as a fine-grained personal access token with read-only Contents,
Issues, Pull requests and Metadata on the repositories the factory serves;
without it, readers can't read private GitHub issues or pull requests. Writers keep
the operator's identity: the factory owns their commits and nothing pushes.

New Codex model children additionally use per-run
`allow_login_shell=false`, `features.shell_snapshot=false` and
`shell_environment_policy.experimental_use_profile=false`. Each child gets one
private, initially empty absolute directory as `ZDOTDIR`, both in a fresh native
environment copy and in the TOML-encoded `shell_environment_policy.set.ZDOTDIR`
override. The [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
defines the controls: omitted login uses nonlogin execution, explicit login
requests are rejected, and shell snapshots/profile loading are disabled. Native
HOME/CODEX_HOME, authentication, sandbox, model, effort and deadlines are preserved.
Children no longer pass `--strict-config`: tessl writes its own `tessl` tables
into each checkout's `.codex/config.toml`, which Codex ignores with a warning
and strict mode turned into a startup failure for every assignment.
Shared environment consumers, other providers and factory Git helper environments
are unchanged; existing receipts and reconnect/collection need no migration.

The directory remains until the owned child's exit is confirmed. Native cleanup
finishes before optional factory Git work. Allocation, spawn, configuration,
stream and cleanup failures retain failed receipts without retry or fallback;
cleanup diagnostics append to any earlier failure. Unconfirmed termination keeps
the directory and its recovery path in the receipt. Reconcile that owned child
before removing only its directory. Abrupt worker death can leave an orphan;
there is no automatic sweeper.

This targets unintended ordinary shell startup. Explicit environment values can
still be removed by inclusion filters; strict config is not a universal survival
check. Changed CLI, shell, filters or managed configuration require requalification,
without clearing filters or adding automatic preflight. System startup and
intentional sourcing remain outside this claim. The
[security cookbook](https://learn.chatgpt.com/docs/enterprise/security-cookbook)
describes why environment filtering is not credential confinement. See the
[canonical inventory](implementation.md) for observed compatibility and remaining
native qualification.

Writer CLI installations must remain outside the assigned checkout and Git
metadata; the resolved executable is reused for the factory commit sandbox. Writer
mode requires a native CLI binary, a Python runtime outside writer-controlled
paths, and no checkout/Git metadata entries in PATH. Script-form CLI wrappers
refuse; select their installed native binary directly.

New writer assignments require the selected role catalog entry's `write_allowed`
permission. The worker checks the role and bound brief before creating artifacts
or starting the native CLI, including with an alternate Python interpreter.
Read-only verification roles refuse writer execution; historical read-only
assignments retain their original reconnect and collection behavior.

For a factory-owned writer commit, supply `--write --commit-message` with the
intended commit message. The specialist edits and tests in its own worktree
and leaves Git staging/commit to the runtime. `factory_commit.py` owns that step
independently of which agent edited the worktree. It confines Git, hooks and
signing with the native Codex CLI's local `sandbox` command, which makes no model
call and uses no subscription allowance; that sandbox binary is a factory tool,
not the writing agent. Only a successful native run with a
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
local commit for those configurations. A hooks directory inside the checkout
whose every hook Git ignores, such as husky's `.husky/_` that `npm install`
generates, is the writer's own output: it is discarded with the other ignored
output, listed in the receipt's `discarded`, and never run. Required
checks/signatures are never silently disabled.
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

Native model commands follow the [execution policy](#execution-policy). Writers may
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
python3 -B skills/factory/runtime/factory.py --rex '/absolute/path/to/rex' --server 'unix:///explicit/socket' \
  codex-start --task example --id writer-1 --role documentation \
  --goal 'Update the admitted documentation and report original evidence' \
  --cwd '/absolute/path/to/isolated/writer/worktree' --purpose work \
  --codex '/absolute/path/to/discovered/codex' --model 'discovered-model-id' \
  --effort 'discovered-supported-effort' --write --commit-message 'docs: clarify writer invocation'
python3 -B skills/factory/runtime/factory.py codex-collect --task example --id writer-1
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
code. Explicit bounded coordination assignments additionally return the typed
answer described in [bounded native answers](jev.md#bounded-native-answers) and
use version-6 receipts. Generic reports remain unchanged. The frozen question
and native event report must agree at execution and collection.
Schema changes belong to the worker. Receipt readers accept versions 4, 5 and 6;
version 4 has no factory-owned commit and remains collectable unchanged with its
original command. Version 5 requires the nullable `commit` field. Neither version
is migrated or rewritten by collection. Version 6 retains the version-5 metadata
and nullable commit field and requires its bound read-only answer. Versions below 4 remain inspection-only:
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

## Explicit Claude assignments

`factory.py claude-start` and `claude-collect` use the existing admitted task,
locks, cumulative attempts, frozen brief and Rex pane. Select `--claude` with an
absolute installed executable, a discovered `--model`, an `--effort` of `low`,
`medium`, `high`, `xhigh` or `max`, explicit `--role`, `--goal`, `--cwd`,
`--purpose` and stable task/assignment IDs. The model and thinking effort belong
to the assignment, not the provider: pick what the role's work needs. Optional
`--seconds` (1–86400, default 600) and `--turns` (1–1000, default 30) bound the
attempt. Code review requires a full `--base`; the existing revision-evidence
owner embeds that exact range in the original brief. There is no automatic retry,
scheduling route, API fallback or release-judgment path.

`--write --commit-message MESSAGE --sandbox /absolute/path/to/codex` makes the
assignment an implementation specialist for a role whose catalog entry permits
writing. Claude then runs with `Read`, `Edit`, `Write`, `Glob`, `Grep` and `Bash`
in `bypassPermissions` mode with no permission prompts, under the
[execution policy](#execution-policy). A writer that changes Git HEAD fails the
factory commit's HEAD check. The decoder checks tool-call shapes, not paths:
reads, searches and edits outside the checkout decode, as a shell command could
reach them anyway, and the factory commit takes only the worktree. After a completed report, `factory_commit.py`
stages, scans, runs hooks, signs and commits the work exactly as it does for any
other writer, using the Codex CLI only as its local sandbox. Writer briefs carry
the task goal instead of review evidence, and `--base` is refused.

`--decision-store STORE --decision ID --question QUESTION` makes a read-only Claude
assignment answer one bounded Jev question as the standby reasoner or the judge,
like the Codex coordination path. Launch freezes the original question in the brief
instead of review evidence, the report schema adds the bounded `answer`, and the
version-3 receipt carries it. Collected answers close their decision-inbox item.

`claude_worker.py` preserves native HOME/config selection and checks the sanitized
`native_claude.probe` before inference. Execution accepts Claude Code 2.1.319 or newer
with claude.ai/firstParty sign-in only. Newer releases run until the strict decoder
meets a shape it does not recognize, which refuses visibly rather than guessing. API/provider/auth environment overrides refuse even when
empty. The private frozen brief goes to stdin with EOF. Safe mode; Read, Glob, Grep
and Bash plus internal StructuredOutput for reviewers, which run in a disposable
checkout and write version-4 receipts with its `checkout` path (version 1 reviewed
in place with Read only and still collects); Read only for coordination answers;
empty strict MCP configuration,
disabled slash commands/Chrome, `bypassPermissions` and no session persistence
are per-invocation controls. They do not establish subscription eligibility.
Claude Code 2.1.319 emits `system`/`ui_invalidate` rendering notices in bypass
mode, sometimes before `init`; the decoder accepts them as inspection-only
telemetry. After `init` it also accepts `system/api_retry` (the CLI retrying a
transient API failure; `attempt`, `max_retries` and `retry_delay_ms` must be
nonnegative integers) and the lifecycle of long or backgrounded Bash calls:
`tool_progress` heartbeats, `system/task_started`, `task_updated`,
`task_notification` and `background_tasks_changed`. Each must name a Bash call or
task the stream already showed. None of these supplies a report, usage or
permission, and the final result stays authoritative.

A writer's receipt is version 2: version 1's fields plus `writer: true`, the
`sandbox` path and nullable `commit` evidence with the same shape and checks as a
Codex writer's. Collection validates that commit, binds the result to the produced
candidate and still refuses reviewer receipts presented as writers or the reverse.

The worker owns private version-1 `<task>.<id>.claude.json` receipts and
`<task>.<id>.claude.jsonl` events. Receipts require kind `claude-execution`, state
`starting`, `completed` or `failed`, job, candidate/requirements inputs, repository
marker, individual worktree Git directory, cwd, role, original brief hash, binary,
requested model/effort, sanitized auth discovery, native session, seconds/max_turns,
created/finished/elapsed times, exit code, report, usage/model_usage, turns,
rate_limits, candidate, events path and error. Terminal fields start null;
completion requires finite nonnegative timing, successful exit and null error.
Each version-1 `claude-event-line` record carries `data`, the unchanged original
native line including newline. `claude_worker.RETAINED_STREAM_LIMIT` bounds each
attempt's cumulative retained stream to 16 MiB (16,777,216 bytes of serialized
JSONL envelopes, including envelope newlines, not native payload bytes). The worker
checks before writing each envelope or appending its decoded event; overflow stops
the child and saves an actionable failed receipt with the original retained prefix.
Collection applies the same byte bound through `factory.read_record(max_bytes=...)`
before parsing or retaining decoded events; it never rewrites oversized evidence.
This is a stream bound, not a task-attempt allowance. Native lines are bounded to 1 MiB; invalid UTF-8
stays escaped inspection evidence and refuses collection. Original events retain
native list-price equivalents, which are not actual monetary spend. Raw native
rate-limit observations retain unknowns and never establish shared pools.

`native_claude.parse_execution` is the pure strict decoder. The worker writes;
`factory.claude_evidence`, collection and integration read the original receipt,
brief and events without migration. Init cwd/model/session/tool controls, assistant
model/session, matched tool results, one structured report and successful final
result/usage must agree. Unsupported/malformed/ambiguous lifecycles, tool escapes,
missing reports, conflicting reports, non-success and timeouts refuse. Unknown
system subtypes and malformed telemetry still refuse. The observed 2.1.319 `system/thinking_tokens`
events (50/delta 50 and 234/delta 184) are accepted only within the initialized
session before the final result. For this subtype, only `estimated_tokens`,
`estimated_tokens_delta` and `session_id` are consumed: both estimates are required
nonnegative integers, excluding booleans, and `session_id` must match the frozen
native session binding. Optional per-event `uuid` is unvalidated opaque raw
metadata, distinct from that binding; missing, null, boolean or other JSON values
do not change decoding or grant authority. No arithmetic continuity or reset
semantics are assumed. Events stay unchanged in the raw stream. Estimates supply
no usage, quota, spend, permission or report
authority; final actual usage remains required. The original failed
[operational-loop architecture receipt](../../software-factory/.factory-state/operational/operational-loop.architecture.claude.json),
[stream](../../software-factory/.factory-state/operational/operational-loop.architecture.claude.jsonl)
and [brief](../../software-factory/.factory-state/operational/operational-loop.architecture.md)
are operator-local evidence, not portable fixtures, and remain unchanged and
uncollected; offline decoding does not qualify that assignment
as successful. A fresh strict assignment and the full operational-loop task remain
separate pending work.

Unknown or legacy receipt/event versions remain inspection-only. First collection additionally
requires the original checkout/context and actual successful owned Rex exit.
Only strict collection sets the existing schema-9 `native_collected` provenance;
manual results never grant it. Historical validation retains original artifacts,
immutable complete result, admitted repository marker and candidate Git objects.
It cannot refresh stale inputs or turn an old failure into approval.

Original bounded shape evidence remains in
[no-tools-observation.json](../../software-factory/.factory-state/claude-lifecycle/no-tools-observation.json),
[read-only-observation.json](../../software-factory/.factory-state/claude-lifecycle/read-only-observation.json)
and their original private
[no-tools stream](../../software-factory/.factory-state/claude-lifecycle/claude-no-tools.stream.jsonl) /
[Read stream](../../software-factory/.factory-state/claude-lifecycle/claude-read-only.stream.jsonl).
The 6.943s supplied defect review and 4.812s canary Read check are observations,
not quality/readiness claims: own Read succeeded; direct outside and symlink reads
were denied; exact-pane reconnect preserved artifacts. Refused dirty/fixture
repository launches remain unlaunched. Scratch conformance used the generic
non-repository entry point. New managed live qualification remains foreman work.
