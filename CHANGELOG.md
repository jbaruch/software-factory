# Changelog

## 0.1.0 — 2026-11-05

### Task admission

Add task scope, acceptance references and an initial Git revision to the native
launcher. Admitted tasks enforce declared cumulative correction and investigation
limits from persisted launch intents. Add pause/resume, offline state inspection
and an owner-managed upgrade for legacy task records. Automatic scheduling and
provider integration remain outside this slice.

### Revision-bound results

Bind new admitted assignments and specialist reports to candidate and requirements
revisions. Preserve stale results as history and require current successful results
for declared launch dependencies. Completed implementation can return a produced
commit for independent review. Existing assignments gain no invented evidence
during the version 2 state upgrade.
Refuse damaged schema downgrades and incomplete request/evidence identities
before they can erase history or authorize dependent launches. Unknown assignment
diagnostics direct the operator to the recorded IDs.

### CI and review

Add independent policy and quality review triggers, Python diagnostics, offline
tests, design checks and pinned ACR validation. Dependency updates run weekly;
the ACR binary pin has a monthly refresh procedure beside its download command.
Add a pinned Gitleaks pre-commit hook and document skill installation.
