# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.11.5] - 2026-10-09

### Fixed

- A voice read keeps its turn's budget whatever the connection. The cold-connection allowance
  (`Timeouts.connect`) took the first voice read of a new client to 1.2 s with a slow memory, past a
  1 s voice turn; `context(view="voice")`, `search`, `timeline`, `open`, `object_state` and
  `object_timeline` with `voice=True`, and the first read of a call (`begin`), now never grow past their
  own budget (`Request.ceiling`). Chat and task reads keep the first-read budget and the allowance.

## [0.11.4] - 2026-10-08

### Changed

- The generated models follow the server as it is now: the constraints block's `omitted`, derived values'
  `partial`, paging cursors, a handoff's `open_objects_more`, agent notes' `left_out`, kept failed batches
  (`kept`, `api.failed_intake`, `api.retry_failed_intake`), the data issue kinds `budget_reached`,
  `row_failed` and `inference_limit`, and an action's `result` as long as an event's text (200,000).
- An agent's working state is kept whole: `over_cap` is gone, and `valid_until` is `None` when the agent's
  type names no retention.
- A replay case's `expires_at` is `None` while the space sets no retention for recordings: turn recordings
  are kept until the customer sets one.
- `ProfileMemory.timeline_next` carries where the timeline's next page starts.

### Fixed

- Agent notes and action results were refused on this side below the server's limits: `remember()` and
  the `remember` tool now take titles up to 300 characters and bodies up to 20,000 (it was 120 and 2,000),
  an action's `result` up to 200,000 (it was 2,000), and an update's tags keep the server's 8. The
  canonical tool definitions are regenerated from the server, the same file as the TS SDK.
- `AgentMemory.left_out` is read: how many notes the block's size left out, each kept and searchable.
- A turn record cut to the lists the server takes (claims, interactions, coordination, effects, event keys) now says it is `partial`; before, only calls, blobs and reads did, and the other cuts were silent.
- A 404 from `/v1/context` that was not about the blocks (an object or a profile the API does not know) no
  longer stops the client from asking for `include` blocks for ten minutes. The blocks count as refused only
  when the same read without them answers.
- A `constraints` block the read asked for and the server could not read is said in the turn block, in the
  space's language, instead of being left out in silence. `Context.unread_blocks` lists the blocks asked and
  absent.

## [0.11.3] - 2026-10-08

### Changed

- The generated models follow the server's current specification: the instruction fields `contact` and
  `only_with` on constraints and signals, and the `field_held` data issue kind.

### Removed

- A counter the benchmark target wrote and never read.

## [0.11.2] - 2026-10-07

### Added

- `staff(namespace, id)` names someone who works for your company by the id one of your systems gives them
  (`SubjectKind.STAFF`). A conversation with staff is internal; a conversation with anyone else is
  customer-facing, whatever the key, so one key can serve your team and your customers. Nothing changes
  for code that does not use it.

## [0.11.1] - 2026-10-05

### Fixed

- The first read of a conversation, task or object in a client gets `Timeouts.context_first` (1.0 s, plus
  the measured round trip) when `context` is left at its default, and so does the first agent memory read.
  The API compiles the pack on that read: one right after a customer's first message took 0.41 s on the
  server, the 0.30 s default plus the round trip cut it short, and the agent answered without memory.
  Later reads of the same key keep the short budget. A `context` you set stays a ceiling.

## [0.11.0] - 2026-10-05

The server gives every item one id, a bare UUID, on every surface. This release follows it and removes
the forms it no longer sends or accepts.

### Changed

- History rows (`search`, `timeline`, `open`) carry each row's own id as a bare UUID; `kind` says what the
  row is. `open()` takes the `id` of an `episode` or `object` row as listed, and `OpenedItem.kind` is
  `episode` or `object`.
- `feedback()`, `admin.correct()`, `admin.fact_history()` and `closes` send the id as given: a bare UUID.
  The SDK no longer strips a `fact:` prefix; the server refuses a prefixed id.
- `HistoryFilters.item_statuses` is a list like the other filters, empty by default and always sent.
- The emulator (`niadra_mock`) gives events and episodes bare UUIDs and finds the kind of an opened id
  itself.
