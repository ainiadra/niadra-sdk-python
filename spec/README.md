# Context Pack schema

`context-pack.v0.json` is a copy of the Context Pack schema of the Niadra open specifications
(JSON Schema 2020-12), and `examples/context-pack-as-data.json` its example of a pack answered as
data. `tests/test_spec.py` keeps the SDK's `ContextPack`, `PackSection` and `PackStamp` equal to it,
field by field. When the specification changes, copy both files again and run the tests.
