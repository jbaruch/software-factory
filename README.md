# Software Factory

A specialist engineering team coordinated by scripts and Jev, with Rex as its execution surface. Product, design, engineering, security, QA and operations retain their own responsibilities. The foreman handles priorities and exceptional planning decisions; routine delivery does not depend on a foreman turn.

This repository contains the agreed design, a specialist role catalog, configuration catalogs, deterministic acceptance scenarios and a **small native Rex launcher**. The launcher runs explicitly supplied commands in specialist panes and reconnects without blindly repeating uncertain launches. A separate Jev adapter compiles and evaluates bounded questions in shadow mode. Automatic model selection, Jev routing, subscription scheduling, merge execution and the dashboard remain unimplemented.

## Read and run

- [Design](docs/design.md): responsibilities, execution, escalation, learning and operational boundaries.
- [Dashboard contract](docs/dashboard.md): delivery, capacity, utilization and overhead definitions.
- [Implementation sequence](docs/implementation.md): the slices required to turn the contracts into a live factory.
- [Native Rex launcher](docs/native-rex.md): explicit specialist launches, inspection and limited restart reconciliation.
- [Jev adapter](docs/jev.md): question compilation, model discovery and explicit shadow evaluation.
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

The Python checks use the standard library. `acr` is a separate prerequisite for package validation. The reference model in `contracts.py` expresses intended observable behavior. Launcher tests use a fake server; the opt-in live smoke check is separate. Passing these checks does not establish complete provider integration or Jev judgment accuracy.

## Distribution

The root `agent-plugin.yaml` declares the agent-facing skills. The proposed package identity is `jbaruch/software-factory`; no GitHub repository or release is created by this scaffold. ACR validation proves source structure, not publication, real-agent operation or live model access.

The application, dashboard, catalogs and evaluation suite remain application source. ACR does not automatically install an application daemon or undeclared source files. Runtime packaging and native harness integration are separate implementation slices. No hooks are declared until there are real, tested entrypoints to wire.

Specialists load only their assigned skill and relevant task artifacts. Do not preload this README, the entire design or every role into every worker.
