# Niadra benchmark

An open, reproducible benchmark of Niadra against the memory layers teams compare it with: Mem0, and a
field of other memory systems added one adapter at a time (ai-memory, Graphiti, Hindsight, Memobase,
Supermemory local, MemOS). It measures what an engineer integrating a memory for customer-facing and
internal agents needs to know, with the same models on every side where a system lets them be chosen,
and publishes every result file. Where Niadra loses, the number is published the same way.

Nothing here is a claim until it is measured inside the cloud region. Numbers from a laptop, from the
emulator or from a smoke run are never published; the site only imports result files whose
environment is `region`.

## What it measures

"Added systems" are the systems measured through an adapter in `src/niadra_bench/systems/` (the field,
below); each one is used through the calls its own documentation shows.

| # | Metric | Niadra | Mem0 | Added systems |
|---|---|---|---|---|
| 1 | Latency of the context before the model call (p50, p95, p99), open loop at 10 and 25 reads per second for 30 s over 20 conversations | `POST /v1/context` with the conversation id, through the public TLS address and through the private address in the VPC | `POST /search` on its own REST server, `top_k` 10, `threshold` 0.1 | its read of a turn (the field table) |
| 2 | Tokens the memory adds to the prompt per turn (`o200k_base`) | `system_block` and `turn_block`, `voice` and `chat` views | the search results in the format of Mem0's examples ("Based on previous conversations, I recall:") | the lines its read returns, in the format its documentation shows |
| 3 | Cost of the memory layer per thousand conversations of 10 exchanges | public price, both ends ($5 and $15), models included | open source: extraction model spend measured from the provider's usage numbers (servers not priced); Platform: public plans over their quotas | the model spend its gateway counted from seeding until its memory settled, per exchange (servers not priced); zero where it calls no model |
| 4 | Cross-channel continuity accuracy on the synthetic dataset | the agent answers from Niadra's context | the same agent answers from Mem0's results | the same agent answers from its read |
| 5 | Privacy: sensitive value handed to an unverified (V0) conversation | counted in the memory block | counted in the memory block; "no mechanism" | counted in the memory block; "no mechanism" |
| 6 | Freshness: a WhatsApp message until the voice agent reads it | `track()` until `context(view="voice")` shows it | `add()` until `search()` shows it | its write until its read shows it (a queue's wait included) |
| 7 | Memory slow (2 s) or down (503) behind the same fault proxy | the SDK as it ships | an HTTP client at its defaults with `raise_for_status()` | the same as Mem0 |
| 8 | History navigation (p50, p95, p99), open loop for 30 s over 20 seeded customers | `POST /v1/history/search` and `POST /v1/history/open` at 10 per second (the production cap), both paths | `POST /search` and `GET /memories/{memory_id}` at 10 and 25; the question's encoding alone as its own line (`encode`) | its read as `search`, and its call that opens one item as `open`, at 10 and 25 |
| 9 | Ingestion acknowledgement (p50, p95, p99), open loop for 30 s over 20 seeded customers | `POST /v1/batch` with one exchange until its `200`, at 10 per second (the production cap), both paths | `POST /memories` with the same exchange until its `200`, with `infer` (its default) and with `infer=False`, at 10 and 25 | its write of one exchange until its 2xx, at 10 and 25 |

Metrics 8 and 9 are timed exactly as metric 1: the same open loop (`latency.open_loop`: constant rate
whatever the answers do, one warm-up call per customer, the same HTTP client and limits), each line
alone, three repetitions, the median and the range across them. Every call that did not get the answer
the caller waits for is an error, counted by kind (`error_kinds`: an HTTP status, a timeout); a search
that answered with nothing, or Niadra's `text_only` search when its encoder did not answer in time,
stays in the percentiles and is counted apart (`empty`, `degraded`). They run after the other metrics
of each repetition, so metrics 1 to 7 are measured under the same conditions as in earlier runs.

Two references run through the same agent for every accuracy number: no memory at all, and the whole
raw history pasted into the prompt.

Beside the grades, each accuracy line reports `context_has_answer`: whether the memory block itself held
the answer, before any agent read it. A value of three or more digits (a protocol, an amount, a ZIP
code) counts when the block holds it as a whole token. A shorter number or a word does not count that
way: the first run counted the "3" of a recurrence answer in any date of the block, so a block that
listed dates "had" every count (18 of Niadra's 32 recurrence blocks in the first run's first
repetition, with 6 right answers). Since dataset v2, a count holds only as a
number of its own (never inside a date, a time or an amount, never next to a month) on a line with a word
that counts ("3 reclamações", "Reclamações de técnico: 3", "twice"), or when the block lists every one
of the occurrences by its reference; a deadline day holds only right after a word that sets it ("até o
dia 15", "by the 15th"). The first run's rule stays in the results as `context_has_answer_loose`, and
`config.context_has_answer_rule` names the rule a run used (`metrics/accuracy.py`).

Each privacy line says whether the system has a verification mechanism at all (`verification`:
`per conversation` for Niadra, `none` for every other system). A system with none hands the block to
any caller, so the page shows "no mechanism" for it rather than a score; the count of blocks that held
the sensitive value is still in the file. Results before 26/09/2026 do not carry the field.

We do not run LoCoMo, LongMemEval or BEAM: they are long personal conversation sets and do not measure
what a Niadra buyer buys.

## How the comparison is kept fair

- **Same place, and Niadra's production kept safe.** Since 26/09/2026 the harness and every system but
  Niadra run on a temporary EC2 host of their own in the cell's VPC in us-east-2 (`deploy/temp-host`),
  one system at a time, each with its own databases on that host: nothing of theirs touches the cell's
  machine or its RDS instance. Niadra is measured where it runs, the cell (m7i-flex.large, RDS
  db.t4g.micro), through its public TLS address (`edge`) and through the cell machine's private address
  in the VPC with the same TLS name (`vpc`); every other system is on the harness's own host (`host`), so
  Niadra's lines carry a network hop the others do not, and the page says so. Until 25/09/2026 everything
  ran as pods of the cell (`cluster`); on that evening the second run saturated PgBouncer and the RDS
  instance went into recovery (niadra-docs `estudo/09-RODADAS.md`), which is why the benchmark left the
  cell. What a run may send to the cell is capped (config `[production]`, "Load on production" below).
- **Same models where a system lets them be chosen.** Every system that extracts with a model uses
  `google/gemini-2.5-flash-lite` through OpenRouter; every system that takes an embedder uses the one
  the cell runs (`niadra-models`, `paraphrase-multilingual-MiniLM-L12-v2`, 384 dimensions), the same
  image and model files, on the harness's host, through a small OpenAI-compatible proxy
  (`bench serve embed-proxy`). Each system's model calls go through a gateway of its own
  (`bench serve llm-meter`), which counts the spend from the provider's usage numbers, sends embeddings
  to that embedder when the system has a single base URL, and, when a system's server fixes some model
  names that its settings do not reach (Graphiti's small model), asks for the same extraction model. A
  system whose embedder or reranker cannot be set keeps its own, named in the field table. One agent
  (`openai/gpt-4.1-mini`, temperature 0, fixed prompt in `src/niadra_bench/agent.py`) answers every
  probe, and one judge with a published rubric grades it; an exact check that looks for the expected
  value in the answer is reported beside it.
- **Mem0 as documented.** One `add()` per exchange with both messages (the README pattern; the v3
  extraction reads both roles in one call), system records as raw memories (`infer=False`), one
  `search()` per turn. No parameter tuned for this dataset. The rerank column (`mem0_oss_rerank`)
  builds `mem0.Memory` in-process on the same store with an LLM reranker on the same model, because
  the REST server's `/search` has no rerank parameter. Its server (`server/` of github.com/mem0ai/mem0 at
  tag v2.2.0, with `mem0ai[nlp]==2.2.0` and the spaCy model its README asks for) keeps pgvector in its own
  PostgreSQL on the harness's host.
- **Every added system as documented.** Each adapter drives its system the way that system's own docs,
  quick start or evaluation harness does, with its defaults, and says so in its module's docstring and in
  the field table: no parameter tuned for this dataset, nothing changed in its code (with one exception,
  Graphiti's server, whose two fixes are needed for it to store anything and are listed below).
- **Niadra as documented.** The SDK is the current release on PyPI (`niadra==0.4.0`; the first run
  installed 0.1.5 and run 2026-09-25-6efee4 0.3.0, whose read path is the same for a read with its own
  `query`). 0.4.0 is the first that puts memory v2's `slots` in `turn_block` (0.3.0 drops the field), so
  a run with `memory_v2` on measures what an agent gets only from 0.4.0 on; its turn block is the live
  turns, the slots, then the delta, where 0.3.0 put the delta first. Each exchange is a batch of `message` events with its
  `occurred_at` and a `conversation.ended`, system records are `system_event`s, the billing agent's
  records are `action`s, and every probe verifies the call or chat before `context()`, as the voice
  and WhatsApp guides show; the question goes in `query`, which only ranks the pack's items by its words. The billing agent has a source of its own that
  declares the operations it records (`credit`, `refund`, `refund_fee`, `reimburse`, `redeliver`), the
  way the internal agents guide tells a company to set it up: the harness creates it once through the
  control API with the sandbox's admin account and issues a key per run, revoked at the end
  (`src/niadra_bench/sources.py`). The first run wrote those actions with the sandbox's starter billing
  source, which trusts `credit` only, so Niadra refused 26 of the 32 promise cases' actions while Mem0
  stored them. Nothing else about Niadra is configured: starter policy and extraction schema, the space's
  language and time zone, no predicate added for the dataset.
- **The run's billing key never outlives it.** A run that is stopped (the container or the host stops,
  SIGTERM, SIGHUP or Ctrl-C) is cancelled, not killed: every target is closed, so the billing key is
  revoked and a space flag the run set is put back. And every run first revokes any key of the
  benchmark's billing sources that an earlier run left active (`ControlPlane.revoke_stale_keys`), since
  two runs never share a space at the same time.
- **Mem0 as its open source release.** What is compared is Mem0's open source release (the `mem0ai`
  package and the REST server of github.com/mem0ai/mem0 at tag v2.2.0), not the hosted Mem0 Platform.
  Two features Mem0 describes for the Platform are not in it: the time of an event (`timestamp` and
  `reference_date` on `add()`, what its temporal diagrams show) and memory decay (`decay` on a project).
  The open source SDK refuses them with "The timestamp parameter is not supported by the OSS Memory
  SDK", "The reference_date parameter is not supported by the OSS Memory SDK" and "The decay parameter
  is not supported by the OSS Memory SDK" (`mem0/memory/notices.py`, lines 130 to 134 at tag v2.2.0;
  `mem0/memory/main.py`, lines 467 to 483, raises the decay one). What the open source release does
  have, and the benchmark uses as it ships: the date of observation in its extraction prompt, as the
  anchor for words like "yesterday", and an `expiration_date` per memory. So no number here says
  anything about the Platform's temporal or decay features, and changed fact and order and time in
  particular are measured against a Mem0 without them. The Platform's accuracy is measured only when a
  run includes `mem0_platform` (below), which the default run does not.
