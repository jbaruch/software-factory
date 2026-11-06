# Software Factory

A specialist engineering team coordinated by scripts and Jev, with Rex as its execution surface. Product, design, engineering, security, QA and operations retain their own responsibilities. The foreman handles priorities and exceptional planning decisions; routine delivery does not depend on a foreman turn.

This repository is the **design and executable-contract milestone**. It contains the agreed design, a specialist role catalog, configuration catalogs and deterministic acceptance scenarios. It does not launch workers, call Jev, inspect subscription accounts, merge changes or serve a dashboard yet.

## Read and run

- [Design](docs/design.md): responsibilities, execution, escalation, learning and operational boundaries.
- [Dashboard contract](docs/dashboard.md): delivery, capacity, utilization and overhead definitions.
- [Implementation sequence](docs/implementation.md): the slices required to turn the contracts into a live factory.
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

The Python checks use the standard library. `acr` is a separate prerequisite for package validation. The reference model in `contracts.py` expresses intended observable behavior; passing these scenarios is not evidence that live provider integrations work or that Jev's judgments are accurate.

## Distribution

The root `agent-plugin.yaml` declares the agent-facing skills. The proposed package identity is `jbaruch/software-factory`; no GitHub repository or release is created by this scaffold. ACR validation proves source structure, not publication, real-agent operation or live model access.

The application, dashboard, catalogs and evaluation suite remain application source. ACR does not automatically install an application daemon or undeclared source files. Runtime packaging and native harness integration are separate implementation slices. No hooks are declared until there are real, tested entrypoints to wire.

Specialists load only their assigned skill and relevant task artifacts. Do not preload this README, the entire design or every role into every worker.
