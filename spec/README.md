# Specification copies

The SDK keeps copies of what it implements from the Niadra open specifications and the server's API, and
tests itself against them. `scripts/sync_spec.py` refreshes the generated part:

```
uv run python scripts/sync_spec.py --server ../niadra-back --spec ../niadra-spec
```

## Context Pack schema

`context-pack.v1.json` is a copy of the Context Pack schema of the Niadra open specifications
(JSON Schema 2020-12), and `examples/context-pack-v1-turn-as-data.json` its example of the answer to
a turn, with the turn's slots, as data. `tests/test_spec.py` keeps the SDK's `ContextPack`,
`PackSection`, `PackStamp`, `PackSlot` and `PackGuard` equal to the schema, field by field, and reads
the example. When the specification changes, copy the files again and run the tests.

## The routes of turn records, typed state, signals and coordination

`openapi/cell.json` is the part of the server's OpenAPI document with these routes and every schema they
reach. The script generates the models (`src/niadra/models/turns.py`, `state.py`, `signals.py`,
`coordination.py`) and the route methods (`src/niadra/api.py`) from it, and `tests/test_generated.py`
fails when a generated file differs from what the script writes. A route the server has not built yet
answers 501.

`examples/turn-record/` holds the Turn Record spec's examples; the tests read each through `TurnRecord`.

## Conformance vectors

`vectors/<name>.<version>.json` are the conformance vectors of the specifications, which the server and
both SDKs run alike, and `examples/claim-contract/` the claim contract examples with their negative
corpus. `tests/test_vectors.py` lists every file the SDK runs. A file not published yet is skipped as
pending vectors; a published one the SDK cannot run yet is an expected failure; an unexpected file or an
unknown case field fails.