- **Identity.** No system but Niadra resolves identity across channels. Mem0 runs with the same user id
  on every channel (`known_id`, its best case) and with each channel's own id (`per_channel_id`). Every
  added system gets one store per customer (a user, group, bank, project or tag) with every channel in
  it: the `known_id` scenario, their best case. Niadra receives the same handles in every scenario.
- **Time.** Where a system's API takes the time of an event, it gets it (Graphiti's message `timestamp`,
  Hindsight's `timestamp`, Memobase's `created_at`, MemOS's `chat_time`, and ai-memory the way its own
  harness replays a dated history, a `[session date: ...]` prefix); where it does not (Mem0's open source
  release, Supermemory local), the order of the writes is the only time it has.
- **Fresh customers.** Every repetition seeds new phone numbers, e-mails and ids, so extraction runs
  again and no memory has seen them. Each metric runs three times; the site shows the median and the
  range.
- **Frozen inputs.** `config/benchmark.toml`, `config/mem0.config.json` and `dataset/cases.jsonl` are
  committed; every result file carries their hashes, the harness commit, the package versions, each
  added system's pinned version and the deployed Niadra server version. Dataset v2 adds
  `config/dataset.v2.toml` and `dataset/v2/cases.jsonl`; a v2 run's configuration hash also covers the v2
  settings, and a v1 run's hash is computed exactly as before v2 existed.

