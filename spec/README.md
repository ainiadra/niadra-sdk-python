# Specification copies

The SDK keeps copies of what it implements from the Niadra open specifications and the server's API, and
tests itself against them. `scripts/sync_spec.py` refreshes the generated part:

```
uv run python scripts/sync_spec.py --server ../niadra-back --spec ../niadra-spec
```

## Context Pack schema

`context-pack.v1.json` is a copy of the Context Pack schema of the Niadra open specifications
(JSON Schema 2020-12): the answer to a read, with the pack as data and the blocks a read asks for by
`include` (constraints, state, coordination and budget). `examples/context-pack/turn-as-data.json` is its
example of the answer to a turn, with the turn's slots, as data. `tests/test_spec.py` keeps `ContextResponse`,
each block's model and the pack models (`ContextPack`, `PackSection`, `PackStamp`, `PackSlot`, `PackGuard`)
equal to the schema, field by field, and reads the example.

## The routes of turn records, typed state, signals and coordination

`openapi/cell.json` is the part of the server's OpenAPI document with these routes and every schema they
reach. The script generates the models (`src/niadra/models/turns.py`, `state.py`, `signals.py`,
`coordination.py`) and the route methods (`src/niadra/api.py`) from it, and `tests/test_generated.py`
fails when a generated file differs from what the script writes. A route of a feature the space did not
turn on answers 404.

`examples/turn-record/` holds the Turn Record spec's examples; the tests read each through `TurnRecord`.

## Conformance vectors

`vectors/<name>.<version>.json` are the conformance vectors of the specifications, which the server and
both SDKs run alike, and `examples/claim-contract/` the claim contract examples with their negative
corpus. `tests/test_vectors.py` lists every file the SDK runs. A missing file, an unexpected file or an
unknown case field fails.
