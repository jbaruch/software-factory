# Jev HTTP adapter

`jev.py` compiles the versioned Choice catalog into the current TypeSafe HTTP
contract. It uses the Python standard library, batches selected questions in one
request and preserves explicit `uncertain` answers, distributions, confidence,
actual model identity and observed token usage. It selects no provider, launches
no worker and grants no acceptance or release authority.

The integration follows the official [System One API](https://docs.typesafe.ai/api/system-one),
[Choice contract](https://docs.typesafe.ai/primitives/choice) and
[model discovery API](https://docs.typesafe.ai/api/list-models).

## Compile and discover

```sh
python3 jev.py compile --question task-demand --question correction-disposition
python3 jev.py models
```

Compilation is offline and credential-free. Live commands read `TYPESAFE_API_KEY`
from their execution environment. Alternatively, pass `--key-file` before the
subcommand to read a private file containing only the key. The path is explicit;
the adapter does not search other repositories or credential stores. Keep the
file outside Git, with mode 0600. Credentials are not printed. HTTP redirects are
refused, and error bodies are not echoed.

Use a model name returned by discovery. No model name, access entitlement or quota
is guessed. An empty catalog remains empty. TypeSafe access is separate from the
native subscriptions used for specialist execution.

## Shadow evaluation

```sh
python3 jev.py evaluate --state /path/to/relevant-state.json \
  --model '<discovered-model-name>' --evidence-revision '<source-revision>' \
  --question task-demand --question correction-disposition
```

For `finding-recipient` or `workflow-recipe`, supply `--candidates /path/to/candidates.json`.
The file maps question IDs to maps of actual eligible IDs and their descriptions:

```json
{"finding-recipient": {"qa-worker": "Owns acceptance coverage for this candidate"}}
```

The generic `candidate` placeholder never reaches inference. Real options are
expanded alongside `no_match` and `uncertain`. A supplied empty candidate set has
only those fallback outcomes. Eligibility and authority remain code decisions;
provide the relevant candidate capabilities and source evidence in state.

Evaluation emits a `schema_version: 1`, `mode: shadow` JSON envelope with the
evidence revision, question versions, request digest and validated response.
It does not persist or cache results. A request digest is an audit aid, not proof
of evidence freshness or stable model aliases. Application caching must bind
actual model/version, relevant evidence, question/version and candidate set.

HTTP failures, invalid answers, missing questions and unavailable usage fail
visibly without retry. A selected Choice must have the maximum probability;
ties are accepted. HTTP diagnostics distinguish request repair, authentication,
rate limits and unavailable service without printing response bodies.
The runtime must retain their pending work; generic
uncertainty needs standby reasoning, while uncertainty after investigation goes
to the judge. This adapter does not implement those queues or dispatch reasoners.

No confidence threshold has been calibrated or approved for consequential
routing. Evaluate representative labeled and held-out cases, including sampled
confident errors, before enabling answer-to-action mappings. Typed answers and
valid distributions do not establish judgment quality. Ordinary tests use fixed
fake transports; live evaluation must be invoked separately.
