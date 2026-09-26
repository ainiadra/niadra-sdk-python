# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased] - 0.5.0

### Added

- `explain=True` on `context()` and `ContextRequest` (sync and async clients), memory v2 only:
  requires `format="json"` (raises `ValueError` otherwise) and adds `why` to each of
  `pack.slots`, a `SlotWhy` naming the retrieval channels that ranked the item (`SlotChannelRank`:
  `channel`, `position`, `weight`, `contribution`), the fused `score`, the `weights_version` used
  and, for a derived line, the `rule` and `basis` behind it. It changes nothing else: the pinned
  text, the slots chosen and the receipt are the same bytes with or without it.
- Models: `SlotChannelRank`, `SlotWhy`, `PackSlot.why`, and `ContextUseEntry` (with `slots`), the
  server's per-delivery measurement, ids public.
- `BatchResponse.masked` and `IngestStatus.masked`: values held back from storage before it, by
  type (`card`, `cvv`, `password`, `secret`), counts only, never the values.
- `niadra_mock`: `explain` support in `MockCell.context()` (the emulator's own retrieval channel,
  scored the way the server scores `lexical`).

## [0.4.0] - 2026-09-25

### Added

- `search(..., limit=)` caps the items after the token budget, and `HistoryFilters(where=...)` takes
  a condition tree for search and timeline: `AND`, `OR` and `NOT` over `id`, `kind`, `channel`,
  `category`, `outcome`, `source_id`, `vendor`, `at`, `valid_until`, `confidence`, `text`,
  `object_type` and `object_namespace`, with `eq`, `ne`, `in`, `nin`, `gt`, `gte`, `lt`, `lte`,
  `contains`, `icontains` and `exists`. It narrows what the policy let through; it never reorders.
- `feedback_batch(items)`: up to 500 corrections in one call, each with its own idempotency key
  (minted when missing); per-item errors come back by index.
- `ingest_status(conversation_id=... | task_id=...)`: whether what was sent became memory yet
  (`open`, `processing`, `ready`, `failed` or `unknown`), states and times only.
- `whoami()`: what the key authenticates as (space, source, vendor, scopes, audience, whether agent
  memory is on), for any key, to check a key before wiring an agent to it.
