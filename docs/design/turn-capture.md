# Turn capture

Status: built, except two parts. In section 4's table, Google ADK, OpenAI Agents, LangGraph and LangChain
record turns; the voice, webhook and messaging rows do not yet. In section 3, the conversation's quotes are
not kept locally. Where the code settled a detail differently from the first draft of this note, the note
now says what the code does, and why.

A turn runs from its input (a message, an interface action, an event, a timer firing) to the last thing
it emits to the customer or to a document. Its record says what the agent read, called, showed, claimed
and decided, with the build pinned (the Turn Record spec, `turn-record.v0`). One rule decides every choice
below: **capture never delays the agent**. The SDK copies and hashes in the agent's process, at the moment
things happen, and sends later. A failure of the capture marks the turn incomplete and never reaches the
agent's code.

## 1. The turn context variable

- A `ContextVar[TurnFrame | None]` named `niadra_turn`, beside the `niadra_session` variable that
  `current_session()` reads today (`conversation.py`).
- `conversation.turn(build=...)` opens a frame, as a sync and an async context manager, and so does an
  adapter hook. It mints the `turn_id` at the input: a UUIDv7, which matches the spec's
  `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`. It sets the variable, and resets it with its token on exit.
- **asyncio:** a task copies the context when it is created, so `create_task` and `gather` inside a turn
  see the turn.
- **Threads:** a thread does not inherit the context. `niadra.turns.bind(fn)` runs `fn` in a copy of the
  caller's context. Adapters use it where a framework runs tools on a thread pool (LangGraph's sync
  tools, CrewAI).
- **Sub-agents:** a frame opened while another is current is a sub-turn. It gets its own `turn_id`, and
  its `agent.parent_turn_id` is the outer turn's. Two sub-agents running in parallel each run in their
  own copy of the context, so their calls never land in each other's frame.
- **Calls:**
  - `call_id` is the provider's tool call id when there is one, and `k1`, `k2`, ... per frame when there
    is not.
  - A second variable holds the call in progress. A model call made inside a tool gets that tool's
    `call_id` as its `parent_call_id`.
  - `attempt`, `synthetic` and `cache_hit` come from the hook that saw the call.
- **Copy at the moment:**
  - Arguments and results become JSON bytes the instant they are seen (`pydantic_core.to_json`, with
    unknown objects written as their `str()`). Those bytes are the deep copy: a later mutation by the agent
    cannot change the record. It costs 0.05 ms for a 25 KB result and 0.3 ms for 150 KB.
  - The digest (SHA-256 over the canonical JSON, `niadra.turns.digest`) is computed later, on the sender,
    from those bytes. Canonical JSON is written in Python, and costs 0.5 ms for 25 KB and 3.3 ms for
    150 KB: too much for the agent's path, nothing for the sender's.
  - When a framework hands the model one version of a result and the interface another, both are kept
    (`result_model`, `result_ui`).
  - A streamed or generator result is kept piece by piece, and materialized when the turn closes.
- **Closing:** on exit the frame gets `ended_at`, `latency_ms` and its completeness. It goes to the turn
  queue as one record plus its blobs, and nothing else happens on the agent's path.
- **Budget:** p95 at most 2 ms per tool call and 5 ms per turn, held by `tests/test_turn_budget.py` on the
  median and the p95 payload sizes (25 KB and 150 KB). Measured: 0.05 ms and 0.26 ms per call, 0.1 ms for a
  turn with two 25 KB calls.
- **Claims:** what the agent says inside a turn is kept, and the claim check runs on the sender, since in
  count mode it changes nothing the agent sends.

## 2. The bounded turn queue

- **Its own queue.** It is separate from the event queue (`_queue.EventBuffer`): turns have their own
  route, `POST /v1/turns`, which takes up to 50 records and 4 MB compressed, and their own rule for what
  to drop.
- **Bounded twice:** by bytes (64 MB of frames and blobs by default) and by count (2,000 turns). The
  bounds are options, like `QueueOptions`.
- **When it is full**, in this order:
  1. The queue drops the blobs of turns that carry no flag, oldest first. A flag is an error, a guard
     that acted, a handoff, a failed assertion, a synthetic turn or negative feedback.
     - The frame stays. A dropped blob whose digest the sender already computed keeps its `sha256` and
       `size` (it becomes hash only); one without leaves the record with the call fields that named it,
       since computing its digest there would cost the agent's path.
     - The turn says `completeness: partial`.
  2. Only then does it drop the oldest frames whole.

  Both are counted, and logged at most once a minute. A flagged turn keeps its blobs longest, because it
  is the one someone will replay.
- **Never waits.** `put` appends under a lock that no I/O ever holds, and a full queue drops instead of
  blocking.
- **The sender** is the flusher pattern of `_queue.py`: a thread for `Niadra`, a task for `AsyncNiadra`,
  one request in flight. The task builds its records (digests, gzip, the claim check, the uploads of
  `pointer` mode) on a worker thread, so the event loop never computes one.
  - The body is gzip, up to 50 turns and 4 MB.
  - A turn too large on its own is sent with its blobs reduced to hashes, so a 413 `turn_too_large`
    never loops.
  - A 503 with `Retry-After` puts the batch back at the front.
  - A 207 drops the rejected items and counts them.
- **The mode** is `TurnOptions.content_mode` when set, `pointer` once a store is set, otherwise the one
  the space's recording names in the profile, otherwise `stored`. A turn refused with
  `content_mode_refused` goes again with digests only, which every source accepts, and so do the next ones.
