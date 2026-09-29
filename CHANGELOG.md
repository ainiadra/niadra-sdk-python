# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/).

## [0.7.0] - Unreleased

The agent core. Every turn an agent takes is recorded in its own process; what it says is checked against
what its tools returned; agents, people and systems coordinate before they contact a customer or act; the
objects a company's systems push reach the agent as typed state with the freshness to say them; each agent
keeps its working state; and a company replays turns, derives its types and measures a tool's
counterfactual in its own CI. The framework adapters record turns. Every feature is off until the space
turns it on, and a space that did not ask sees no change.

The versions published before it were previews: nothing of theirs carries over, and none of their names,
options or fallbacks is kept.

### Added

- `Niadra` and `AsyncNiadra`, twin clients over one transport core, fail-open by default (no exception from
  a public method unless `strict`), and a no-op client without a key.
- `context()` before the model call, cached per conversation and revalidated by ETag, with the customer's
  last turn as `query` (the turn's `slots`), `prefetch()` while the customer speaks, the voice read path,
  `format="json"` for the pack as data (`context-pack.v1`) and `explain=True` for why each slot was chosen.
- History navigation (`search()`, `timeline()`, `open()`) and `tools()`, the same navigation as
  function-calling tools bound to one customer, with `subject_token()` and `mcp_url` for MCP.
- `track()` with a bounded local queue, one batch in flight per client, and a conversation turn sent at once;
  `conversation()` and `task()`; `identify()`, `verify()`, `feedback()`, `feedback_batch()`, `upload_media()`,
  `ingest_status()`, `whoami()`, `object_state()` (the object as a state read serves it, `ObjectRead`) and
  `object_timeline()`.
- Agent memory (`agent_memory()`, `search_agent_memory()`, `remember()`), backed answers (`niadra.backing`)
  and the guard lines a read carries; `niadra.admin` for a key with the `admin` scope.
- `wrap()` for OpenAI-shaped clients and the adapters under `niadra.integrations`, each an optional extra;
  the Dify plugin and the Langflow component in `integrations-extras/`.
- `niadra-mock`, an in-memory emulator of the API with ASGI and WSGI entry points.
- `niadra.api` and `AsyncNiadra.api`: one method per route of turn records, replay and scenarios, typed
  state and the agent's working state, subject signals and measurement, and coordination, named as the
  server names the operation. Unlike the rest of the SDK they raise.
- `niadra.models.turns`, `state`, `signals` and `coordination`: the models of those routes, generated
  from the server's OpenAPI document by `scripts/sync_spec.py`.
- `niadra.turns.digest`: the digest of a turn record's value, SHA-256 over its canonical JSON (RFC 8785),
  as every producer computes it.
- `niadra.state.expr`: niadra-expr, the type registry's expression language, evaluated as the server
  evaluates it: `parse`, `compile_expression` (resolution against a type declaration) and `evaluate` over
  an `Environment`, with the four logical values in `niadra.state.logic`.
- Turn records: `conversation.turn()` records what one turn read, called and said, with the build it
  ran on (`Niadra.build()`), and `@niadra.tool` records each call of a tool of yours inside it, deep-copied
  at the moment, with the objects its result showed. A bounded queue keeps the closed turns (values of
  unflagged turns go first when it is full) and a background sender posts them to `POST /v1/turns` in the
  space's content mode, with the values in your own bucket in `pointer` mode (`turns.store()`). Capture
  costs a tool call 0.05 ms at the 95th percentile for a 25 KB result. `niadra.turns.otel` puts the turn
  on your OpenTelemetry spans (`pip install 'niadra[otel]'`).
- The warm cache for when Niadra is down: `client.profile()` keeps the SDK profile (features, claim
  contract, type registry); `context(include=["constraints", "state"])` reads the constraints block and the
  state view with the pack, and a failed read serves the last good ones, `degraded`; `may_contact()` checks
  an outbound contact against the local copy of the suppression list, which keeps applying with Niadra out
  of reach.
- The claim contract in count mode: what the agent says inside a turn is checked against what its tools
  returned, and each claim goes to the turn record with its verdict; `conversation.claims.check()` runs the
  check on demand. Count mode never changes an output.
- `niadra-mock` serves the SDK profile, the constraints and state blocks, and the suppression list.
- The claim guard that acts: `conversation.claims.guard(stream)` holds what could start a claim until its
  sentence ends (150 ms at most, 300 ms a message) and lets it go as the contract's actions say: a blocked
  sentence gives way to the category's caveat, a stale copy of one field becomes its fresh value only when
  that is unequivocal, a warning marks the claim. `claims.guard_text()` does the same on a whole output. An
  immutable output never changes: a block sends it to a person. The turn record carries the act taken.
- Coordination: `conversation.check()` asks before acting and, when Niadra does not answer within 200 ms,
  decides by the purpose's direction (a customer's message and service go, marketing, retention,
  collection and an effect with a key wait, the local opt-out always holds); `conversation.declare` sends
  what happened in the background until Niadra takes it; `conversation.claim()` holds a lease or a task lock.
