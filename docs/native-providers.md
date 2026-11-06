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

Claude, Grok and Antigravity need their own account, catalog, allowance and native
lifecycle adapters. Do not substitute API-key billing for their subscription
routes. The static provider catalog remains explicitly unverified.