- `niadra.admin`, for a key with the `admin` scope, on the sync and the async client:
  `find_profiles`, `memory` (everything memory holds about a customer, every status, with
  provenance), `fact_history` (every value a fact's slot held and who replaced whom), `correct` and
  `correct_batch` (the data subject's correction, keyed `<key>:<n>` per item), `forget` and
  `forget_status`, and `export`. They fail open like the rest of the SDK.
- Models: `IngestStatus`, `KeyIdentity`, `ProfileMemory`, `FactOut`, `FactHistory`, `FactRelation`, `ProfileMatch`,
  `CorrectionRequest`, `Erasure`, `ExportPackage`.
- Memory v2 on the read path. A conversation sends the customer's last turn (the text of the last
  `customer()`) as `query` on every `context()`; `turn=` passes another one and `turn=None` sends
  none. In a space with memory v2 the answer keeps the pinned pack and adds `slots`, what that
  turn selected from memory, which `turn_block` places after the live turns and before the delta,
  as the API documents it, so every integration gets it with no change. The pack is cached as the read without `query` and
  the slots never are. A space without memory v2 compiles a read with `query` for it and does not
  pin it: after one such answer the client reads the pinned pack instead, within the same budget,
  and stops sending the turn for ten minutes. `Niadra.context()` and `AsyncNiadra.context()` take
  `turn=` too.
- The pack as data follows `context-pack.v1`: `ContextPack.slots` lists the turn's lines typed as
  `PackSlot` (`section`, `derived`: `count`, `no_record` or `withheld`, `channels`, `text`);
  `ContextResponse.slots` is the rendered block. Both are optional: an answer without them reads as
  before. `spec/context-pack.v1.json` is the schema, and a test keeps the models equal to it.
- `prefetch()` on the clients and on conversations and tasks: `POST /v1/context/prefetch` with a
  partial transcript of the customer's turn, so the server warms what the read that answers the
  turn will need. It runs in the background (a task on the running loop, or a thread for
  `Niadra`), one at a time per conversation (the newest text waits for the one in flight), never
  raises and never holds a turn; a server without the route is not asked again for ten minutes.
  `Timeouts.prefetch` bounds it (1 s).
- Voice adapters: LiveKit sends the turn so far on each `user_input_transcribed` event and passes
  the last user message as the turn of each read; Pipecat has `memory.prefetcher()`, a processor
  for right after the STT service that prefetches on interim and final transcripts, and passes the
  last user message as the turn.
- `niadra-mock`: `enable_memory_v2()` (and `--memory-v2`) answers a read's `query` with `slots`
  instead of a pack compiled for it; `POST /v1/context/prefetch` answers 202 and keeps each request
  in `cell.prefetches`. Models: `PackSlot`, `PrefetchRequest`.

### Changed

- `turn_block` puts the live turns first and the delta last (it was the delta first), the order the
  API documents with the slots between them. Only the end of the prompt moves; the pinned pack and
  the provider's prompt cache are not affected.

### Fixed

- The WhatsApp, Twilio and ElevenLabs webhook checks refuse a signature header with non-ASCII
  characters instead of raising `TypeError` (a 500 where a 401 belongs): they compare bytes.

## [0.3.0] - 2026-09-25

### Added

- Six integrations under `niadra.integrations`, each an optional extra with the same wiring as
  the others (the context after the agent's instructions and the turn block at the end, the turns
  with the provider's usage, the kit's tools bound to the customer, `agent_memory=`, handoffs) and
  never failing the agent:
  - `retell` (Retell AI): `RetellWebhooks` answers the signed inbound call webhook with the
    context as dynamic variables (`outbound()` gives them for `create_phone_call`), runs the
    history tools as custom functions for the call's customer (`tool_configs()`), and records a
    `call_ended` event's transcript, transfer and end, idempotently;
  - `semantic-kernel`: `NiadraKernel` with a `ChatCompletionAgent` thread (`NiadraThread`), a
    prompt rendering and a function invocation filter for prompt functions, and the history tools
    as a `KernelPlugin`;
  - `haystack` (Haystack 3): `NiadraContext` and `NiadraReply` pipeline components,
    `NiadraAgentHooks` for an `Agent`, and the history tools as Haystack `Tool`s;
  - `camel` (CAMEL-AI): `NiadraMemory`, an `AgentMemory` around the agent's chat memory, and
    `NiadraToolkit`;
  - `dspy`: `niadra_adapter()` (the pack placed by the adapter's `format()`), `NiadraModule` to
    run a program with it and record the turn, and the history tools as `dspy.Tool`s;
  - `ag2` (AG2 1.x, AutoGen's community line): `NiadraAG2` middleware and tools.
- A Dify plugin in `integrations-extras/dify/`: a tool provider with Get context, Record reply and
  the kit's history and agent memory tools, the customer set by the app and never by the model.

## [0.2.1] - 2026-09-25

### Fixed

- `wrap()` for OpenAI-shaped clients takes `agent_memory=` like every other adapter: the agent's notes go into the same system message, before the customer's context.

## [0.2.0] - 2026-09-25

### Added

- Twenty-one integrations under `niadra.integrations`, each an optional extra that wires the context
  (after the agent's instructions, the turn block at the end), the turns (with the provider's
  usage), the history tools bound to the customer, the proof a platform gives about who is there
  (`verify()` before the first context) and handoffs into the framework's own extension points,
  and never fails the agent:
  - voice: `livekit` (`NiadraAgent`, `NiadraMemory`), `pipecat` (`NiadraMemoryProcessor`),
    `elevenlabs` (initiation, server tool and signed post-call webhooks), `vapi` (`VapiServer`);
  - channels: `whatsapp` (signed Cloud API webhooks to turns, `sent()`), `twilio` (voice with
    `StirVerstat` as proof, SMS and WhatsApp through Twilio);
  - agent frameworks: `openai_agents`, `langchain`, `langgraph` (middleware for `create_agent`),
    `google_adk`, `strands`, `pydantic_ai`, `llamaindex`, `crewai`, `agno`, `agent_framework`;
  - model SDKs: `anthropic`, `bedrock` and `google_genai` `wrap()`s, `litellm`; Azure OpenAI through
    the OpenAI `wrap()`, now tested with the real clients;
  - Langflow components in `integrations-extras/langflow`.
  Extras: `livekit`, `pipecat`, `elevenlabs`, `vapi`, `whatsapp`, `twilio`, `openai`,
  `openai-agents`, `langchain`, `langgraph`, `anthropic`, `bedrock`, `google-genai`, `litellm`,
  `google-adk`, `strands`, `pydantic-ai`, `llamaindex`, `crewai` (Python 3.11 or later), `agno`,
  `agent-framework`. Every adapter takes `agent_memory=` to put the agent's own notes before the
  customer's context and add the agent memory tools.
- Agent memory: `agent_memory()` (the notes as one block for the prompt, cached and revalidated
  by ETag; empty with `enabled=False` when the space has it off), `search_agent_memory()` and
  `remember()`, also on conversations and tasks (with their view, and their id as evidence).
  `tools(agent_memory=True)` adds `search_agent_memory`, and `write_agent_memory=True` adds
  `remember`, for keys with the `agent_memory:write` scope. A note with personal data is refused,
  and the tool tells the model so in a fixed error.
- `context(format="json")` returns the pack as data in `context.pack` (`context-pack.v0`).
- History filters take `when` (a time phrase in Portuguese, English or Spanish) and
  `show_expired`; search and timeline answers carry `window` and `ignored`. Events take
  `valid_until`; history items carry it and opened items carry `versions`.
- `customer()` takes `stt_confidence`; `customer()` and `agent()` take more `handles=` of the same
  person and a `content=` (media by reference).
- `niadra-mock` answers every `/v1/agent-memory` route with the API's rules (off until
  `enable_agent_memory()`, writes only with the scope, personal data refused, versions,
  proposals), the tools listing, the pack as data, time phrases, expiry, object versions, and the
  channel proof of an inbound customer turn with `verification_hint`.

### Changed

- The tool definitions are the API's own, byte for byte (`tool_definitions.json`, the same file as
  the TypeScript SDK's): the history tools take a `filters` object, and the kit still reads the
  flat filter fields of 0.1. `item_kinds: ["system_event"]` is read as `object`.
- New request fields (`format`, `show_expired`) are sent only when used, so a cell that does not
  know them still takes the request.

## [0.1.5] - 2026-09-24

### Changed

- `open()` sends `POST /v1/history/open` with the item id, the level and the conversation id in the
  body, instead of `GET /v1/history/items/{id}` with the conversation id in the query: a
  conversation id may be a phone number or an e-mail, and a URL reaches access logs.
- `open()` takes `subject=`, the customer the item must belong to; the server opens any other item
  as 404. The tool kit passes its bound customer, so `open_history_item` opens only that
  customer's items.
- `task_id=` on `open()` is no longer sent: the server never read it on this route. The argument
  stays for code written against 0.1.4.
- `niadra-mock` answers `POST /v1/history/open`, with the same customer check.
- The `excerpt` field of an opened item says the server no longer sends it.

## [0.1.4] - 2026-09-24

### Changed

- A conversation turn (a message with a `conversation_id`) leaves the queue at most 0.2 s after it
  was queued, taking whatever else is waiting along, instead of up to a second: it is what the other
  agents read in `live`. `QueueOptions.turn_interval` sets it; other items still wait for `interval`
  (1 s) or `batch_size` (15). A queued turn wakes the background sender when it would otherwise
  sleep past it.
- The timeline tool says the history comes newest first, as the server returns it, instead of
  "in chronological order".

## [0.1.3] - 2026-09-24

### Added

- The agent's turn carries the usage the model provider reported for the call behind it, as
  `usage` (`ModelUsage`: provider, model, prompt tokens with the cached ones included, cached
  tokens, tokens written to the cache). `wrap()` reads it from every OpenAI-compatible response
  and from the last chunk of a stream that asked for it (`stream_options={"include_usage": True}`),
  and never changes the request. Niadra sums it per agent, vendor and model, and the Console shows
  the prompt cache's hit rate and estimated savings.
- `agent(text, usage=...)` on conversations and tasks takes the provider's response (OpenAI
  chat completions or Responses, Anthropic messages) or a `ModelUsage`, for agents that do not
  use `wrap()`. `ModelUsage.from_response()` reads one yourself. A response without usage is
  left out; the turn is recorded either way.

## [0.1.2] - 2026-09-24

### Changed

- The search tool no longer offers the model a `system_event` item kind, and its description names
  business objects instead of system events: a system event is never an item, it changes its object,
  so the filter is `object`. The server still reads `system_event` from 0.1.1 and older as `object`.
- The emulator files a system event found by search under its object, as the cell does.

## [0.1.1] - 2026-09-24

### Fixed

- A time budget now bounds the whole call. httpx times each phase of a request apart, so a slow
  answer could hold `context()` past its 300 ms; a budgeted attempt now ends at the budget, on a
  worker thread in `Niadra` and by cancellation in `AsyncNiadra`.
- The writes a caller waits for (`identify`, `verify`, `feedback`, `subject_token` and the upload
  reservation) stop at `Timeouts.write` (5 s) in total instead of 5 s per attempt, three attempts
  and backoff. An `identify` or `verify` that runs out of time stays queued for the background
  sender.
- `Timeouts.upload` (60 s) bounds a media upload in total instead of each attempt.

## [0.1.0] - 2026-09-23

First public release, against the `/v1` API and the v0 open specs.

### Added

- `Niadra` and `AsyncNiadra`, twin clients over one transport core.
- `context()` with per-conversation caching: fresh for 10 s, stale-while-revalidate for 10
  minutes more with one background refresh per key, ETag revalidation, the last good pack on
  errors up to 30 minutes old, at most 1,000 packs, and purge on 401 and 403 (`CacheOptions`).
  Plain and delta reads of a conversation share one entry, and each delta is handed out once.
- History navigation: `search()`, `timeline()` and `open()`.
- `track()` with a bounded local queue, batching by count and time, three attempts with backoff,
  and per-source heartbeats. `action()` and `handoff()` build on it.
- `identify()` and `verify()`, sent at once so the next `context()` reflects them.
- `conversation()` and `task()` context managers that read the pack the server pins, ask for
  deltas after the first read and keep them until the pack changes, capture turns, and emit
  `conversation.ended` or `task.ended`. `verify()` on a session raises its level for later reads.
  A task centers its pack on its object and defaults to the `brief` view. `tools()` on a session
  gives the history kit bound to it, following its verification level.
- `mark_injected()` and `context_stamp`: the agent's turns and actions carry the etag of the pack
  its prompt held and when it went in, as the event's `context_stamp`.
- `object_state()` and `object_timeline()` for business objects.
- `feedback()` to retract or correct a fact, resolve an open item or record a conversation's outcome.
- `upload_media()`: reserves an upload, sends the bytes to the signed URL with exactly the headers it names (HTTPS
  only, never the key) and returns `media_ref` and `media_sha256`; optionally bound to a `subject`.
- `tools()`: the history kit as function-calling tools bound to one customer, in OpenAI and
  Anthropic shapes.
- `subject_token()` and `mcp_url` for MCP connections bound to one customer.
- `wrap()` for the OpenAI Python client, sync and async: `chat.completions.create` and `parse`
  (also under `beta`), streams, and `with_raw_response`. It stamps the injection, records the
  answer (the first choice), and never lets a capture failure reach the caller.
- `object_refs` in events accept the `type:namespace:id` shorthand.
- Handle helpers: `phone`, `email`, `whatsapp`, `whatsapp_bsuid`, `system_id`, `app_user`, `anonymous`.
- Fail-open behavior: a no-op client without a key, no exceptions from public methods unless
  `strict=True`, and per-method time budgets.
- `niadra-mock`, an in-memory emulator of the API with ASGI and WSGI entry points and a
  `niadra-mock` command, including object reads, feedback and media uploads.