- The canonical tool definitions name the kind of an openable row instead of an id prefix.

### Removed

- The `merged` open item status, `HistoryItem.merged_into`, and `OpenedItem.status` and `merged_into`:
  the server derives one id per item, so twins are not created and nothing is merged.

## [0.10.5] - 2026-10-03

### Added

- `HistoryFilters.item_statuses` (`open`, `overdue`, `resolved`, `merged`) lists open items by status in
  `search` and `timeline`; named, the timeline lists open items beside what happened. Unset, it is not sent and
  the rows are what they were: the open and overdue items and the promises kept in the last 7 days.
- `HistoryItem` reads `object`, `expected_operation`, `status`, `closed_at`, `closed_by` (what closed a
  resolved item, as its `open_item.closed` webhook said it, a `ClosedBy`) and `merged_into` (the item a merged
  twin lives on in).

### Fixed

- `open("open_item:<id>")` answered with a validation error: `OpenedItem.kind` only took `episode` and
  `object`. It now reads `open_item`, with `status` and `merged_into` (the item a merged id answers for), and
  any kind `/v1` adds later.

## [0.10.4] - 2026-10-03

### Fixed

- A read made before the startup probe measured the round trip used the bare default budget, and timed out
  from Sao Paulo (the first context read carrying the customer's turn took 674 ms). While the startup probe is
  on its way and a connection is open, `Timeouts.context` and `Timeouts.navigation` left at their defaults get
  `Timeouts.connect` on top; with no connection open the transport already adds it, never twice. A probe that
  failed, or `VoiceOptions(probe=False)`, leaves the defaults as they are.

## [0.10.3] - 2026-10-03

### Added

- `keep_warm` (on by default): while a conversation or task is open (created and not ended) and the client was
  used in the last 10 minutes, the client sends `GET /healthz` every 100 s when nothing else went out for 90 s,
  one attempt with a 2-second budget, so the connection to the region stays open. A turn after a pause longer
  than the 120 s an idle connection is kept no longer pays TCP, TLS and often DNS again (from Sao Paulo, a read
  after 150 s idle took 550 ms instead of 200, and about 900 ms when the DNS entry had also expired). The pings
  are not the client's use; a conversation nobody holds any more stops them. `keep_warm=False` never pings.

## [0.10.2] - 2026-10-03

### Changed

- The client measures the round trip to the region once when it starts (`GET /healthz`, twice), not only
  for voice, which also opens the connection the first read uses. `Timeouts.context` and
  `Timeouts.navigation` left at their defaults take the measured round trip on top: from Sao Paulo (170 ms to
  us-east-2) every chat read with the defaults ran out of time. A value you set stays a ceiling, and the
  client logs one warning when the round trip plus 50 ms exceeds it. The voice budgets are unchanged, and
  their warnings now come only once the client reads in voice. `VoiceOptions(probe=False)` measures nothing.

## [0.10.1] - 2026-10-03

### Added

- `context_use(since=, until=, group_by=, source_id=, channel=, view=, experiment_group=)`: the context-use
  report (`GET /v1/context-use`) as `ContextUseReport`, with its buckets typed and the other blocks in
  `extra`. A key of an `analyst` source with the `analytics` scope reads every source of the space.
- `link(person, organization, role=...)` and `end_link(link_id)`: a system of record that knows who works for
  whom (a CRM, an HR system) links a person to the account or partner they act for, and ends the link, with a
  key that has the `identity:link` scope instead of `admin`. `can_see_contacts` still needs `admin`. `Link` and
  `LinkRequest` are the models.

## [0.10.0] - 2026-10-03

### Added

- `gov_id(number, country)` and `org_registry(number, country)`: a national document as a handle. The number
  travels as typed and the space keeps only a keyed hash of it.
- `open(..., about=)`: with the person bound in `subject`, also an item of the organization they act for that
  their view shows (the pack's account block). A conversation's `tools()` passes its `about`, so the bound kit
  reaches what the pack shows.
- `about_unlinked` on `Context`, `SearchResponse` and `TimelineResponse`: `about` named an organization with no
  active link to the subject, and the read went on with the subject's own memory. A conversation logs it once
  on the `niadra` logger.
- `APIError.explained()` and `niadra.errors.explain()`: an API refusal for a log line, with its code, detail and
  request id.
- `HistoryItemKind` includes `system_event`.
- `niadra-mock` ships `py.typed`.

### Changed

- `str(error)` of an `APIError` carries the code, the API's detail and the request id, not only the status.
- Idle connections stay open 120 s between turns (5 s before). A call that has to open a connection gets
  `Timeouts.connect` (1.0 s by default) on top of its method's budget, once per cold period, so a first read
  after idle no longer times out on the TLS handshake.
- A coordination check the API refuses (400, 401, 403 or 422) is `invalid_request`, never `unchecked`: an
  outbound contact is deferred and an inbound one allowed, and `strict` raises. Refusals of background writes
  (the outbox, turn records) log the problem's detail and request id.
- The tool definitions follow the server's: the timeline tool says it also lists the open items, facts,
  patterns or objects `filters.item_kinds` names.

## [0.9.2] - 2026-10-02

### Changed

- The canonical phone of the suppression list follows the revised rules of the suppression spec (3.1 and 3.4): Mexico's and Argentina's mobile prefixes, an 11-digit North American number, a carrier code, `(0)`, `tel:` and direction marks. The key the SDK computes for an opted-out contact matches the server's again.

## [0.9.1] - 2026-10-02

### Fixed

- `conversation.claims.guard_text()` in a space with no claim contract raised `NameError: name 'Guarded' is not defined`: the result class was imported for type checking only. It now returns the text as it is, as documented.

### Added

- A route or field the API deprecates answers with the `Deprecation`, `Sunset` and `Link` headers; the
  client logs one warning per deprecated route per process on the `niadra` logger, with the two dates and the
  migration note, never the path.

## [0.9.0] - 2026-09-30

### Added

- `Context.age_ms`: how long ago Niadra sent or confirmed the pack a read served. It is 0 for an answer just
  received and grows while the cache serves the pack (`cache`, `stale`, or `last_good` with Niadra down).
- The chaos test (`tests/test_chaos.py`): Niadra's process killed, its network gone silent, answering 503 and
  answering past the deadline, in the middle of a conversation.
- A state read's objects carry `derived`: the type's derived fields over its related objects (a look's
  `all_pieces_available`), computed at the read, each with `v`, `logic`, `over` and `unknown`
  (`DerivedState`). A shared object of a derived type carries `derived_status`, and its push's `inputs` name
  shared objects.

### Fixed

- The local copy of the suppression list is read to its end against the server: a page shorter than the limit
  ends a read, and its cursor is where the next read starts. The server names the cursor on the last page
  too, so the copy read 50 pages of nothing and was never held: `may_contact()` and a check that Niadra did not
  answer fell back on the purpose's direction. The emulator answers as the server does.
- A check about an outbound contact keeps the local copy of the suppression list, read in the background once
  a minute. Before, only `may_contact()` read it, so an agent that only called `check()` had no copy when Niadra
  went down, and a purpose that fails open (`service`, `transactional`) went out to a customer who had opted
  out of it.

## [0.8.0] - 2026-09-30

### Added

- The claim guard takes the offers a context read served as evidence: each object in the constraints block's
  `already_presented` carries the numbers it was last shown with (`values`: price, total, discount,
  installment), each with its role and whether it may be claimed now, and `block_values()` turns them into values the
  guard checks a number against, as it checks a tool's result. An offer shown too long ago to claim makes the
  number `stale`, and a price only the pack's text states is still `unsupported`. The constraints block's
  `Shown` model gains `values` (`ShownValue`).

### Removed

- `@niadra.tool(binding=...)`, `Counterfactual(bindings=...)` and `niadra counterfactual --bindings`: a tool's
  binding comes only from the space's `tool-bindings` document, which the SDK profile serves. A counterfactual
  for a tool the space does not bind stops before calling it.

## [0.7.0] - 2026-09-30

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