## The field

Each system runs from its own container entry (`deploy/systems/<dir>/compose.yaml`, versions pinned),
alone on the host, and through its adapter (`src/niadra_bench/systems/<module>.py`). The columns are what
the adapter does, the way the system's own documentation does it, and what the reader of a number must
know.

| Key | System and version | Containers | How it is used | Caveats |
|---|---|---|---|---|
| `niadra` | Niadra, the cell's deployed server | the cell | `POST /v1/batch` per session with each event's time and channel; `POST /v1/context` per turn with the question as `query`; voice and chat views | The only system with identity across channels and a verification level per conversation; read over a network hop (edge and VPC) |
| `mem0_oss`, `mem0_oss_rerank` | Mem0 open source, v2.2.0 | its REST server, PostgreSQL with pgvector, its gateway | above | No event time; one extraction call per exchange; the rerank column is in-process (no latency line) |
| `ai_memory` | ai-memory 2.4.1 (MIT, akitaonrails) | one binary (`akitaonrails/ai-memory:2.4.1`) | One project per customer; each session replayed through `POST /hook/batch` at its hooks' cadence (`session-start`, a `user-prompt-submit` per customer turn, a `stop` with the assistant excerpt per agent turn, `session-end`), the session's date before each text and a system record as its own session, exactly as its LongMemEval harness replays a history (`evals/src/retrieval/ingest.rs`); `memory_query` over MCP with the question and `limit` 10, and the agent receives each hit's title and snippet, the context its harness counts; `memory_read_page` as `open` | Made for coding agents, not customers. Its defaults: no model provider, so pages are written by rule and the text of an agent's answer is kept only as a raw observation, reached through the raw fallback when no page matches; its optional local embeddings, as its own benchmark runs it (`all-MiniLM-L6-v2`, English, in process; its `openai-compat` embedder could take the benchmark's, which is left unused so it runs as documented); assistant capture on, as its harness sets it. A hit's title is the session's first prompt cut at about 80 characters, and the date prefix takes about 40 of them |
| `ai_memory_llm` | the same, with its optional consolidation by a model at `session-end` (`AI_MEMORY_CONSOLIDATE_ON_SESSION_END`) | the binary and its gateway | as above, plus the settle waits for its consolidation queue to go quiet | The consolidation prompt and page kinds are its own, written for code (decisions, gotchas, procedures) |
| `graphiti` | Graphiti server 0.30.2 (`zepai/graphiti`) with Neo4j 5.26.2 | server, Neo4j, gateway | `POST /messages` per session with the customer's `group_id`, each message's `role_type`, `role`, `timestamp` and the channel; the settle reads `GET /episodes/{group_id}` until every message is an episode; `POST /search` with `max_facts` 10; `GET /entity-edge/{uuid}` as `open` | Its server needed two fixes to store anything (`deploy/systems/graphiti/patch.py`): the client of a request was closed when the request ended, before the background queue used it, and the queue's worker stopped for good at the first failed episode. Its queue adds one message at a time, with several model calls each: a full dataset v2 repetition is about 5,200 episodes, many hours; run it with `--limit` and say so. One base URL for its model and its embedder; the gateway pins its small model to the extraction model |
| `hindsight` | Hindsight 0.10.1 (`ghcr.io/vectorize-io/hindsight`, embedded PostgreSQL) | one container, gateway | One bank per customer; one `retain` item per session, the whole conversation as `Name (timestamp): text` lines with its `timestamp`, a `context` label and a `document_id`, as its docs ask for a conversation (not one per turn); `recall` with its defaults; `GET .../memories/{id}` as `open`; a live exchange is an `async` retain | Its reranker is its default local cross-encoder |
| `hindsight_reflect` | the same, read with `reflect` | its own server and gateway (`deploy/systems/hindsight-reflect`) | the same writes; each read is `POST .../reflect` with the question and its defaults, and the agent receives its answer text | A model call on every read: its latency, its cost and its tokens include the reasoning |
| `memobase` | Memobase v0.0.42 (built from its repository), PostgreSQL with pgvector, Redis 7.4 | API, database, Redis, gateway | One user per customer (a UUID from the customer's id); one `ChatBlob` per session with each message's `created_at`, then `flush`, as its docs ask at the end of a session; `context()` with `max_token_size` 600 on voice and 1,500 in chat (Niadra's view budgets) and the question as the current chat | No commit since 11/01/2026; profile topics are its defaults (written for companions and assistants, not customer service); a live exchange waits in its buffer until it flushes by size or age, which metric 6 counts |
| `supermemory` | Supermemory local, `supermemory-server` server-v0.0.8 (MIT binary) | the binary, a forwarder in its network namespace, gateway | One `containerTag` per customer; one document per session (`customId`, the conversation as `user:` and `assistant:` lines, `dreaming: "instant"`, which its docs name for benchmarking); `POST /v4/profile` with the question, and the context its quickstart builds from it (static profile, dynamic profile, related memories) | No event time. The local binary is licensed for 10,000 documents (about 2,800 per repetition of dataset v2): run one repetition per fresh container. It signs requests from its own host with its key, so the harness reaches it through a forwarder (`supermemory-local`) that adds a loopback hop. No search mode "documents" (the dataset has no documents) |
| `memos` | MemOS v2.0.34 (built from its repository), Neo4j 5.26.6, Qdrant 1.15.3 | API, Neo4j, Qdrant, gateway | One user and cube per customer; `POST /product/add` per exchange with `chat_time` and the session, `async`; the settle calls `POST /product/scheduler/wait` for every customer and then waits for the memory count to stay the same (the wait needs the optional Redis queue, off in its example configuration); `POST /product/search` with its defaults (`fast`, `top_k` 10) | Its activation memory (a KV cache of a local model) is outside the server API and not measured; a CRM or ERP record is a `system` message |

What no system here has, so their rows read "no mechanism" rather than a score: a verification level per
conversation and a policy by purpose (privacy); identity across channels (every added system gets the
customer's id already resolved, its best case).

## Load on production

Only Niadra runs on production; the caps are in config `[production]` and apply to every run in the
region that reaches the cell (`BENCH_ENVIRONMENT=region`, the temporary host's setting), `bench ab` in the
region included, whatever its agent; niadra-mock, the local pipeline and a local cell keep the run's own
settings:

| What | Cap | Why |
|---|---|---|
| Seeding | 2 batches per second across all seeding tasks, 4 at a time; a 429 or 503 pauses every seeding task for 30 s (or the Retry-After, if longer) | On 25/09 the afternoon run seeded about 12 batches per second with about 18% of the conversations extracted (174 of 986): about 2 extractions per second reached the workers and the cell held. Since niadra-back bb4ecaa nearly every conversation is extracted, so the same pace became about 10 per second in the evening run and saturated PgBouncer (4 server connections per database and role, 6 per database) and the db.t4g.micro. At 2 batches per second, dataset v2 (2,780 batches, 2,328 of them conversations) sends about 1.7 extractions per second, the rate the cell absorbed on 25/09, and takes about 23 minutes per repetition |
| Reads outside the timed loops | 4 at a time; the settle pass reads every case at most once per 10 s | The settle, the accuracy pass and the verifications read the context of hundreds of customers; 4 at a time keeps them to tens of reads per second |
| Metric 1, `context()` | 10 and 25 per second, both paths | `context()` serves the cached pack |
| Metric 8, history navigation | 10 per second only | Each call opens a transaction on the space's database and writes a receipt |
| Metric 9, ingestion | 10 per second only | Each call writes two events in one transaction and every 10 calls open a conversation to extract: one 30 s line is 300 calls and 30 conversations per path |

Before a run, the sandbox space's daily extraction ceiling must cover it: at about $0.002 per
extraction, three repetitions of dataset v2 (about 7,000 conversations) need about $14; the operator sets
`llm_daily_micros:extraction` to at least 16,000,000 with `PUT /v1/quotas`. Past the ceiling, extraction
waits for the next day and the settle step sees nothing change, so the accuracy pass would read half-built
memory.

## History navigation and ingestion, call by call

The docs promise history navigation "in under 200 ms" and the ingestion acknowledgement "in under
80 ms", in the region. What each side is sent, and why it is the comparable call:

| Metric | Niadra | Mem0 (open source REST server, v2.2.0) | Why it is comparable |
|---|---|---|---|
| 8, `search` | `POST /v1/history/search`, the request behind `search()` and the `search_customer_history` tool: the case's probe question, the customer's phone, `max_tokens` 800 (the SDK's default), the conversation id, `verification` V1 | `POST /search` with the same probe question, `filters.user_id` of the same seeded customer (`known_id`), `top_k` 10, `threshold` 0.1 | Both are the one call an agent makes to look something up in a customer's past. Mem0 has a single read, so its line is the same call as its metric 1 line |
| 8, `open` | `POST /v1/history/open`, the request behind `open()` and the `open_history_item` tool, on an episode or object of the same customer | `GET /memories/{memory_id}` on a memory a search of the same customer returned | Both read one item a search pointed to. Mem0 has no episode or conversation to open: a memory is one sentence, while Niadra's `open` returns the episode (what was asked, promised, the outcome, what memory came from it). The work behind the two answers is not the same |
| 9, ack | `POST /v1/batch` with two message items (the customer's message and the agent's answer), the request the SDK's queue sends after `track()`, built with the SDK's own models, until the `200` | `POST /memories` with the same two messages, the call its README makes per exchange, until the `200`; `add_infer` with `infer` left at its default (true) and `add_raw` with `infer=False` | Both are what the application waits for before a write counts as taken. Niadra answers after the events are durable and extracts later, in the background. Mem0's open source server has no asynchronous add (the hosted Platform's `async_mode` is not in it): with `infer` its answer comes after the extraction model ran, and with `infer=False` after the text was embedded and stored, with no extraction ever. Neither Mem0 mode does what Niadra's acknowledgement does, so both lines are published |

How the Niadra side is prepared, before any clock starts:

- **Verification.** Each navigation conversation is proven at V1 (`network_attestation`, as a voice
  agent that attested the caller's number), as every accuracy probe and the freshness reader do, and
  each call asks for V1: an unproven read at V0 would have the policy withhold items, which is not the
  call a real agent makes mid-call.
- **An item to open.** For each customer, a search (and, if it returns no episode or object, the
  timeline) finds an item, which is opened once to check it opens at that level. A customer with none
  is left out of the `open` line; a line with no customer at all is written with `skipped` and no
  number.
- **The HTTP client, not the SDK's budget.** The calls go through a plain HTTP client with a 10 s
  timeout, so the number is the server's answer. The SDK gives navigation 0.6 s (0.3 s on voice) and
  returns an empty result past it; the p99 shows how often that would happen.
- **Fresh writes.** Every ingestion call is a new exchange with a new number and new idempotency
  keys, 10 exchanges per conversation, over the same seeded customers as metric 1, so the server never
  answers from its duplicate check.
- **Paths.** Through the public TLS address the calls go where the SDK sends them (`edge`); from the
  benchmark's host in the cell's VPC they also go to the cell machine's private address with the same
  TLS name (`vpc`, `NIADRA_VPC_ADDRESS`). A harness running as a pod of the cell (no longer done) would
  also call each service's own address (`cluster`: `NIADRA_CLUSTER_URL` for navigation,
  `NIADRA_CLUSTER_INGEST_URL` for the acknowledgement).
- **Rates.** Niadra's lines run at 10 per second only (config `[production]`); the other systems' at 10
  and 25.

Both searches start by encoding the question with the same model (Niadra's read service calls the
cell's `niadra-models`; Mem0, and every added system that takes the benchmark's embedder, call the same
image and model files on the harness's host). So that a search's time can be read without that step,
metric 8 has a third line, `encode`: `POST /v1/embed` on `NIADRA_MODELS_URL` with the same probe
questions, at the same rates (system `embedder`, path `host`: the copy on the harness's host, not the
cell's pod). Niadra's search line also keeps the steps its server names in `Server-Timing` (`server_timing`,
p50 and p95 per step): today only `app`, the whole request; a step the server adds later, such as the
encoding, shows there with no change here.

Mem0's `add_infer` loop costs model spend (the extraction runs on every call) and leaves its server
busy with the requests the client gave up on, so it runs last, after `add_raw`, with a pause of
`cooldown_s` after each rate.

## The dataset

`dataset/cases.jsonl`: 240 synthetic cases, 120 in Portuguese and 120 in English, across telecom,
insurance, banking, retail and logistics, from `bench prepare` (deterministic from the seed). Each case
has the customer's handles per channel, a CRM profile that links them, 3 to 8 earlier sessions on
WhatsApp, voice, e-mail, app, CRM and ERP (conversations, system events and internal agent actions),
a probe question with the expected values, and the verification level of the probe conversation.

Categories, per language: continuity 20, identity 20, recurrence 16, order and time 16, promise and
confirmed action 16, privacy 16, changed fact 16.

The validity rule has two layers. At generation time (and in CI) every expected value must sit in the
key sessions only and never in the question. In each run, the agent must answer right with the whole
history and wrong with none; cases that fail are listed in the results and left out of that run's
accuracy.

### Dataset v2

`dataset/v2/cases.jsonl` (`bench run --dataset v2`; the default stays v1, the dataset of run
2026-09-25-6efee4, so every published run can be run again as it was). 356 cases, 178 per language: the
240 cases of v1, byte for byte and with the same ids (same seed, same plan), then four categories v1
never asked about, from `config/dataset.v2.toml`. Both systems answer every case, and the v1 validity
rule decides in each run which of them count.

| Category | Per language (pt, en) | What the customer asks | What makes it hard |
|---|---|---|---|
| `paraphrase` | 16, 16 | a fact they stated once, in other words ("Que senha o porteiro precisa para deixar o instalador subir?" about "a portaria libera com o código 17674") | the question shares no content word with the statement; the structural rule checks it |
| `long_history` | 10, 10 | a protocol given for one matter, by that matter (even cases), or the current value of something they changed (odd cases) | 30 to 60 sessions per customer, the key session anywhere in the last six months, among exchanges that hand out other protocols |
| `unanswerable` | 16, 16 | the number of the record an e-mail was about, and the protocol "you gave me in your reply" | the reply gave none. The right answer names the record and says there is no protocol, with no number; in half the cases another matter's protocol, on another channel, is in the history |
| `recurrence_topic` | 16, 16 | how many times they complained about one matter | they also complained about another matter a different number of times, the last time in their latest conversation |

How these were kept from favoring either system:

- They are questions customers ask, written from the customer's side (`dataset/vocab_v2.py`), in both
  languages and in the five domains. No wording, field name or category name was taken from either
  system, and nothing was checked against either system's output while writing them.
- Each has the answer only in the history: the structural rule runs on every case in CI, and each run
  keeps a case only when the agent gets it right with the whole history and wrong with no memory.
  An `unanswerable` case stays valid under that rule because its answer also names the record, which only
  the history holds: with no memory, "I do not have that information" misses it.
- An `unanswerable` case is graded with its own rubric (`NO_RECORD_JUDGE_PROMPT` in `agent.py`): correct
  when the answer gives the record and says there is no such protocol, with no number for it. Every other
  case is graded with the first run's rubric, unchanged. The exact check still asks for the record's
  number and for no decoy.
- The long customers' key sessions sit at random ages up to six months, so neither ranking by recency
  nor ranking by similarity is favored by where the answer is.

Running v2 costs more than v1: about 2,330 conversations per repetition instead of 986 (the long
customers are most of the difference), so seeding Mem0 and Niadra's extraction take about twice as
long and cost about twice as much: on 25/09/2026 a v1 repetition cost about $6.50 on OpenRouter (Niadra's
extraction about $0.35 before niadra-back bb4ecaa, Mem0's seeding about $2.70, the accuracy pass about
$1.50, Mem0's `add_infer` loop about $2), so a v2 repetition with Niadra and Mem0 is about $11, three
about $33 (budget $45), before the added systems' own extraction, which their gateways count.

## Layout

```
config/                  frozen configuration (benchmark.toml, with the production caps) and Mem0's
dataset/                 the committed dataset v1 and its manifest; dataset/v2/, the same for v2
src/niadra_bench/        the harness (`bench` command)
src/niadra_bench/systems/  one adapter per added system (base.py: what an adapter provides)
tests/                   unit tests, dry runs against niadra-mock and fakes, the deploy scripts with a stand-in AWS CLI
deploy/Dockerfile        the harness image
deploy/compose/base.yaml the embedding proxy and the harness, shared by every run
deploy/systems/<dir>/    one container entry per system (compose.yaml, pinned; a Dockerfile when built from source)
deploy/stack.sh          puts base, environment and system files together and runs docker compose
deploy/local/            the local environment (fakes, niadra-mock) and run.sh, the local pipeline
deploy/temp-host/        the temporary host: up.sh, down.sh, bench.sh (this computer), host.sh (the host)
deploy/cell/cleanup.sh   removes what the benchmark left on the cell when it ran there
results/                 published runs: results/<date>-<id>/{summary.json,rep-N.json,cases-repN.jsonl}
results/ab/              A/B runs in the region: results/ab/<date>-<id>/{ab.json,ab.md,baseline/,candidate/}
results/local/           A/B runs on a local cell or the emulator (not committed)
config/ab.toml           `bench ab`'s ceiling, apart from benchmark.toml so the published hash stays
deploy/local/cell_server.py  the local cell `bench ab --local-cell` runs in a niadra-back checkout
```

## Running it on the temporary host

Requires the AWS session of the cell's account (`aws login --profile niadra`; the scripts refuse any other
account) and, on this computer, the AWS CLI, Python 3 and bash. Nothing needs to be installed on the
cell. From `benchmarks/`:

```bash
# 1. See what would be created and what it costs; nothing is created.
deploy/temp-host/up.sh

# 2. Create it and wait until it is ready (about 10 minutes: Docker, this repository at BENCH_REF,
#    the secrets read by name, the embedder image, the harness image).
deploy/temp-host/up.sh --confirm

# 3. The campaign: each comma-separated list is one run, alone on the host, one after the other.
deploy/temp-host/bench.sh campaign niadra mem0_oss,mem0_oss_rerank ai_memory ai_memory_llm \
  hindsight hindsight_reflect memobase supermemory memos -- --dataset v2
deploy/temp-host/bench.sh start graphiti --dataset v2 --limit 60 --repetitions 1   # Graphiti alone, smaller
deploy/temp-host/bench.sh status          # repeat: containers, the campaign's progress, the run's log

# 4. The results into results/, then one folder for the site (the first folder's references count).
deploy/temp-host/bench.sh collect
uv run bench combine results/<niadra run> results/<mem0 run> results/<ai-memory run> ...

# 5. Delete everything it created, and check that nothing is left (it refuses while a results folder
#    is still only on S3).
deploy/temp-host/down.sh
```

What `up.sh --confirm` creates, each tagged `niadra:bench-temp-host=<id>`, and `down.sh` deletes and
checks: the instance (m7i-flex.large, 2 vCPU and 8 GiB, free-tier eligible, Ubuntu 24.04, in the cell
machine's public subnet with a public address, IMDSv2 only; the VPC's private subnets have no route out,
by the cell's design, and the host must reach GitHub, the image registries, OpenRouter and Niadra's public
address), its 40 GiB encrypted gp3 volume (deleted with
it), a security group with no inbound rule (the host is reached through Systems Manager only), an IAM role
and instance profile (Systems Manager; `secretsmanager:GetSecretValue` on `niadra/platform/openrouter` and
`niadra/tenant/bootstrap` only; pull of `niadra/models` only; read and write of
`s3://<cell bucket>/benchmarks/temp-host/<id>/` only), and that S3 prefix. The host terminates itself after
`BENCH_MAX_HOURS` (default 24) even if `down.sh` never runs; the role, the group and the prefix cost
nothing and stay until `down.sh`.

Cost, on-demand in us-east-2 (up.sh reads the price list and prints it again): m7i-flex.large $0.0958 per
hour, 40 GiB of gp3 $0.0044 per hour, a public IPv4 address $0.005 per hour: about $0.105 per hour, $0.84 for
an 8-hour campaign, $2.52 if it runs the full 24 hours. On the free plan it is paid from the account's
credits (about $130). c7i-flex.large (4 GiB) is cheaper ($0.085 per hour) but too small for a system
with Neo4j beside the harness and the embedder; set `BENCH_INSTANCE_TYPE` to change it. The models are
paid on OpenRouter, outside AWS: Niadra's extraction runs on the cell; each other system's extraction is
counted by its gateway and reported as its cost line.

Memory on the host, one system at a time: the embedder 1.5 GiB and the harness 1.5 GiB at most, then the
system (MemOS: Neo4j 2 GiB, Qdrant 768 MiB, its API 2 GiB; Graphiti: Neo4j 2 GiB, its server 1 GiB;
Hindsight 3 GiB; Supermemory 3 GiB; Mem0 1.8 GiB with its database), under the 7.6 GiB the instance
gives. Neo4j's heap and page cache are capped in the two compose files that use it, the only setting of
theirs changed.

Secrets, by name only: the host reads `niadra/platform/openrouter` (property `api_key`) and
`niadra/tenant/bootstrap` (the sandbox tenant's source keys and admin account, which the billing agent's
source and the `memory_v2` flag need) with its own role, into root-only files, and gives the harness the
bootstrap as a read-only file and the provider key through the environment of its gateways and its agent.
The local systems' own tokens and database passwords are random, generated on the host.

`start` and `campaign` take any `bench run` arguments; `--niadra-memory-v2 on|off` works as before (the
flag lives at `settings/memory_v2`; `NIADRA_MEMORY_V2_FLAG` overrides that; two runs with different values
must not overlap in the same space):

```bash
deploy/temp-host/bench.sh start niadra --dataset v2 --niadra-memory-v2 off
deploy/temp-host/bench.sh start niadra --dataset v2 --niadra-memory-v2 on
```

Expected duration per repetition of dataset v2 (356 cases, about 2,330 conversations), not yet measured:
Niadra's seeding at the production cap about 23 minutes, then its settle; Mem0 about 1 h 15 (seeding both
scenarios, the accuracy pass, the timed loops); each added system from minutes (ai-memory without a model)
to hours (Graphiti, whose server adds one message at a time with several model calls each; run it with
`--limit`). `mem0_platform` needs a `MEM0_API_KEY` from a free Mem0 account, added to the harness's
environment by hand; it is not part of the default run.

### What the benchmark left on the cell

Runs until 25/09/2026 ran on the cell. `deploy/cell/cleanup.sh` lists what they may have left there (the
Kubernetes objects labelled `app.kubernetes.io/part-of=niadra-benchmarks`, the `bench-mem0` secret, Mem0's
databases `mem0_bench` and `mem0_bench_app` and the role `mem0_bench` on the RDS instance, the benchmark's
images in the node's containerd); `--confirm` removes them and lists again. The old `bench down --drop-db`
ran `kubectl run -i` inside the script that Systems Manager fed to bash on its standard input, so kubectl
read the rest of the script as the pod's input and the `DROP` never ran. Every script sent through Systems
Manager now is written to a file and run with its standard input closed (`deploy/temp-host/lib.sh`,
`ssm_run`), and the cleanup's SQL goes in as `psql -c` arguments through `kubectl exec` to a pod that only
waits.

## Adding a system

One module in `src/niadra_bench/systems/` with one `HttpSystem` subclass, and its container entry:

1. `src/niadra_bench/systems/<name>.py`: the class sets `system` (the results key), `title`, `compose`
   (its directory under `deploy/systems/`), `url_env` and `default_url` (the service's name in its compose
   file), `version` (the pinned release), and, if it calls a model, `meter_env` and `meter_default` (its
   gateway). It implements `seed_calls` (the calls that write a customer's history, as its docs do it),
   `read_call` and `memories` (a turn's read, and the lines the agent receives), `exchange_call` (one live
   exchange), and, when the system has them, `open_call`, `ensure_calls` (a user to create first),
   `health_call` and `settle` (how to know its background work is done). The module's docstring says how
   the system is used and why that is its documented way. `tests/test_systems.py` checks every adapter.
2. `deploy/systems/<dir>/compose.yaml`: its first line names the keys it serves (`# systems: <key> ...`);
   its services, with every image pinned (a test refuses `latest`), its gateway if it calls a model
   (`bench serve llm-meter` with `LLM_UPSTREAM: ${BENCH_LLM_UPSTREAM}`, `LLM_API_KEY: ${BENCH_LLM_API_KEY}`
   and `EMBED_UPSTREAM: http://embed:8080/v1`), and `mem_limit` on each, so it fits the host beside the
   harness.
3. A dry run: `deploy/local/run.sh <key>`, then its row in the field table above, with its caveats.

The registry finds the class by its key; `bench run --systems <key>` measures it with every metric.

## A/B: the delta between two settings

`bench ab` runs a baseline and a candidate over the same prepared dataset and reports the delta, figure
by figure. The candidate differs from the baseline by `--candidate-env KEY=VALUE` (repeatable), a setting
of the Niadra server under test, or by `--candidate-config <file.toml>`, whose sections override
`benchmark.toml`'s. A key only the candidate sets runs on the baseline at its default
(`NIADRA_MEMORY_V2=off`, `NIADRA_SEMANTIC_CHANNEL=off`; any other key needs `--baseline-env`), so both
sides say what they ran with. Everything else is equal: the same cases in the same order, seeded from the
same instant (the start of the hour the A/B began, `--now`), the same agent and judge, repetition by
repetition (baseline 1, candidate 1, baseline 2, ...). Both sides are graded on the same valid cases: the
two references of the validity rule answer once per repetition, on the baseline side, and their verdict
holds for both.

What the delta covers (`ab.md`, and `ab.json` with schema `niadra-bench.ab.v1`): accuracy overall and by
category (the judge when there is one, else the exact check), `context_has_answer` overall and by category,
tokens per view (median and p95), privacy leaks, the p50 and p95 of metrics 1, 8 and 9, and cost. Each
side shows its median over the repetitions and the range between them; the delta is the candidate's
median minus the baseline's. `ab.json` also lists the valid cases that changed verdict (`flips`: the
answer, and whether the memory block held it), by category. An A/B folder has no `summary.json`, so the
site's importer never takes one.

`bench ab --same` is the determinism check: the candidate is the baseline again, and every accuracy,
`context_has_answer`, privacy and token figure must come out identical, per repetition and case by case
(latency may differ). It exits with 1 and lists what differed otherwise. Run it on the same checkout and
place before trusting a delta.

Where the two sides run:

- **A local cell** (`--local-cell <niadra-back checkout>`): no AWS, no key. Each side starts
  `deploy/local/cell_server.py` inside that checkout (`uv run --project <checkout>`, with the checkout as
  the working directory), with the side's settings in its process environment: niadra-back's in-memory
  flow harness (`tests/unit/flow/harness.py`, every real service and task handler) behind its own HTTP
  routes, which the harness reads through the SDK as it reads the region. A batch answers after the
  pipeline ran every task it started, so nothing is left to settle. What stands in for the cloud, the
  same on both sides: a rule extractor in place of the extraction model (the session's ask and each line
  that settles a new number as the episode summary, the category and intent by keywords, no facts), the
  hash embeddings and the real rule gate of `models/`, a clock fixed at `--now` (default: the hour the
  A/B started), and one in-memory cell per customer and repetition (the in-memory store is copied to open
  each transaction; a cell per customer keeps a side of dataset v2 at about five minutes a repetition on
  a laptop). What a space learns across customers (memory v2's nightly weights) does not run here. The
  agent is `context` unless `--agent llm` (which needs `OPENROUTER_API_KEY`).
  `NIADRA_SEMANTIC_CHANNEL=models` gives the read path the hash encoder; `inprocess` needs the model
  files and is refused. Results go to `results/local/ab/`, which is not committed. These numbers compare
  two settings of the same code: they are never published, and no absolute figure of a local cell says
  what the region would measure.
- **The emulator** (`--mock`): niadra-mock on both sides. It has no server settings, so it runs only
  `--same`; the CI runs it.
- **The region** (neither option, from the temporary host): the Niadra of `NIADRA_BOOTSTRAP`, read and
  written within the production caps (config `[production]`). The harness
  changes only what the bootstrap's admin account changes through the control API, the space's settings:
  `NIADRA_MEMORY_V2` (as `--niadra-memory-v2` does, put back at the end of each side's repetition). A
  setting of the read deployment's process, such as `NIADRA_SEMANTIC_CHANNEL`, is refused there: that
  deployment also serves production; that A/B runs on a local cell until the region has a read deployment
  of the benchmark's own. The two sides seed different customers (the same cases) into the same space,
  one after the other. `[ab] max_minutes` in `config/ab.toml` (450) stops an A/B before a repetition that
  would pass it, and the report says it is incomplete.

A local cell imports the checkout's code when it starts and records that commit. Point it at a worktree
that nothing else pulls during the A/B (`git worktree add ../wt/niadra-back-ab origin/main`, then `uv
sync` there): a checkout that moves mid-run leaves the cell with code from two commits.

```bash
# On a laptop, against a niadra-back checkout (its environment synced with `uv sync`):
uv run bench ab --local-cell ../../niadra-back --same --dataset v2
uv run bench ab --local-cell ../../niadra-back --candidate-env NIADRA_MEMORY_V2=on --dataset v2
uv run bench ab --local-cell ../../niadra-back --baseline-env NIADRA_MEMORY_V2=on \
    --candidate-env NIADRA_SEMANTIC_CHANNEL=models --dataset v2
# A branch against main: the candidate runs on its own checkout.
uv run bench ab --local-cell ../../niadra-back --candidate-cell ../../wt/niadra-back-mybranch \
    --baseline-env NIADRA_MEMORY_V2=on --dataset v2

# In the region, from the temporary host (see "Running it on the temporary host"; `collect` copies
# results/ab/<date>-<id>/ too):
deploy/temp-host/bench.sh ab --same --dataset v2
deploy/temp-host/bench.sh ab --candidate-env NIADRA_MEMORY_V2=on --dataset v2
```

## Ranking gate

No change to how memory v2 picks and orders what a pack carries enters niadra-back's `main` without its
delta attached to the pull request: `domain/serve/slots.py`, `domain/compile/scoring.py`,
`domain/measure/weights.py`, `DEFAULT_WEIGHTS`, and any flag that turns a retrieval channel on. The delta
is `bench ab` with the change as the candidate (a flag, or the branch's checkout as `--candidate-cell`
against `main`'s as `--local-cell`), on dataset v2, three repetitions, with `bench ab --same` passing on
the same checkout first; the PR carries `ab.md`. A category that loses accuracy or `context_has_answer`
beyond the range of its repetitions blocks the change, as do median tokens that rise without an accuracy
gain. Turning `memory_v2` or the semantic channel on by default also needs the region's A/B, committed
under `results/ab/`, and the decision cites that file.

## Publishing

1. Put the runs of one campaign together with `bench combine` (one folder, the site's schema, with the
   list of runs in `combined_from`) and commit `results/<date>-<id>/` here (summary, repetitions, and the
   per-case rows that let anyone audit every grade), with the runs it came from.
2. In niadra-frontend: `node scripts/import-benchmark.mjs ../niadra-sdk-python/benchmarks/results/<date>-<id>/summary.json`.
   The importer refuses anything but a `region` run. Each added system appears as a new `system` key in
   every metric; Niadra's paths are now `edge` and `vpc`, every other system's `host`; privacy lines carry
   `verification`; the environment adds `harness_host` and `harness_machine_class`.
3. Where Niadra loses on a metric, add one line with the most likely reason to `site_notes` in the
   imported file (`{"pt": {"latency": "..."}, "en": {...}}`); the page prints it under that chart.

## Local checks and a dry run without keys

```bash
uv sync
uv run pytest -q                       # unit tests, dry runs against niadra-mock and fakes, the deploy scripts
uv run bench prepare --check           # both committed datasets are what the generator makes, and valid
uv run bench run --mock --systems niadra --quick --limit 28 --repetitions 1 --output /tmp/bench   # no network
uv run bench ab --same --mock --quick --limit 28 --repetitions 1                    # the A/B plumbing
deploy/local/run.sh                    # every system, one at a time, on Docker with fakes; then combined
deploy/local/run.sh ai_memory -- --dataset v2 --quick --limit 14 --repetitions 1
```

The local pipeline runs each system's real server and databases with fake model and embedding servers and
niadra-mock, so it needs no key. It proves the plumbing only; its numbers are never published.

## Known limits of the comparison

- Mem0's open source `add()` takes no timestamp (Platform only), so Mem0 learns the order of events
  from the order they were added; Niadra receives each event's time.
- Mem0's hybrid search lemmatizes with spaCy's English model, as its README installs it, also for the
  Portuguese cases.
- Mem0 v2.2.0's server lists plain `psycopg`, which does not import on `python:slim`; the image adds
  `psycopg[binary]`, the change Mem0 made on its main branch after the tag.
- The rerank column is measured in-process, so it has accuracy, tokens and cost but no latency,
  navigation or ingestion line.
- Navigation measures `search` and `open`, not `timeline` (the third call the docs name): Mem0 has no
  paged history of a customer to set against it beyond listing every memory.
- Mem0's `open` equivalent reads one memory, a sentence; Niadra's `open` builds an episode. The two
  lines are the nearest calls, not the same work.
- Mem0's ingestion lines are the open source server's two synchronous modes. The hosted Platform's
  asynchronous `add()` (`async_mode`, the answer before the extraction) is the closest to Niadra's
  acknowledgement, but the Platform runs outside the region, so it is not measured.
- The billing agent's source is created with the bootstrap's admin account only when the harness has
  `NIADRA_CONTROL_URL` (the temporary host sets it to the control plane's public address). Without it (a local run, niadra-mock) the actions go
  through the sandbox's starter billing source; any refused action is still listed under
  `seed.niadra.refused_actions`, and `seed.niadra.declared_operations` says which setup the run used.
- The agent answers from one read, with no tools. Niadra's voice guide also gives the agent
  `search_customer_history` for older matters; the benchmark measures the pack alone, as it measures one
  `search()` for Mem0 and one read for every added system (ai-memory's agents may also call
  `memory_read_page` on a hit; Hindsight's `reflect` and Honcho-style reasoning reads are not measured).
- E-mail and app sessions are written with the WhatsApp source's key and their own `channel`, because
  the sandbox has no e-mail or app source; the memory records the event's channel, so the packs are the
  same, but a company would give each channel its own source and key.
- Niadra's freshness read verifies the voice call at V1 before the clock starts, as every accuracy probe
  does: an unproven call reads at V0, where the starter policy withholds order numbers.
- The third Mem0 column of the plan (its own defaults, `gpt-4o-mini` and `text-embedding-3-small`)
  needs an OpenAI embeddings key and is not in the default run.
- Niadra is read across a network (its public address, or its private address in the VPC through the
  cell's ingress); every other system answers on the harness's own host. Their latency lines have no
  network in them; Niadra's do.
- The embedding line `encode` times the copy of the embedder on the harness's host, which Mem0 and the
  added systems call; Niadra's read service calls the cell's own copy, which that line does not time.
- Niadra's history navigation and ingestion run at 10 per second only (the production cap); the other
  systems at 10 and 25. The 25 per second lines have no Niadra counterpart.
- The added systems' ingestion lines use each system's documented write for a live exchange: an
  asynchronous one where the system has it (Graphiti's queue, Hindsight's `async` retain, Supermemory's
  queued document, MemOS's `async` add, ai-memory's hook batch), so like Niadra's they answer before the
  memory is built; Memobase's insert goes to its buffer. Mem0's open source server has no such mode.
- Runs of different systems are put together by `bench combine`, with one validity rule for all (the
  first run's references); each system still ran alone on the host, at a different time, against the
  same dataset and configuration hash.

