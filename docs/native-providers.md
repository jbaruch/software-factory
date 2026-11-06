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
subscription routes.

## Grok and Antigravity catalogs

```sh
python3 native_catalog.py grok --binary '/absolute/path/to/grok'
python3 native_catalog.py antigravity --binary '/absolute/path/to/agy'
```

The adapter invokes only `--version` and `models`, with closed stdin and a
20-second deadline per command. It uses native sign-in/configuration, leaving
their contents with the CLI. No prompt, login, worker, settings change or retry
is requested. The catalog formats were observed with Grok Build 1.0.70 and
Antigravity CLI 1.5.1/1.5.2. Unexpected rows and duplicate IDs refuse discovery with
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

## Explicit Codex assignments

`factory.py codex-start` builds one native `codex exec` invocation in an admitted
task's Rex pane. Select an actual model and supported effort from fresh native
observations. Existing project policy and operator authority must permit native
delegation before execution. This adapter has offline conformance coverage;
live specialist execution and eligibility remain unverified.

```sh
python3 factory.py --rex '/absolute/path/to/rex' --server 'unix:///explicit/socket' \
  codex-start --task example --id qa-1 --role qa --goal 'Review the selected candidate' \
  --cwd '/absolute/path/to/clean/worktree' --purpose work \
  --codex '/absolute/path/to/codex' --model 'discovered-model-id' --effort high
python3 factory.py codex-collect --task example --id qa-1
```

Admission and input selection use the [native launcher commands](native-rex.md).
The default worker sandbox is read-only. An explicit `--write` selects
`workspace-write`; writers need their own worktree. `--seconds` declares the
execution allowance, initially 600 seconds. Expiry interrupts only the owned
native child and records failure. An uncertain attempt never retries itself.
The adapter rejects API-key environment overrides and preserves native config;
it does not establish authentication type or eligibility from that check alone.

`codex_worker.py` owns version-1 `*.codex.json` execution receipts and their
private JSONL streams. Each stream record has `schema_version: 1`,
`kind: codex-event-line` and `data`, the unchanged original native line including
its newline. Native fields remain inside that line; the local version belongs
to the worker. The reader accepts only version 1. Earlier unversioned raw streams
are legacy version 0 and remain available for inspection, but refuse collection;
they are never rewritten or used as permission to rerun an existing effect.
Before starting a child the worker exclusively claims
the stream and writes a `starting` receipt. Existing artifacts refuse a new
execution. A receipt binds the job, input revisions, brief hash, working
directory, resolved CLI, requested model/effort and writer mode. It records wall
timestamps, monotonic duration, exit code, native thread ID, compact report,
nullable native usage, resulting Git candidate, stream path and error. The
native events follow the official [non-interactive contract](https://learn.chatgpt.com/docs/non-interactive-mode).
The schema file beside the receipt requires only `outcome` and original
`artifacts` from the specialist; runtime identity and Git facts are gathered by
code. Schema changes belong to the worker. No older receipt versions exist.

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

The complete CLI → Rex → receipt → collection path has also been exercised
against a harmless local event fixture on Rex API revision 11. Reconnection
reused the same block. A timed-out fixture was interrupted and refused
collection. These are mechanical integration observations; no native model turn
or specialist judgment was performed by that check.