- **Content modes:**
  - `stored` sends each blob's content in the record.
  - `pointer`: the sender first writes each blob to the company's bucket, over the S3 protocol and with
    the company's credentials, then sends only the pointer and the digest. The upload runs on the
    sender, never on the agent's path. A record whose blobs cannot all be written leaves as `hash_only`,
    `partial`.
  - `hash_only` sends only digests.
- **At exit:** the queue is flushed with a deadline, as the event queue is.

## 3. The local warm cache, with Niadra down

The SDK keeps, per client:

- the last good pack per conversation (this exists: `_cache.ContextCache`, up to 30 minutes stale);
- the `now` view and the constraints block of each conversation;
- from `GET /v1/sdk/profile`: the claim contract and the summarized type registry;
- the suppression list;
- the contact token's public keys;
- the conversation's quotes (derived objects whose inputs it saw).

How each part stays current:

- **The profile** is read once at start. It is revalidated by ETag when `valid_for_s` runs out.
- **An older server** answers the profile with 404. The SDK then turns the new features off for 10
  minutes and asks again, as `PrefetchSupport` does for the prefetch route.
- **The suppression list** is pulled by cursor (`GET /v1/suppressions?cursor=`) and kept in memory with
  its cursor.
- **Memory is bounded** per kind, least recently used first, like `CacheOptions`.

When Niadra does not answer:

- a read serves the cached value, marked `degraded`;
- claims are checked locally against the cached contract;
- the opt-out applies from the local copy of the suppression list while that copy is at most 60 s old;
- only the purposes the company set to fail closed wait: they get `defer`;
- the outage never turns into "not observed": a cached field keeps its value and logic with its real
  age.

As built:

- `client.profile()` (`niadra._profile`) reads `GET /v1/sdk/profile` on first need, and again once
  `valid_for_s` has passed; the turn sender reads it before it builds a batch, so the agent's path rarely
  waits for it. A failure keeps the last profile; a 404 counts every feature as off for 10 minutes. The
  profile decides whether turns are kept, the content mode when the space names one, and the claim
  contract (`use_claim_contract()` puts the company's own copy first, for CI).
- `context(include=[...])` reads the constraints block and the state view in the same round trip. They are
  cached with the pack, so a read that fails serves the last good ones with `degraded: true`. A block the
  space does not serve (404, 501) is dropped from the read, which goes on whole, and not asked for again
  for 10 minutes.
- The claim contract runs in count mode: what the agent says in a turn is checked on the sender against
  the values its tools returned (a field named like a role is a value of that role), and each claim goes to
  the record with its verdict and the act `none` (it stands) or `count`. `conversation.claims.check()` runs
  the same check on demand and returns the records. This check never changes an output; the calls that
  act on one are `conversation.claims.guard()` and `guard_text()`.
- `may_contact(handle, purpose, channel=)` (`niadra.coordination.suppression`) reads the list by cursor on
  first need, then in the background once a minute. The last copy applies however old it is; with no copy
  and Niadra out of reach, `transactional` and `service` go and every other purpose waits; a space without
  a list suppresses nothing.

The route methods of `niadra.api` raise. Each call built on them picks its failure direction, and holds
its own time budget.

## 4. How adapters open and close turns

- **Where a tool is a function,** the generic decorator `@niadra.tool(...)` records the call inside the
  current turn. Outside a turn it runs the tool untouched and records nothing.
- **The native hooks** see what the decorator cannot: sub-agents and an agent used as a tool.

  | Framework | Opens and closes the turn | Records the calls |
  |---|---|---|
  | Google ADK | `before_agent_callback` / `after_agent_callback`; an `AgentTool` is wrapped as a sub-turn | `before_tool_callback` / `after_tool_callback` |
  | OpenAI Agents | `RunHooks.on_agent_start`, run end; `on_handoff` opens a sub-turn | `on_tool_start` / `on_tool_end` |
  | LangGraph, LangChain | the graph run; each node inherits the context variable | `wrap_tool_call`, `on_tool_start` / `on_tool_end` |
  | LiveKit, Pipecat | one turn per user utterance | the wrapped `function_tool`, a `FrameProcessor` |
  | Webhook voice and messaging | one turn per inbound event | the tool call the webhook carries, recorded as a pair |
  | A loop without a framework | `conversation.turn()` | the decorator, or `turn.tool_call()` by hand |

- **Opening:** an adapter opens the turn at the input, with its `kind` (`message`, `action`, `event`,
  `timer`), and a sub-agent's start opens a sub-turn.
- **Closing:** the turn closes after the last emission to the customer or to the document. For a
  streamed reply that is after the stream ends, not when the model call returns.
- **Off unless asked.** Every hook is off by default, turned on by a parameter (`turns=True`), and runs
  only when the profile lists `turns` for the space. With it off, the adapter behaves exactly as it does
  today.
- **As built** (`integrations/_common.py`, `TurnHooks`): a framework may run a callback in a task of its
  own, where a context variable set in another callback does not reach, so the adapters key their turns
  and calls by the framework's own ids (the invocation and agent, the run, the tool call id) and make the
  turn current around the tools and the recorded answer. A model call is recorded by the adapter that
  sees its tokens; `conversation.agent(usage=...)` only pins the model.
- **A hook never fails the agent.** When the hook's own code fails, it logs, marks the turn `incomplete`
  and lets the agent's call go on.
