# Working on Software Factory

Read the relevant part of `docs/design.md` before changing a coordination contract. Keep specialist judgment distinct from mechanical execution and classifier routing. Every Jev question must offer an explicit `uncertain` outcome.

Run `python3 -m unittest discover -s tests -v`, `python3 scripts/check_design.py` and `acr validate . --json` for relevant changes. Ordinary tests use recorded decisions/fake transports; they must not contact providers or manipulate live Rex sessions. The explicitly invoked `scripts/smoke_rex.py` is a separate live conformance check that creates only its own test session and panes.

Keep instructions short and role-specific. Store operational facts once and generate views from them. A passing package validator or contract test does not establish live integration, judgment quality, subscription eligibility or release readiness. Preserve the user's existing authorization boundaries and repository coding/release policy.
