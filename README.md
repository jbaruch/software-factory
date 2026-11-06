# Software Factory

A specialist engineering team coordinated by scripts and Jev, with Rex as its execution surface. Product, design, engineering, security, QA and operations retain their own responsibilities. The foreman handles priorities and exceptional planning decisions; routine delivery does not depend on a foreman turn.

This repository contains the agreed design, a specialist role catalog, configuration catalogs, deterministic acceptance scenarios and a **small native Rex launcher**. The launcher records admitted task scope, enforces declared correction/investigation limits, honors task holds and reconnects without blindly repeating uncertain launches. It does not yet select models, call Jev, schedule subscription allowances, merge changes or serve a dashboard.

## Read and run

- [Design](docs/design.md): responsibilities, execution, escalation, learning and operational boundaries.
- [Dashboard contract](docs/dashboard.md): delivery, capacity, utilization and overhead definitions.
- [Implementation sequence](docs/implementation.md): the slices required to turn the contracts into a live factory.
- [Native Rex launcher](docs/native-rex.md): explicit specialist launches, inspection and limited restart reconciliation.
- [Task state](docs/task-state.md): admission, attempt limits, holds and the state schema.
- `skills/factory/references/roles.json`: the specialist catalog, focused prompts and decision authority.
- `config/providers.json`: the five requested subscription routes, with unknown capabilities and quota sharing preserved explicitly.
- `config/coordination.json`: bounded Jev judgments and execution policies.
- `evals/scenarios.json`: replayable, credential-free acceptance scenarios.

From this directory, with Python 3.11+:

```sh
python3 -m unittest discover -s tests -v
python3 scripts/check_design.py
acr validate . --json
```

Install `requirements-dev.txt` to run `pyright --warnings`. The Factory checks
workflow runs diagnostics, offline tests, design checks and pinned ACR validation.

The Python checks use the standard library. `acr` is a separate prerequisite for package validation. The reference model in `contracts.py` expresses intended observable behavior. Launcher tests use a fake server; the opt-in live smoke check is separate. Passing these checks does not establish complete provider integration or Jev judgment accuracy.

## Distribution

The root `agent-plugin.yaml` declares the agent-facing skills. Source is hosted at [jbaruch/software-factory](https://github.com/jbaruch/software-factory); no package release is published yet. ACR validation proves source structure, not publication, real-agent operation or live model access.

The application, dashboard, catalogs and evaluation suite remain application source. ACR does not automatically install an application daemon or undeclared source files. Runtime packaging and native harness integration are separate implementation slices. No hooks are declared until there are real, tested entrypoints to wire.

Specialists load only their assigned skill and relevant task artifacts. Do not preload this README, the entire design or every role into every worker.