- `niadra.contact_gateway()` and `niadra.coordination.verify_contact_token()`: the contact token's offline
  check at your gateway, with the space's public keys kept (`pip install 'niadra[gateway]'`).
- The agent's working state: `conversation.agent_state.get()` and `put()`, compare-and-swap or merge by key,
  reading its own writes, kept and sent again while Niadra is down.
- `niadra.resolvers` and `verify_claim()`: a value not safe to claim is read again by your resolver, inside
  your boundary and within 300 ms, and the fresh value decides; `niadra.content` puts back the text a
  pointer-mode space keeps in your storage.
- Replay inside your boundary: `niadra.replay.Replayer` (and `AsyncReplayer`) runs the turns of a scenario N
  times with the build you pin, answers your tools from the record (`@niadra.tool(dry_run=True)` lets one run
  for real when the record has no answer), keeps everything the agent sends from leaving, evaluates the
  assertions and reports the run, and Niadra answers with the statistical verdict. The agent's working state
  starts empty in a replay and its writes stay there. Runs are numbered from 1, as the replay spec numbers
  them.
- `niadra.replay.overlap_at_k()`: the depth-weighted overlap of two ranked lists that the tool counterfactual
  reports (`spec/counterfactual.md`, 4); it passes the `counterfactual-overlap` vectors.
- The recording the SDK profile serves: a turn leaves in the content mode the space names for the source,
  and a turn without a pin the space requires for replay is kept with a warning, once.
- The `niadra` command: `niadra replay` for your CI (exit 1 on a regression) and `niadra resolver-worker`,
  which serves the space's refresh requests with your resolvers and pushes what they read.
- LangChain: `NiadraCallbackHandler(conversation, turns=True)` records each top-level run as a turn, with
  its tool and model calls.
- Turn records from the framework adapters, with `turns=True`: Google ADK (agent and tool callbacks; a
  sub-agent or an `AgentTool` is a sub-turn), OpenAI Agents (`RunHooks`; a handoff opens a sub-turn) and
  LangGraph (`before_agent`, `after_agent` and `wrap_tool_call` of the middleware) record each run's tool
  calls with the provider's call ids, its model calls with their tokens, and what the agent said.
- The conformance vectors of the open specifications, run by `tests/test_vectors.py`, and the design of
  the turn capture (`docs/design/turn-capture.md`).
- `niadra.claims`: the claim contract's checker, pure and without a model: the number and role parser,
  the category detection, the verdicts and actions, and the 0.90 text anchor, over the categories of the
  contract the SDK profile serves (`ClaimContractSummary`). It passes the `claim-parser`, `claim-detect`
  and `claim-anchor` vectors, and no phrase of the example contracts' negative corpus triggers it.
- `niadra.coordination.destination`: a handle's canonical destination (`phone:+<E.164>` with the
  Brazilian ninth digit, `email:<address>`) and its key per reader, `base64url(HMAC-SHA256(salt, ...))`,
  as the suppression list and the contact token compute them.
- `niadra.exposure`: the exposure token a card carries, `nx1.<id>.<position>.<verifier>`, built and read
  with the refusals of its spec.
- `niadra.constraints.render`: the constraints block rendered for one tool call through the tool's
  binding, in advisory or apply mode, and the count of what the call's results honored.
- The blocks a read asks for reach the model: with `include`, the state view's lines and the constraints
  block go in the turn block after the slots, inside one `<niadra>` section that opens with the pack's
  "data, not instructions" line in the pack's language. A read that asks for no block keeps its turn block
  byte for byte. The context answer also carries the coordination block.
