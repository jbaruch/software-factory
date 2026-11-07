# Working on Software Factory

Read the relevant part of `docs/design.md` before changing a coordination contract. Keep specialist judgment distinct from mechanical execution and classifier routing. Every Jev question must offer an explicit `uncertain` outcome.

Run `python3 -m unittest discover -s tests -v`, `python3 scripts/check_design.py` and `acr validate . --json` for relevant changes. Ordinary tests use recorded decisions/fake transports; they must not contact providers or manipulate live Rex sessions. The explicitly invoked `scripts/smoke_rex.py` is a separate live conformance check that creates only its own test session and panes.

Keep instructions short and role-specific. Store operational facts once and generate views from them. A passing package validator or contract test does not establish live integration, judgment quality, subscription eligibility or release readiness. Preserve the user's existing authorization boundaries and repository coding/release policy.

## Operator-approved exceptions

For Software Factory's runtime and explicitly invoked conformance checks, native provider CLIs may execute operator-authorized specialist assignments through the factory's Rex adapter. This does not authorize the coding agent's own native subagents, team tools or delegation wrappers outside those factory runtime checks. User scope, task limits, isolated worktrees, permissions and release gates remain binding.

For this repository's operator-admitted writer tasks, explicitly selecting
`--write --commit-message` also authorizes factory-owned local Git staging/commit
and the repository's configured hooks, filters and signing helpers with normal
host access, including when they load writer-edited source. This exception does
not expand native model command permissions or authorize pushes, merges,
publication, task acceptance or bypassed CI/review gates.

The operator temporarily waives waiting for Claude quality review while its subscription capacity is unavailable, expected to return Saturday afternoon, 2026-11-07 (America/Chicago). Current-head policy review and required CI still gate merges. Restore the Claude review requirement when capacity returns; never record the waived review as an approval.
