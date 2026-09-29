# Examples

One script per integration, the same code the documentation shows. Each needs its extra
(`pip install 'niadra[livekit]'`, for instance), a Niadra source key in `NIADRA_API_KEY` and the
provider's own key. To try them without Niadra's cloud, start the local emulator with
`niadra-mock` and set `NIADRA_BASE_URL=http://127.0.0.1:8765`.

The tests under `tests/integrations/` run the same wiring against the emulator with each
framework's real types and a scripted model, in CI.

## Concepts

One script per concept of the agent core, each tested against the emulator in `tests/test_examples.py`:

| Concept | Example |
| --- | --- |
| Turn record | `turn_records.py`: a turn's read, tool call, model and answer, with Niadra up or down |
| Claims | `claim_guard.py`: a price that disagrees with the tool, and the company's prompt held back |
| Coordination | `coordination.py`: a farewell that goes once, whichever agent tries it |
| Object state | `object_state.py`: a value said only when it may be claimed now, read fresh when stale |
| Working state | `working_state.py`: sub-agents that write their own fields of one state |
| Field access | `masked_tool.py`: a tool's output as the key may read it |
| Replay | `replay_demo.py`: a deterministic agent to replay in CI |
| Tool counterfactual | `tool_counterfactual.py`: whether the hard constraints change what a search returns |
| Type derivation | `ci/niadra-checks.yml`: `niadra types derive --check` and `niadra contract test` in CI |