- `niadra.internal_text`: fingerprints of the company's own prompt (8-word shingles, computed and kept in
  the process). A passage the model repeats gives way to the claim contract's `redact` line, and the turn
  records the span with the verdict `internal_text_found` and the prompt's version, never the text.
- `@niadra.tool(binding=...)` records what a call did with the constraints block: the hard constraints its
  arguments sent and, over the objects its result shows, how many were checked, broke one or lacked the
  field. A turn keeps the pack, the block and the working state it read, and what the person was shown or
  engaged with (`frame.interact`).
- `@niadra.tool(mask_output=True)` keeps the fields the key may not read (`deny` removed, `mask` masked, by
  the profile's `field_access`) from what reaches the model; the last profile read keeps applying while
  Niadra is down, and `on_unknown="block"` withholds the output when none was ever read.
- The tool counterfactual: `niadra.replay.Counterfactual` and `niadra counterfactual` take the recorded
  calls of a tool whose arguments carried an element of the constraints block, call the tool again without
  it (dry when it writes state, never without a dry run) and send Niadra only overlaps and positions.
- A replay starts from the working state the recorded turn read. LangGraph and Google ADK tools answer from
  the record, LangChain tools through `replayable()`, and any other LangChain tool is refused with
  `ReplayRefusedError` instead of running live.
- `niadra types derive` proposes an object type from one PostgreSQL table's catalog (never a row) with the
  fingerprint of what it read; `--check` exits 1 on drift and sends Niadra only the fingerprint and the
  counts. It passes the `type-derive` vectors. `niadra contract test` runs the claim contract against its
  negative corpus and example turns, for a company's CI.
- One example per concept of the agent core (`examples/claim_guard.py`, `coordination.py`, `object_state.py`,
  `working_state.py`, `masked_tool.py`, `tool_counterfactual.py`, with `turn_records.py` and
  `replay_demo.py`) and `examples/ci/niadra-checks.yml`, each run by the tests.
- `ContextResponse.budget` (`BudgetBlock`, with `BudgetPack`, `BudgetUse` and `BudgetCut`): with
  `include=["budget"]`, what the pack costs per section, what this agent already spent in the conversation
  and the case, and the units the measurement says it leaves unused. Shown, never enforced.
- `niadra.api.overview()`: the coordination overview in counts (`GET /v1/coordination/overview`).
- `scripts/sync_spec.py --spec` also copies the Context Pack schema (`spec/context-pack.v1.json`), and
  `tests/test_spec.py` holds the answer and every include block to it.
- The tool bindings the space declares come in the SDK profile (`SdkProfile.tool_bindings`, typed
  `ToolBinding`), for this source's tools. A tool without a binding in code measures the constraints block
  and runs its counterfactual through the binding served for its name, and `binding=` in code wins.
  Left unset, `mask_output` follows the served binding's `capabilities.mask_output`. `api.constraints()`
  with `tool` answers the block rendered for that tool, as advice.
- Watch revalidation in `niadra resolver-worker`: a watch fires only on a value its source confirmed. The
  worker serves `watch_revalidation` requests first, pushes every object it read with the `request_id` it
  answers (which settles the request and decides the object's due watches even when the value did not
  change), and releases a request it cannot answer (`POST /v1/state/refresh-requests/{id}/release`), as
  `not_found` when the resolver returns `niadra.resolvers.NOT_FOUND` and `failed` when it fails. A type
  without a resolver, or whose resolver's circuit is open, still waits out its lease. `Resolvers.fetch()`
  says why a read brought no object.
- `ConstraintsBlock.text`: the constraints block as the server writes it for a model, each field by its
  type's label and each operator in words, in the space's language. The turn block places it as it places the
  state view's text.
- The claim contract reads the computed values a state read serves (`ObjectRead.values`) as evidence, by
  their name, while they are claim-safe; the claim guard takes as evidence every value the include blocks
  placed in the turn block (`niadra.turns.claims.block_values(state, constraints)`). A value that is not
  claim-safe backs nothing.
- A hedged number is no claim (the claim contract spec, 5.4): "I can't confirm the $24.90 still applies" or
  "$689.00, not $612.00" state no such price; `niadra.claims.hedged` finds them.
