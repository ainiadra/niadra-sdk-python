# Niadra benchmark

An open, reproducible benchmark of Niadra against the memory layers teams compare it with: Mem0, and a
field of other memory systems added one adapter at a time (Graphiti, Hindsight, Memobase, Supermemory
local, MemOS, Redis Agent Memory Server, Honcho, LangMem, Cognee). It measures what an engineer
integrating a memory for customer-facing and internal agents needs to know, with the same model on every
side where a system lets it be chosen (GPT-6 Luna at reasoning `low`, the model Niadra runs), and
publishes every result file. Where Niadra loses, the number is published the same way.

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

Beside the grades, every answered probe of every system is checked for **values without a source**
(`metrics/backing.py`, the metric line "valores sem lastro por mil respostas"): the numbers, dates,
codes and amounts the agent's answer states that neither that system's memory block nor the customer's
words in the turn back (the probes call no tool, so there are no tool results). The rule is the SDK's
backed-answers check (`niadra.backing.check`: amounts, dates, codes of letters with three or more digits,
numbers of three or more digits; never words, one or two digits, times or a year alone; sums and counts of
backed amounts are backed), applied by the harness to every system's answers with the same inputs, so it
measures what each memory gave the agent, not an SDK; no system's agent runs it during the pass. Each line
(`metrics.backing`) gives the answers, the values they stated, the values without a source per thousand
answers (median and range over the repetitions), by kind and by category. The accuracy, contradiction
(`contradiction` category) and privacy figures are unchanged by it. The rule comes from the SDK the harness
installs from PyPI (`niadra.backing`, from 0.5.0 on).

Each privacy line says whether the system has a verification mechanism at all (`verification`:
`per conversation` for Niadra, `none` for every other system). A system with none hands the block to
any caller, so the page shows "no mechanism" for it rather than a score; the count of blocks that held
the sensitive value is still in the file. Results before 26/09/2026 do not carry the field.

Since 27/09/2026 every Niadra probe's row also carries what its pack left out and why
(`meta.exclusions`, `metrics/exclusions.py`): after the accuracy pass the harness reads the probe's read
receipt and its lineage (`POST /v1/receipts/search`, `GET /v1/lineage/receipt/{id}`), whose manifest lists
each excluded item with its reason (`policy`, `verification`, `quarantine`, `budget`) and rule (for
example `min_verification`, `skeleton`, `view`); ids, kinds and categories only, never a value. Each
repetition then attributes Niadra's lost answers by data (`exclusions.attribution`): `withheld` when the
pack held something back by policy or verification, `not_withheld` when it held nothing back, `unknown`
without a manifest. The reads need an admin credential: the sandbox's admin person in the region (the same
login that issues the billing key), or a local cell's `admin_key`; a run without one records none. Every
run also writes `summary.md` beside `summary.json` (`bench report` rebuilds both): accuracy by category
for every system, with Mem0's two identity conditions side by side, `shared id` (`known_id`, one user id
on every channel, its best case) and `id per channel` (`per_channel_id`), and Niadra's lost answers by
cause.

We do not run LoCoMo, LongMemEval or BEAM: they are long personal conversation sets and do not measure
what a Niadra buyer buys.

## How the comparison is kept fair

- **Same place, and Niadra's production kept safe.** Since 26/09/2026 the harness and every system but
  Niadra run on a temporary EC2 host of their own in the cell's VPC in us-east-2 (`deploy/temp-host`),
  one system at a time, each with its own databases on that host: nothing of theirs touches the cell's
  machine or its RDS instance. Niadra is measured where it runs, the cell (m7i-flex.large, RDS
  db.t4g.micro), through its public TLS address (`edge`) and through the cell machine's private address
  in the VPC with the same TLS name (`vpc`); every other system is on the harness's own host (`host`), so
  Niadra's `edge` and `vpc` lines carry a network hop the others do not, and the page says so. Niadra's
  own `host` lines, from a pod on the cell's machine straight to each deployable over plain HTTP
  (`deploy/cell/host-lines.sh`, `NIADRA_HOST_ADDRESS`), are measured the way the others are. Until
  25/09/2026 everything ran as pods of the cell (`cluster`); on that evening the second run saturated PgBouncer and the RDS
  instance went into recovery (niadra-docs `estudo/09-RODADAS.md`), which is why the benchmark left the
  cell. What a run may send to the cell is capped (config `[production]`, "Load on production" below).
- **Same model where a system lets it be chosen.** Niadra's cell extracts with `openai/gpt-6-luna` and
  decides with `typesafe/jev-1.13` (set in niadra-back; config `[models] niadra` records it). Every other
  system that calls a model uses `openai/gpt-6-luna` at reasoning effort `low` through OpenRouter (config
  `[models]`), set the way each system's own settings do it (the table in "Models, system by system"
  below). Every system that takes an embedder uses the one the cell runs (`niadra-models`,
  `paraphrase-multilingual-MiniLM-L12-v2`, 384 dimensions), the same image and model files, on the
  harness's host, through a small OpenAI-compatible proxy (`bench serve embed-proxy`). Each system's model
  calls go through a gateway of its own (`bench serve llm-meter`), which counts the spend from the
  provider's usage numbers (cache reads and writes and reasoning tokens apart), sends embeddings to that
  embedder when the system has a single base URL, asks every call for the benchmark's reasoning effort
  (`LLM_REASONING_EFFORT`, so a call its settings do not reach runs at the same effort, and counts the calls
  that arrived without it), and, when a system's server fixes some model names that its settings do not
  reach (Graphiti's small model), asks for the same model. A system whose embedder or reranker cannot be
  set keeps its own, named in the field table.
- **One model family for the agent and the judge too.** One agent (`openai/gpt-6-luna`, reasoning `low`,
  `seed` 20260924, fixed prompt in `src/niadra_bench/agent.py`) answers every probe, and one judge on the
  same model with a published rubric grades it; an exact check that looks for the expected value in the
  answer is reported beside it, and needs no model. Runs before 27/09/2026 used `openai/gpt-4.1-mini` at
  temperature 0 for both. The change keeps one model family for everything the benchmark runs, the memory
  systems, Niadra's extraction, the agent and the judge, so no system's memory is read by a model of
  another family than the one that wrote it, and it is cheaper (Luna is $0.10 and $0.50 per million tokens
  of input and output, gpt-4.1-mini $0.40 and $1.60). Luna takes no temperature (OpenRouter drops the
  field), so the answers are made repeatable with `seed`; its reasoning tokens count toward `max_tokens`,
  set to 2,000 for an answer of one or two sentences. A judge from the same family as the agent may grade
  that family's answers more kindly; the exact check beside it has no model, and every per-case row keeps
  the answer and the verdict for anyone to grade again.
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
  Graphiti's server, whose two fixes are needed for it to store anything and are listed below). LangMem is a
  library with no server: it runs in a small service of the harness's (`deploy/systems/langmem/server.py`)
  that calls only its documented API. Honcho is AGPL: the harness runs its published image and calls its
  REST API, and no line of it is in this repository.
- **Niadra as documented.** The SDK is the current release on PyPI (`niadra==0.6.1`, whose `track()` sends a
  conversation turn at once where 0.5.0 waited up to 0.2 s, which the freshness metric counts; the first run
  installed 0.1.5 and run 2026-09-25-6efee4 0.3.0, whose read path is the same for a read with its own
  `query`). 0.4.0 is the first that puts the turn's `slots` in `turn_block` (0.3.0 drops the field), so
  a run measures what an agent gets only from 0.4.0 on; its turn block is the live turns, the slots,
  then the delta, where 0.3.0 put the delta first. Each exchange is a batch of `message` events with its
  `occurred_at` and a `conversation.ended`, system records are `system_event`s, the billing agent's
  records are `action`s, and every probe verifies the call or chat before `context()`, as the voice
  and WhatsApp guides show; the question goes in `query`, which picks the turn's slots and never changes
  the pinned pack. The billing agent has a source of its own that
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
  added system gets one store per customer (a user, group, bank, project, tag, peer, namespace or
  dataset) with every channel in it: the `known_id` scenario, their best case. Niadra receives the same handles in every scenario.
- **Time.** Where a system's API takes the time of an event, it gets it (Graphiti's message `timestamp`,
  Hindsight's `timestamp`, Memobase's `created_at`, MemOS's `chat_time`, Redis Agent Memory Server's and
  Honcho's message `created_at`); where it does not (Mem0's open source release, Supermemory local, LangMem,
  Cognee), the order of the writes is the only time it has. Redis Agent Memory Server stores each
  message's time, but its extraction prompt grounds relative dates on the time the extraction runs.
- **Fresh customers.** Every repetition seeds new phone numbers, e-mails and ids, so extraction runs
  again and no memory has seen them. Each metric runs three times; the site shows the median and the
  range.
- **Frozen inputs.** `config/benchmark.toml`, `config/mem0.config.json` and `dataset/cases.jsonl` are
  committed; every result file carries their hashes, the harness commit, the package versions, each
  added system's pinned version and the deployed Niadra server version. Dataset v2 adds
  `config/dataset.v2.toml` and `dataset/v2/cases.jsonl`; a v2 run's configuration hash also covers the v2
  settings, and a v1 run's hash is computed exactly as before v2 existed.

### Guard lines

Niadra writes a guard line (`[Guarda]` / `[Guard]`, in the turn block's slots) for a kind of value (a
date, an amount, a protocol, an order...) only after its measurement counted agents contradicting a value
of that kind in the last 30 days (`context_use_guard_daily`), and only when a system of record or a human
agent settled the value the customer's turn asks for. A fresh benchmark space has no such count, so a run
sees no guard lines unless it makes them appear, and says how:

- **In the region, measure first:** `bench run --niadra-guards measure` sends every Niadra probe's answer
  back to Niadra as the agent's outbound message in the probe's conversation, with the backed-answers
  fields a checked turn carries (`backing`: the count and the kinds of the values without a source, never a
  value), and ends the conversation. Niadra's measurement then counts the contradictions, and the read path
  (which rereads the contradicted types once a minute) writes guards in the reads after it: in practice the
  repetitions after the first. The writes go through the production caps. `summary.json` records it
  (`config.niadra_guards`).
- **On a local cell, seed:** `NIADRA_BENCH_GUARD_TYPES=<types>` in a side's settings (for example `bench
  ab --local-cell ... --candidate-env NIADRA_BENCH_GUARD_TYPES=date,amount,protocol,order`) starts every
  customer's cell with one contradiction of each type measured today; the cell's `cell.json` lists the
  seeded types. The region's rows are never written by the harness.

Each Niadra row of `cases-rep<n>.jsonl` counts the guard lines its read carried (`meta.guards`). On dataset
v2 most values are stated by the AI agent in the conversations, which a guard never holds the agent to, so
few probes get one: an A/B on a local cell (28 cases, every type seeded) showed none, while the seeded types
did reach the read path's query (`contradicted_value_types`).

## The field

Each system runs from its own container entry (`deploy/systems/<dir>/compose.yaml`, versions pinned),
alone on the host, and through its adapter (`src/niadra_bench/systems/<module>.py`). The columns are what
the adapter does, the way the system's own documentation does it, and what the reader of a number must
know.

| Key | System and version | Containers | How it is used | Caveats |
|---|---|---|---|---|
| `niadra` | Niadra, the cell's deployed server | the cell | `POST /v1/batch` per session with each event's time and channel; `POST /v1/context` per turn with the question as `query`; voice and chat views | The only system with identity across channels and a verification level per conversation; read over a network hop (edge and VPC) |
| `mem0_oss`, `mem0_oss_rerank` | Mem0 open source, v2.2.0 | its REST server, PostgreSQL with pgvector, its gateway | above | No event time; one extraction call per exchange; the rerank column is in-process (no latency line) |
| `graphiti` | Graphiti server 0.30.2 (`zepai/graphiti`) with Neo4j 5.26.2 | server, Neo4j, gateway | `POST /messages` per session with the customer's `group_id`, each message's `role_type`, `role`, `timestamp` and the channel; the settle reads `GET /episodes/{group_id}` until every message is an episode; `POST /search` with `max_facts` 10; `GET /entity-edge/{uuid}` as `open` | Its server needed two fixes to store anything (`deploy/systems/graphiti/patch.py`): the client of a request was closed when the request ended, before the background queue used it, and the queue's worker stopped for good at the first failed episode. Its queue adds one message at a time, with several model calls each: a full dataset v2 repetition is about 5,200 episodes, many hours; run it with `--limit` and say so. One base URL for its model and its embedder; the gateway pins its small model to the benchmark's model |
| `hindsight` | Hindsight 0.10.1 (`ghcr.io/vectorize-io/hindsight`, embedded PostgreSQL) | one container, gateway | One bank per customer; one `retain` item per session, the whole conversation as `Name (timestamp): text` lines with its `timestamp`, a `context` label and a `document_id`, as its docs ask for a conversation (not one per turn); `recall` with its defaults; `GET .../memories/{id}` as `open`; a live exchange is an `async` retain | Its reranker is its default local cross-encoder |
| `hindsight_reflect` | the same, read with `reflect` | its own server and gateway (`deploy/systems/hindsight-reflect`) | the same writes; each read is `POST .../reflect` with the question and its defaults, and the agent receives its answer text | A model call on every read: its latency, its cost and its tokens include the reasoning |
| `memobase` | Memobase v0.0.42 (built from its repository), PostgreSQL with pgvector, Redis 7.4 | API, database, Redis, gateway | One user per customer (a UUID from the customer's id); one `ChatBlob` per session with each message's `created_at`, then `flush`, as its docs ask at the end of a session; `context()` with `max_token_size` 600 on voice and 1,500 in chat (Niadra's view budgets) and the question as the current chat | No commit since 11/01/2026; profile topics are its defaults (written for companions and assistants, not customer service); a live exchange waits in its buffer until it flushes by size or age, which metric 6 counts |
| `supermemory` | Supermemory local, `supermemory-server` server-v0.0.8 (MIT binary) | the binary, a forwarder in its network namespace, gateway | One `containerTag` per customer; one document per session (`customId`, the conversation as `user:` and `assistant:` lines, `dreaming: "instant"`, which its docs name for benchmarking); `POST /v4/profile` with the question, and the context its quickstart builds from it (static profile, dynamic profile, related memories) | No event time. The local binary is licensed for 10,000 documents (about 2,800 per repetition of dataset v2): run one repetition per fresh container. It signs requests from its own host with its key, so the harness reaches it through a forwarder (`supermemory-local`) that adds a loopback hop. No search mode "documents" (the dataset has no documents) |
| `memos` | MemOS v2.0.34 (built from its repository), Neo4j 5.26.6, Qdrant 1.15.3 | API, Neo4j, Qdrant, gateway | One user and cube per customer; `POST /product/add` per exchange with `chat_time` and the session, `async`; the settle calls `POST /product/scheduler/wait` for every customer and then waits for the memory count to stay the same (the wait needs the optional Redis queue, off in its example configuration); `POST /product/search` with its defaults (`fast`, `top_k` 10) | Its activation memory (a KV cache of a local model) is outside the server API and not measured; a CRM or ERP record is a `system` message |
| `redis_agent_memory` | Redis Agent Memory Server 0.15.2 (Apache 2.0; `redislabs/agent-memory-server:0.15.2-standalone`, the open server its repository keeps under `V0/`) | one container (Redis 8, API, task worker), gateway | One user per customer; one working memory per conversation (`PUT /v1/working-memory/{session}`) with every message's role and `created_at`, which the server promotes to long-term memory in the background with its default `discrete` strategy (its "background extraction" pattern); the settle waits until every message is marked extracted and the memory count stays the same; `POST /v1/long-term-memory/search` with the question and the `user_id` and its defaults (semantic, `limit` 10, recency boost); `GET /v1/long-term-memory/{id}` as `open` | A research artifact, not Redis's product (Agent Memory in Redis Iris runs only in its cloud). Its extraction waits 30 s after a conversation's last write (its debounce), which metric 6 counts; its prompt grounds relative dates on the time the extraction runs, not on the messages' time. A CRM or ERP record is a `system` message. Topic and entity extraction run through the same model |
| `honcho` | Honcho v3.2.1 (AGPL 3.0; `ghcr.io/plastic-labs/honcho`), PostgreSQL 15 with pgvector, Redis 8.2 | API, deriver, database, Redis, gateway | One workspace; a peer per customer and two peers for the company (its agents, its systems of record) with `observe_me` false, as its design patterns ask; one session per conversation, each message with its peer and `created_at`; the settle waits for the workspace's queue to empty; the customer peer's context (`GET .../peers/{id}/context`, peer card and representation) with the question as `search_query` | Run as a service only: no line of Honcho is in this repository. Its deriver reasons only once a work unit holds 512 tokens or is 30 minutes old (its defaults), so a short live exchange waits up to 30 minutes, which metric 6 counts (it times out at 30 s). No `open`: the context carries no item ids. The pgvector columns are set to 384 dimensions at first start with its own script, as its docs ask |
| `honcho_dialectic` | the same, read with the dialectic API | the same containers, another workspace | the same writes; each read is `POST .../peers/{id}/chat` with the question and its defaults (`reasoning_level` low), the quickstart's `peer.chat()`, and the agent receives its answer text | A model call on every read: its latency, its cost and its tokens include the reasoning. Shares the containers and the gateway with `honcho`: run the two keys one at a time |
| `langmem` | LangMem 0.0.30 (MIT), LangGraph's `AsyncPostgresStore` in PostgreSQL 17 with pgvector | the harness's LangMem service, database, gateway | One namespace per customer (`("memories", "{user_id}")`); each conversation to `create_memory_store_manager` with its defaults, queued and processed in order by one worker (as its `ReflectionExecutor` does), the service answering 202 once queued; the store's search with the question and `limit` 10 (its `search_memory` tool's default); the store's `get` as `open` | A library with no server: the service that runs it is the harness's (`deploy/systems/langmem/server.py`), calling only its documented API. No event time. One worker in order means seeding a full repetition of dataset v2 takes hours: run it with `--limit` and say so. A CRM or ERP record is a `system` message |
| `cognee` | Cognee 1.6.1 (Apache 2.0; `cognee/cognee`), its default local stores (SQLite, LanceDB, its embedded graph) | one container, gateway | One dataset per customer, access control on (its default: each dataset has its own stores; the harness logs in as its default user); each session added as a text (`POST /api/v1/add`), then one `cognify` of the dataset, which returns when the graph is built; `POST /api/v1/search` with `GRAPH_COMPLETION` (its guide's search), the question, the dataset and a session of the customer's own, and the agent receives the answer text | A model call on every read (its answer, plus its automatic turn analysis): latency, cost and tokens include them, and its image serves with one process (`gunicorn -w 1`, its entrypoint), so reads wait for each other. No event time. The server's default search is now `HYBRID_COMPLETION`; the guide's `GRAPH_COMPLETION` is used. A live exchange is `remember` (add, cognify and its improve step) with `run_in_background`. The benchmark's embedder is not a HuggingFace model id, so its chunk sizing counts tokens with TikToken (it logs a warning). Its `cognify` answers only when a customer's graph is built, so its calls wait up to 30 minutes (every other call 60 s; on 27/09 seven customers with 30 to 60 sessions passed 60 s). Made for documents and code, not customers |

### Models, system by system

Every system that lets its model be chosen calls `openai/gpt-6-luna` at reasoning effort `low` (config
`[models]`; each compose file repeats both as the defaults of `BENCH_LLM_MODEL` and
`BENCH_LLM_REASONING_EFFORT`, and a test checks that they agree). The model is set where each system's own
configuration sets it; the effort too where the system has a setting for it. Every system's gateway also
asks each call for the same effort, which is what reaches the calls of a system with no such setting, and
counts the calls that arrived without it (`reasoning_set` in the run's `model_usage`).

| System | Its model setting | Its reasoning effort setting | What stays fixed |
|---|---|---|---|
| `niadra` | the cell's deployment (niadra-back): Luna extracts, `typesafe/jev-1.13` answers the typed decisions (the extraction gate among them) | the cell's deployment, not the harness | nothing |
| `mem0_oss`, `mem0_oss_rerank` | `llm` and the reranker's `llm` in `config/mem0.config.json` | `reasoning_effort` in the same config, with `is_reasoning_model` true: Mem0's name check does not know Luna, and a reasoning model gets neither temperature nor `max_tokens` | nothing |
| `graphiti` | `MODEL_NAME`; its server fixes a small model (`gpt-4.1-nano`) that no setting reaches, which the gateway replaces with Luna | none: its client sends an effort only to model names it knows as reasoning models (gpt-5, o1, o3), so the gateway's | nothing |
| `hindsight`, `hindsight_reflect` | `HINDSIGHT_API_LLM_MODEL` | `HINDSIGHT_API_LLM_REASONING_EFFORT`, which every operation inherits | its reranker, a local cross-encoder (no model call) |
| `memobase` | `best_llm_model`, `thinking_llm_model` and `summary_llm_model` in its `config.yaml` | none, so the gateway's | nothing |
| `supermemory` | `OPENAI_MODEL`, which its fast and text models default to | none, so the gateway's | nothing |
| `memos` | every model setting of its `.env.example` | none, so the gateway's | its activation memory's local model, outside the server API and not measured |
| `redis_agent_memory` | `GENERATION_MODEL`, `FAST_MODEL`, `SLOW_MODEL` (LiteLLM's `openai/` route) | none, so the gateway's | nothing |
| `honcho`, `honcho_dialectic` | every `*_MODEL_CONFIG__MODEL` (deriver, summary, the five dialectic levels, both dreams) | every `*_MODEL_CONFIG__THINKING_EFFORT` | any model name no setting reaches, replaced by the gateway |
| `langmem` | the `ChatOpenAI` its manager is built with (the harness's service) | `ChatOpenAI(reasoning_effort=...)` | nothing |
| `cognee` | `LLM_MODEL` | `LLM_ARGS` with `reasoning_effort` (the arguments it merges into every completion call; LiteLLM may drop it for a model it does not know, and the gateway sets it) | any model name no setting reaches, replaced by the gateway |
| `mem0_platform` | Mem0's hosted service, not settable | not settable | its whole model stack (not in the default run) |

Every embedder is the benchmark's (above) but Hindsight's reranker, which is not an embedder. No measured
system has an extraction model it will not let the benchmark set.

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

Before a run, check that the cell's deployment extracts with `openai/gpt-6-luna` at reasoning `low` and
decides with `typesafe/jev-1.13` (niadra-back's settings; the run records the server version it read, and
config `[models] niadra` names the models), and that the sandbox space's daily ceilings cover the run. With
Luna and Jev a conversation costs about $0.0007 (Luna about $0.0006 at 3,200 tokens in and 550 out with its
reasoning, Jev about $0.0001 at 2,150 in), so three repetitions of dataset v2 (about 7,200 conversations
with the timed writes) need about $5 of extraction and $0.65 of decisions; the operator sets
`llm_daily_micros:extraction` to at least 8,000,000 and `llm_daily_micros:decisions` to at least 1,000,000
with `PUT /v1/quotas`. Past a ceiling, that work waits for the next day and the settle step sees nothing
change, so the accuracy pass would read half-built memory.

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
  TLS name (`vpc`, `NIADRA_VPC_ADDRESS`). From a pod on the cell's own machine they go straight to the
  deployable that serves each path, over plain HTTP inside the cluster (`host`, `NIADRA_HOST_ADDRESS`, a
  base URL or a template with `{service}`; `deploy/cell/host-lines.sh` runs it), the way every other
  system is reached on the harness's host. `NIADRA_PATHS` keeps only the paths it names. None of these
  settings is in the frozen config: the config hash does not change. Runs before 26/09/2026, when the
  harness ran as a pod of the cell, have a path `cluster`: each service's own address, through the
  cluster's network.
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
customers are most of the difference), so seeding Mem0 and Niadra's extraction take about twice as long
and cost about twice as much. On 25/09/2026, with the models of then, a v1 repetition of Niadra and Mem0
cost about $6.50 on OpenRouter. "Cost of a full campaign" below has the estimate with Luna.

## Cost of a full campaign

An estimate, for the budget only: every run's gateways measure the real spend, and each run's
`model_usage` and cost lines report it. The campaign is the one in "Running it on the temporary host":
Niadra and Mem0 at 3 repetitions, every other system at 1, all on the whole of dataset v2 (per repetition:
356 cases, 2,328 conversations, 2,400 exchanges and 452 system records). Prices are OpenRouter's on
27/09/2026 (config `[prices]`): GPT-6 Luna $0.10 per million input tokens, $0.50 output, $0.01 read from
the cache, $0.125 written to it; Jev $0.042 input, its answers free.

What the estimate assumes, none of it measured yet:

- 150 reasoning tokens per call at effort `low`, billed as output (niadra-back measured about 370 per
  extraction at Luna's default effort on 26/09).
- No cache hits. Each system's fixed instructions are the prefix OpenAI caches from 1,024 tokens, and a
  cache hit costs a tenth of the input price, so the measured input cost can be much lower (Mem0's prompt,
  about 8,200 tokens per `add()`, most of all); a prefix written to the cache and never read again costs a
  quarter more than plain input for those tokens. The cost lines report the share read from the cache
  (`cache_read_share`).
- Tokens per call from each system's design (its prompts and how many calls a write or a read makes), and
  for Mem0 from the prompt size measured on 25/09 (8,240 tokens in and 91 out per `add()`).
- Every system's writes include the timed writes of metrics 9 and 6 (1,070 per repetition), and a system
  that reasons on every read also reasons on the timed reads of metrics 1, 7 and 8 (2,140) besides the
  accuracy pass.
- The agent and the judge: 500 tokens in and 40 out per answer, 350 in and 30 out per grade, so $0.096 per
  pass of the 356 cases. Niadra's run asks four passes per repetition (the two references, the chat and
  voice views), Mem0's four (two scenarios, with and without rerank), every other system's one.

| System | Repetitions | What calls the model, per repetition | USD |
|---|---|---|---|
| `niadra` | 3 | on the cell, one Luna extraction (3,200 in, 400 out) and one Jev decision call (2,150 in) per conversation, about 2,410 with the timed writes; four agent and judge passes | 6.10 |
| `mem0_oss`, `mem0_oss_rerank` | 3 | one extraction per `add()` in two scenarios (4,800) and in metric 9's `add_infer` loop (1,050); the rerank column's one call per candidate, 10 per search, 712 searches; four passes | 19.97 |
| `hindsight` | 1 | fact extraction (3,500 in, 400 out) and consolidation (2,500 in, 300 out) per retained item, 3,850 items | 4.33 |
| `hindsight_reflect` | 1 | the same writes, and three calls (4,000 in, 300 out) per `reflect`, 2,496 reads | 9.01 |
| `memobase` | 1 | three calls (2,000 in, 300 out) per flush, about 2,880 | 3.77 |
| `supermemory` | 1 | two calls (2,000 in, 300 out) per document, 3,850 | 3.37 |
| `memos` | 1 | three calls (2,500 in, 400 out) per add (reader, preferences, scheduler), 3,920 | 6.27 |
| `redis_agent_memory` | 1 | three calls (1,500 in, 300 out) per conversation extracted (memories, topics, entities), 2,887 | 3.34 |
| `honcho` | 1 | one deriver call (3,000 in, 500 out) per work unit, about 2,887, and two dream calls per customer | 2.45 |
| `honcho_dialectic` | 1 | the same writes, and two calls (3,000 in, 300 out) per dialectic read, 2,496 reads | 5.07 |
| `langmem` | 1 | two calls (2,000 in, 400 out) per conversation, 3,850 | 3.75 |
| `cognee` | 1 | three calls (2,500 in, 800 out) per customer's `cognify`, four (2,500 in, 600 out) per live exchange's `remember`, two (2,000 in, 200 out) per graph completion read | 5.42 |
| `graphiti` | 1 | six calls (2,500 in, 300 out) per episode, one episode per message, 6,290 | 18.02 |
| **Total** | | | **about 91** |

The least certain line is Supermemory's: in the local dry run of 27/09 (fake model) it made about 40 model
calls per document, not two; if it does the same with Luna, the reasoning tokens alone put its line near
$12. The gateways' `calls` in each run's `model_usage` settle it.

Niadra's extraction and decisions (about $4.95 of its line) are paid by the cell's production key; everything
else, about $86 (Niadra's agent and judge passes included), by the benchmark's own key,
`niadra/bench/openrouter`. That key's limit, US$60, is below this estimate: OpenRouter refuses calls past it,
so in the campaign's order the limit would be reached around LangMem, and LangMem, Cognee and Graphiti
(about $27 together) would fail. The estimate assumes no cache hits and may well come in lower (Mem0's
line most of all), but before the third part either raise the key's limit to about $100, or check the key's
spend on OpenRouter after the second part and run what does not fit with `--limit` (Graphiti with
`--limit 120` is about $6). The temporary host adds about $0.105 an hour ($5 for 48 hours).

## Layout

```
config/                  frozen configuration (benchmark.toml, with the production caps) and Mem0's
dataset/                 the committed dataset v1 and its manifest; dataset/v2/ and dataset/typed/, the same
src/niadra_bench/        the harness (`bench` command)
src/niadra_bench/systems/  one adapter per added system (base.py: what an adapter provides)
tests/                   unit tests, dry runs against niadra-mock and fakes, the deploy scripts with a stand-in AWS CLI
deploy/Dockerfile        the harness image
deploy/compose/base.yaml the embedding proxy and the harness, shared by every run
deploy/systems/<dir>/    one container entry per system (compose.yaml, pinned; a Dockerfile when built from source)
deploy/stack.sh          puts base, environment and system files together and runs docker compose
deploy/local/            the local environment (fakes, niadra-mock) and run.sh, the local pipeline
deploy/temp-host/        the temporary host: up.sh, down.sh, bench.sh (this computer), host.sh (the host)
deploy/cell/host-lines.sh Niadra's `host` lines, from a pod on the cell's machine (this computer starts it)
deploy/cell/cleanup.sh   removes what the benchmark left on the cell when it ran there
results/                 published runs: results/<date>-<id>/{summary.json,rep-N.json,cases-repN.jsonl}
results/ab/              A/B runs in the region: results/ab/<date>-<id>/{ab.json,ab.md,baseline/,candidate/}
results/local/           A/B runs on a local cell or the emulator, and results/local/model-cache/ (not committed)
results/typed/           the typed-object set on a local cell, with and without the blocks: typed.json, typed.md
config/ab.toml           `bench ab`'s ceiling, apart from benchmark.toml so the published hash stays
deploy/local/cell_server.py  the local cell `bench ab --local-cell` runs in a niadra-back checkout
deploy/local/model_cache.py  the local cell's cache of model answers and its spend ledger (`--real-models`)
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

# 3. The campaign, in three parts, each started once `status` says the one before it finished; each
#    comma-separated list is one run, alone on the host, one after the other. Niadra first, with the
#    references, 3 repetitions; Mem0 at 3 repetitions; every other system at 1, on the whole dataset.
deploy/temp-host/bench.sh campaign niadra -- --dataset v2
deploy/temp-host/bench.sh campaign mem0_oss,mem0_oss_rerank -- --dataset v2 --no-references
deploy/temp-host/bench.sh campaign hindsight hindsight_reflect memobase supermemory memos \
  redis_agent_memory honcho honcho_dialectic langmem cognee graphiti \
  -- --dataset v2 --repetitions 1 --no-references
deploy/temp-host/bench.sh status          # repeat: containers, the campaign's progress, the run's log

# 4. The results into results/, then one folder for the site (the first folder's references count,
#    or those of the folder --references names).
deploy/temp-host/bench.sh collect
uv run bench combine results/<niadra run> results/<mem0 run> results/<hindsight run> ...

# 5. Delete everything it created, and check that nothing is left (it refuses while a results folder
#    is still only on S3).
deploy/temp-host/down.sh
```

What `up.sh --confirm` creates, each tagged `niadra:bench-temp-host=<id>`, and `down.sh` deletes and
checks: the instance (m7i-flex.large, 2 vCPU and 8 GiB, free-tier eligible, Ubuntu 24.04, in the cell
machine's public subnet with a public address, IMDSv2 only; the VPC's private subnets have no route out,
by the cell's design, and the host must reach GitHub, the image registries, OpenRouter and Niadra's public
address), its 40 GiB encrypted gp3 volume (deleted with it), a security group with no inbound rule (the
host is reached through Systems Manager only), an IAM role and instance profile (Systems Manager;
`secretsmanager:GetSecretValue` on `niadra/bench/openrouter` and `niadra/tenant/bootstrap` only, never on
the production key `niadra/platform/openrouter`; pull of `niadra/models` only; read and write of
`s3://<cell bucket>/benchmarks/temp-host/<id>/` only), and that S3 prefix. The host terminates itself
after `BENCH_MAX_HOURS` (default 24) even if `down.sh` never runs; the role, the group and the prefix cost
nothing and stay until `down.sh`.

Cost, on-demand in us-east-2 (up.sh reads the price list and prints it again): m7i-flex.large $0.0958 per
hour, 40 GiB of gp3 $0.0044 per hour, a public IPv4 address $0.005 per hour: about $0.105 per hour, $0.84 for
an 8-hour campaign, $2.52 if it runs the full 24 hours. On the free plan it is paid from the account's
credits (about $130). c7i-flex.large (4 GiB) is cheaper ($0.085 per hour) but too small for a system
with Neo4j beside the harness and the embedder; set `BENCH_INSTANCE_TYPE` to change it. The whole
campaign on the full dataset (Graphiti alone is many hours) can pass 24 hours: create the host with
`BENCH_MAX_HOURS=48 deploy/temp-host/up.sh --confirm`. The models are paid on OpenRouter, outside AWS
("Cost of a full campaign" below): Niadra's extraction runs on the cell; each other system's model calls
are counted by its gateway and reported as its cost line.

Memory on the host, one system at a time: the embedder 1.5 GiB and the harness 1.5 GiB at most, then the
system (MemOS: Neo4j 2 GiB, Qdrant 768 MiB, its API 2 GiB; Graphiti: Neo4j 2 GiB, its server 1 GiB;
Hindsight 3 GiB; Supermemory 3 GiB; Mem0 1.8 GiB with its database; Redis Agent Memory Server 1.5 GiB;
Honcho: its API 1 GiB, its deriver 1 GiB, PostgreSQL 768 MiB, Redis 256 MiB; LangMem: its service 768 MiB,
PostgreSQL 768 MiB; Cognee 4 GiB; each system's gateway 128 MiB), under the 7.6 GiB the instance
gives. Neo4j's heap and page cache are capped in the two compose files that use it, the only setting of
theirs changed.

Secrets, by name only: the host reads `niadra/bench/openrouter` (property `api_key`; an OpenRouter key made
for the benchmark alone, with a spend limit of its own, US$60 when it was created on 27/09/2026) and
`niadra/tenant/bootstrap` (the sandbox tenant's source keys and admin account, which the billing agent's
source needs) with its own role, into root-only files, and gives the harness the bootstrap as a read-only
file and the provider key through the environment of its gateways, its agent and its judge: every model
call the benchmark makes goes on that key. Niadra's own extraction runs in the cell on the production key
(`niadra/platform/openrouter`), which the host never reads, and is counted there, apart.
The local systems' own tokens and database passwords are random, generated on the host.

`start` and `campaign` take any `bench run` arguments. `--max-settle-s` caps how long an added system's
settle may wait, below its own minimum (Graphiti's 12 hours, LangMem's 6, most others' 4): a campaign with
a wall-clock limit per system scores what a system has when the cap is reached, its settle is recorded as
`settled: false`, and `config.max_settle_s` says the cap was set. It is a run argument, not configuration,
so the configuration hash stays and the run still combines. Every run but the first of a campaign can take
`--no-references`: `bench combine` decides validity with the first folder's references only, so asking
them again in each run spends the agent and the judge on answers nothing reads (two target passes per
repetition). The first run keeps them and needs every case, and `bench combine` refuses a first folder
without them, unless `--references <folder>` names the folder whose references decide validity (a run
of the same dataset and configuration; its references are the combined folder's, and the first folder's
own are left out). The references may cover fewer repetitions than a system: Niadra once, Mem0 three
times. A repetition past the references' last takes its validity from that last one, and its `rep-N.json`
says which (`validity.references`); the combined `summary.json` gives each system's repetitions and cases
in `per_system`, and `bench combine` prints them.

`bench combine` also scores every system again on the valid cases that every system answered
(`metrics.accuracy_shared`, with `dataset.shared`): when some systems ran on a subset (`--limit`, the
178-case half of dataset v2), that is the comparison on the same cases, and the site leads with it.
`metrics.accuracy` keeps each system on all of its own valid cases.

Niadra's own model spend is not seen by any harness gateway (its extraction runs in the cell on the
production key), so a Niadra run may carry `cell-cost.json`: cost lines read from the cell over the run's
window (the extraction runs' tokens at the harness's prices, the method of every added system's
`models_only` line; the cell's own ledger), with every input that gives them. `bench combine` adds them to
that repetition's cost lines.

When the cell needs to rest between repetitions (on 27/09/2026 a second repetition in a row slowed the
cell's reads past the stop rule while the first had not), Niadra runs as separate runs of one repetition
each, with the cell drained between them, and `bench stack` puts them back together:

```bash
deploy/temp-host/bench.sh campaign niadra -- --dataset v2 --repetitions 1   # three times, a drain between
uv run bench stack results/<niadra run 1> results/<niadra run 2> results/<niadra run 3>
uv run bench combine results/<the stacked folder> results/<mem0 run> ...
```

The stacked runs must be the same measurement (the same dataset and configuration hashes, cases, systems,
references, harness commit and Niadra server version); repetition k of the stack is the k-th in the order
given, its `rep-k.json` names the run and repetition it came from, and `stacked_from` lists the runs.

The combine published on 28/09/2026 (`results/2026-09-28-0e1a50`): Niadra's run on a temporary cell with
production's machine and database classes, server 2026.09.28-0103-b87a029, one repetition of the 356
cases (`2026-09-28-0e264a`), first; then the eleven runs of 27/09. Validity comes from the references of
Niadra's repetition of 27/09 (`partial/2026-09-27-aaf0dd`, server f1c87c8, stopped in its second
repetition), the only references measured beside the competitors' runs; that folder's Niadra rows are
superseded and never combined:

```bash
uv run bench combine --references results/partial/2026-09-27-aaf0dd results/2026-09-28-0e264a \
  results/2026-09-27-9e1e99 results/2026-09-27-1f0e6a results/2026-09-27-eed88a results/2026-09-27-d03ae2 \
  results/2026-09-27-64b5ae results/2026-09-27-320e9d results/2026-09-27-385a79 results/2026-09-27-63d738 \
  results/2026-09-27-a1ff8a results/2026-09-27-4cb045 results/2026-09-27-aa9155
```

The combine of 29/09/2026 (`results/2026-09-28-25548d`): Niadra again on a temporary cell with production's
machine and database classes (m7i-flex.large, db.t4g.micro with the production parameter group,
shared_buffers 88 MiB), running production's images (server `src-cdc4b2673c98bf22`: niadra-back 13b19e5,
niadra-infra c2e40b5), harness 9435eb2. Three runs of one repetition of the 356 cases, the cell drained
between them (`2026-09-28-915dd9`, `2026-09-28-444ce1`, `2026-09-29-ba158a`, each with its `cell-cost.json`),
stacked into `2026-09-28-abcac0` (its `cell-cost.json` gives the three repetitions' lines); then the eleven
runs of 27/09, with the same references. `2026-09-29-c59882` is Niadra's `host` path from the cell's own
machine (`deploy/cell/host-lines.sh`, three repetitions on 20 cases, `--dry-run`); it is not combined:

```bash
uv run bench stack results/2026-09-28-915dd9 results/2026-09-28-444ce1 results/2026-09-29-ba158a
uv run bench combine --references results/partial/2026-09-27-aaf0dd results/2026-09-28-abcac0 \
  results/2026-09-27-9e1e99 results/2026-09-27-1f0e6a results/2026-09-27-eed88a results/2026-09-27-d03ae2 \
  results/2026-09-27-64b5ae results/2026-09-27-320e9d results/2026-09-27-385a79 results/2026-09-27-63d738 \
  results/2026-09-27-a1ff8a results/2026-09-27-4cb045 results/2026-09-27-aa9155
```

The combine of 29/09/2026, later in the day (`results/2026-09-29-74cec8`): the same measurement on the images
production runs after the W6 performance wave (server `src-43f620128f97d891`: niadra-back 212559e,
niadra-infra cc5e1e0), on a new temporary cell of the same classes, harness 093d82f, the same configuration
and dataset hashes. Three runs of one repetition (`2026-09-29-9d0229`, `2026-09-29-40cdd3`,
`2026-09-29-ba953f`, each with its `cell-cost.json`), stacked into `2026-09-29-5fc6f2`; then the eleven runs of
27/09, with the same references. The competitors were not measured again. The first run's timed-loop
sessions were extracted after its drain, when their idle time ran out, inside the second run's window: the
second run's `cell-cost.json` finds them by session, leaves them out of its `models_only` line and gives
the figure as read beside it. `2026-09-29-34b061` is Niadra's `host` path from the cell's own machine,
not combined:

```bash
uv run bench stack results/2026-09-29-9d0229 results/2026-09-29-40cdd3 results/2026-09-29-ba953f
uv run bench combine --references results/partial/2026-09-27-aaf0dd results/2026-09-29-5fc6f2 \
  results/2026-09-27-9e1e99 results/2026-09-27-1f0e6a results/2026-09-27-eed88a results/2026-09-27-d03ae2 \
  results/2026-09-27-64b5ae results/2026-09-27-320e9d results/2026-09-27-385a79 results/2026-09-27-63d738 \
  results/2026-09-27-a1ff8a results/2026-09-27-4cb045 results/2026-09-27-aa9155
```

Expected duration per repetition of dataset v2 (356 cases, about 2,330 conversations), not yet measured:
Niadra's seeding at the production cap about 23 minutes, then its settle; Mem0 about 1 h 15 (seeding both
scenarios, the accuracy pass, the timed loops); each added system from an hour to many hours (Graphiti,
whose server adds one message at a time with several model calls each, and LangMem, whose one worker takes
one conversation at a time; `--limit` makes a smaller run of either, and the page marks it).
`mem0_platform` needs a `MEM0_API_KEY` from a free Mem0 account, added to the harness's environment by
hand; it is not part of the default run.

### What the benchmark left on the cell

Runs until 25/09/2026 ran on the cell. `deploy/cell/cleanup.sh` lists what they may have left there (the
Kubernetes objects labelled `app.kubernetes.io/part-of=niadra-benchmarks`, the `bench-mem0` secret, Mem0's
databases `mem0_bench` and `mem0_bench_app` and the role `mem0_bench` on the RDS instance, the benchmark's
images in the node's containerd); `--confirm` removes them and lists again. Every script sent through
Systems Manager is written to a file and run with its standard input closed (`deploy/temp-host/lib.sh`,
`ssm_run`), and the cleanup's SQL goes in as `psql -c` arguments through `kubectl exec` to a pod that only
waits, so no command can read the rest of a script as its own input.

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

`bench ab` runs a baseline and a candidate over the same prepared dataset and reports the delta, figure by
figure. The candidate differs from the baseline by `--candidate-env KEY=VALUE` (repeatable), a setting of
the Niadra server under test, or by `--candidate-config <file.toml>`, whose sections override
`benchmark.toml`'s. A key only the candidate sets runs on the baseline at its default
(`NIADRA_SEMANTIC_CHANNEL=off`, `NIADRA_BENCH_GUARD_TYPES` empty; any other key needs `--baseline-env`),
so both sides say what they ran with. Everything else is equal: the same cases in the same order, seeded
from the same instant (the start of the hour the A/B began, `--now`), the same agent and judge, repetition
by repetition (baseline 1, candidate 1, baseline 2, ...). Both sides are graded on the same valid cases:
the two references of the validity rule answer once per repetition, on the baseline side, and their
verdict holds for both.

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
  a laptop). What a space learns across customers (the nightly retrieval weights) does not run here. The
  agent is `context` unless `--agent llm` (which needs `OPENROUTER_API_KEY`).
  `NIADRA_SEMANTIC_CHANNEL=models` gives the read path the hash encoder; `inprocess` needs the model
  files and is refused. Results go to `results/local/ab/`, which is not committed. These numbers compare
  two settings of the same code: they are never published, and no absolute figure of a local cell says
  what the region would measure. The customers' tag comes from `--now`, not from the A/B's id (every A/B
  starts fresh cells), so two A/Bs with the same `--now` seed the same customers.
- **A local cell with production's models** (`--local-cell <checkout> --real-models`): the same cell, but
  the extraction model and the decisions port are niadra-back's own adapters, `OpenRouterLlm` (GPT-6
  Luna at the effort `app/extract.py` asks for, `low`, with the prompts, schema and settings the region
  runs) and `JevDecider` (`typesafe/jev-1.13`, the questions and thresholds of `domain/decide`), built
  with their defaults, which are the region's (the deployment sets no provider order or base URL). The
  harness reads the benchmark's OpenRouter key from Secrets Manager by name (`niadra/bench/openrouter`,
  AWS profile `NIADRA_PROFILE`, default `niadra`, region `NIADRA_REGION`, default us-east-2) into
  `OPENROUTER_API_KEY`, unless that is already set; the agent and the judge use it too and stay as
  `benchmark.toml` sets them (`--agent llm` for graded answers). The key is never printed or written.
  What is still local: the models server (hash embeddings, regex PII), the fixed clock and the in-memory
  store; the space's daily AI ceiling is not enforced, and an extraction whose model call failed is
  retried at once, since the clock does not move. The rule extractor scored 97.7% (judge) on dataset v2
  where the region's first run with the real models scored 73.7%: this mode is how the team measures a
  write-path or read-path change against what the region runs, without the region. Its first run (27/09,
  dataset v2, one repetition, niadra-back f1c87c8, the commit the region measured) scored 76.9% on the
  region's 338 valid cases, per category within a few points of the region's repetition 1 except
  continuity (+10), recurrence (+12.5) and unanswerable (-12, judge only; the blocks held the answer
  equally often). Most of the gap is the extraction itself: Luna does not always give a conversation the
  same category, and a `billing` category is withheld at V1 by the starter policy, so the two runs held a
  different number of items back in 20 of the 33 cases whose block changed. Read a local delta against
  that spread. It cost US$ 0.58 for the cell's models on the first side, US$ 0.001 on the second (9,975
  of 9,996 calls from the cache), and US$ 0.12 for the agent and the judge over both sides.

  Every model call goes through a cache (`deploy/local/model_cache.py`): an answer is kept in
  `results/local/model-cache/` under a hash of the model, the prompt version (niadra-back's
  `EXTRACTOR_VERSION.PROMPT_VERSION`) and the exact request body, and a request with the same key is
  answered from disk. The cell's models see the same requests on both sides of an A/B that changes only
  the read path, and in a rerun with the same `--now` and checkout, so those cost nothing after the first
  run; anything that changes what a model reads (a prompt, the extraction schema, a write-path change
  that alters an earlier answer, another `--now`) asks again. A failed call is never kept.
  `--no-model-cache` asks the provider every time and reads or writes nothing. Each repetition records
  `model_spend` (the cell's calls, answers from the cache, failures, spend and what the cache saved, per
  model, and the agent's and judge's calls and cost, all from OpenRouter's `usage`), and `ab.md` adds a
  "Model spend" table. It also records the spend by purpose (Luna's extraction; each kind of Jev decision,
  `jev:triage` for the batch triage niadra-back asks from 28/09), counting every answer at what it cost when
  it was fetched, read from disk or not, so a fully cached side still shows what its models cost; and the
  exchanges the repetition wrote, so the "Model spend by purpose" table gives the cost per 1,000
  conversations of ten exchanges by the benchmark's method (spend over exchanges, times 10,000).

  niadra-back from 28/09 holds a closed session's extraction for a short window, so a customer's sessions
  that close together go to the models in one call (estudo 20, C4). The region seeds a customer's sessions a
  second or two apart, so there a customer's history is one batch; a local cell keeps the windows open while
  it seeds and closes them all when the settle starts (`POST /_bench/close-windows`), which is the same
  grouping. A checkout without the window runs as before.
- **The emulator** (`--mock`): niadra-mock on both sides. It has no server settings, so it runs only
  `--same`; the CI runs it.
- **The region** (neither option, from the temporary host): the Niadra of `NIADRA_BOOTSTRAP`, read and
  written within the production caps (config `[production]`). The harness changes no server setting there:
  each one, such as `NIADRA_SEMANTIC_CHANNEL`, is a setting of the read deployment's process, which also
  serves production, so `--candidate-env` is refused and the region runs `--same` or `--candidate-config`;
  an A/B of a server setting runs on a local cell until the region has a read deployment of the
  benchmark's own. (The one space setting the harness used to change, `memory_v2`, is gone: the memory has
  one behavior.) The two sides seed different customers (the same cases) into the same space, one after
  the other. `[ab] max_minutes` in `config/ab.toml` (450) stops an A/B before a repetition that would pass
  it, and the report says it is incomplete.

A local cell imports the checkout's code when it starts and records that commit. Point it at a worktree
that nothing else pulls during the A/B (`git worktree add ../wt/niadra-back-ab origin/main`, then `uv
sync` there): a checkout that moves mid-run leaves the cell with code from two commits.

```bash
# On a laptop, against a niadra-back checkout (its environment synced with `uv sync`):
uv run bench ab --local-cell ../../niadra-back --same --dataset v2
uv run bench ab --local-cell ../../niadra-back --candidate-env NIADRA_SEMANTIC_CHANNEL=models --dataset v2
# A branch against main: the candidate runs on its own checkout.
uv run bench ab --local-cell ../../niadra-back --candidate-cell ../../wt/niadra-back-mybranch \
    --dataset v2
# The same with production's models and graded answers; a fixed --now lets a rerun reuse the cache.
uv run bench ab --local-cell ../../wt/niadra-back-main --same --real-models --agent llm --dataset v2 \
    --repetitions 1 --metrics accuracy,tokens,privacy --now 2026-09-27T12:00:00+00:00

# In the region, from the temporary host (see "Running it on the temporary host"; `collect` copies
# results/ab/<date>-<id>/ too):
deploy/temp-host/bench.sh ab --same --dataset v2
deploy/temp-host/bench.sh ab --candidate-config <file.toml> --dataset v2
```

## The typed-object set: what the blocks of `include` do to an answer

Dataset v2 has no typed object and never asks for the blocks a read adds with `include` (the subject's typed
state and the constraints they declared), so every A/B on it only shows that a read without them did not
change. `dataset/typed/` (`bench prepare --dataset typed`, 42 cases, 21 per language) asks questions whose
answer depends on those blocks, one agent per sector, synthetic data only:

| Category | Sector | Before the question | Right answer |
|---|---|---|---|
| `price_freshness` | retail | the checkout computed the cart's total 30 to 90 minutes ago | not confirming it as current (older than the type lets a price be affirmed) |
| `quote_expiry` | retail | the customer changed the delivery postal code after a shipping quote | the quote no longer holds |
| `deadline_revision` | legal | the court's calendar moved a deadline the agent had already given | the revised deadline |
| `not_checked` | legal | nobody checked whether a notice is addressed to the client | not checked: never "no" |
| `changes_since_seen` | health plan sales | a plan's price moved after the customer was shown it | the new price |
| `hard_constraint` | health plan sales | the customer set a required filter in another agent's simulator | the option that meets it |
| `effect_once` | retail | the closing agent already sent the farewell with the survey | already sent, not again |

Each case is a script of writes (conversations, system events about objects of the types in
`config/typed.object-types.json`, an agent's turn record with a tool's observation, an exposure or a
preference, a platform push, a coordination effect) and one question. The object types follow the server's
sector templates, cut to the fields the cases write. Every case carries one value of a `pii` field
(`sensitive`) that no read at V0 may hand the agent. The ground truth is checked twice: by the judge, with the
case's own rule next to the reference answer (`TYPED_JUDGE_PROMPT` in `agent.py`), and by an exact check with
no model (`expect.all_of`/`none_of`, whole tokens; for a hedge, an expiry, a check nobody made or an effect
already done, the words that say it, in both languages). The validity rule is v2's: a case counts where the
agent is right with the whole history and wrong with no memory; both references answer once per case, and the
report gives every case and the valid ones.

`bench typed` runs it on a local cell of niadra-back, with production's models (GPT-6 Luna extracts, Jev
decides) and the benchmark's agent and judge:

```bash
# Under the machine's e2e lock: a cell of niadra-back main, kept running.
LOCAL_E2E_PORT=20300 LOCAL_E2E_DIR=$TMPDIR/niadra-local-e2e-bench \
    ../../niadra-infra/scripts/local-e2e.sh ../../wt/niadra-back-main --keep
# The SDK must be this repository's source: the pinned release does not place the blocks in the turn block.
PYTHONPATH=../src uv run bench typed --api http://127.0.0.1:20309 --control http://127.0.0.1:20300 \
    --cell-dir $TMPDIR/niadra-local-e2e-bench --cell-pg "host=127.0.0.1 port=20310 user=postgres" \
    --v2-sample 20 [--limit 5]
```

It turns on the agent features and declares the typed set's types in the sandbox space with the bootstrap's
admin account (the space keeps its language and time zone, as for dataset v2), seeds every case with a new
customer, waits until each case's objects hold what its question is about and no customer's context moves,
and then asks every question twice for the same customer: `without` (a plain read) and `with` (`include:
["state", "constraints"]`, which the SDK places in the turn block inside its `<niadra>` section). It reports,
per side and category (`results/typed/<date>-<id>/typed.json` and `typed.md`, every answer and memory block in
`cases.jsonl`):

- the judge's and the exact check's accuracy, and the cases that changed verdict;
- tokens per turn of the memory block, what the blocks add, and what the same data weighs as a tool's JSON
  result (what an agent without the blocks would fetch, in a round trip that sends the prompt again);
- with `--v2-sample N`, N cases of dataset v2 read with no block in both views, against today's medians (voice
  89, chat 98; a turn that asks for no block must stay within 5%);
- the claim guard's verdicts and acts on every answer (the SDK's `guard_text`, a mutable chat output, with the
  sector's example contract from `spec/examples/claim-contract/`, the case's language first; on the `with` side
  the fields the state read served are the turn's evidence, as `context()` records them in a turn), and how
  often it acted on an answer the judge graded correct;
- reads at V0 (no proof) on both sides whose block held the case's sensitive value;
- for the effect cases, whether the coordination check refuses a second attempt;
- the agent's and the judge's spend (OpenRouter's `usage`) and, with `--cell-pg`, the cell's own model spend
  over the run (`cell-cost.json`: its spend ledger before and after, and its extraction runs).

A local cell's numbers compare two reads of the same code; they are never published.

The first run (`results/typed/2026-09-29-28409a`, 29/09/2026: niadra-back 71529d7 on a local cell started by
`local-e2e.sh`, whose own check had turned on its other features too; the SDK source at d10af9c; two
repetitions, 84 answers per side) read, per side:

- **Judge, every case:** 32.1% without the blocks, 63.1% with them. By category: price freshness 33.3 to
  83.3%, quote expiry 0 to 91.7%, revised deadline 83.3 to 91.7%, not checked 75.0 to 83.3%, changes since seen
  0 to 8.3%, hard constraint 0 to 25.0%, effect once 33.3 to 58.3%. On the 53 answers the validity rule keeps:
  26.4 to 50.9%. The exact check: 45.2 to 65.5%.
- **Tokens:** the blocks add a median of 43 tokens to a voice turn and 50 to a chat turn (p95 56 and 165; a
  court notice's content in its envelope is the large one). The same data as a tool's JSON is 645 and 780
  tokens. The 20 cases of dataset v2 read with no block: voice 92, chat 94.5 (today 89 and 98), within 5%.
- **Claim guard:** it acted on 31 answers without the blocks (5 of them right) and on 34 with them (27 of them
  right). A deadline the state block gave is `unsupported`, because a type's computed values (`values`) are
  not evidence for the guard, only its fields; a hedged answer that names the earlier total as earlier is
  `stale`; a price the customer or the earlier conversation stated is `unsupported`.
- **V0:** no read at V0 on either side held a case's sensitive value; at V1, 42 of 84 reads on each side held
  it, from the system event that wrote the `pii` field, in the pack's system line.
- **What to read with care:** the agent is not deterministic even with its seed (3 of the 12 effect cases,
  where both sides read the same bytes, changed verdict); the voice cases of `changes_since_seen` answer "I must
  verify your identity first" on both sides, because the pack holds a recorded value back until the identity is
  verified and tells the agent to ask for it, while the state block shows the new price; the constraint lines (`health_plan.copay: não`) name an
  attribute the pack's summary of the offers does not carry, so the agent rarely ties them to an option.
- **Cost:** US$ 0.031 for the agent and the judge (672 calls) and US$ 0.020 for the cell's models (its ledger).

## Ranking gate

No change to how the memory picks and orders what a pack and its slots carry enters niadra-back's `main` without its
delta attached to the pull request: `domain/serve/slots.py`, `domain/compile/scoring.py`,
`domain/measure/weights.py`, `DEFAULT_WEIGHTS`, and any flag that turns a retrieval channel on. The delta
is `bench ab` with the change as the candidate (a flag, or the branch's checkout as `--candidate-cell`
against `main`'s as `--local-cell`), on dataset v2, three repetitions, with `bench ab --same` passing on
the same checkout first; the PR carries `ab.md`. A category that loses accuracy or `context_has_answer`
beyond the range of its repetitions blocks the change, as do median tokens that rise without an accuracy
gain. Turning the semantic channel on by default also needs the region's A/B, committed
under `results/ab/`, and the decision cites that file.

## Publishing

1. Put the runs of one campaign together with `bench combine` (one folder, the site's schema, with the
   list of runs in `combined_from`; a system measured in separate runs is stacked first with `bench stack`) and commit `results/<date>-<id>/` here (summary, repetitions, and the
   per-case rows that let anyone audit every grade, each with the memory block the agent received since
   26/09/2026), with the runs it came from.
2. In niadra-frontend: `node scripts/import-benchmark.mjs ../niadra-sdk-python/benchmarks/results/<date>-<id>/summary.json`.
   The importer refuses anything but a `region` run. Each added system appears as a new `system` key in
   every metric; Niadra's paths are now `edge` and `vpc`, every other system's `host`; privacy lines carry
   `verification`; the environment adds `harness_host` and `harness_machine_class`; `metrics.backing` is
   the new line of values without a source per thousand answers; `config.niadra_guards` says whether guard
   lines were measured.
3. Where Niadra loses on a metric, add one line with the most likely reason to `site_notes` in the
   imported file (`{"pt": {"latency": "..."}, "en": {...}}`); the page prints it under that chart.

## Local checks and a dry run without keys

```bash
uv sync
uv run pytest -q                       # unit tests, dry runs against niadra-mock and fakes, the deploy scripts
uv run bench prepare --check           # every committed dataset is what the generator makes, and valid
uv run bench run --mock --systems niadra --quick --limit 28 --repetitions 1 --output /tmp/bench   # no network
uv run bench ab --same --mock --quick --limit 28 --repetitions 1                    # the A/B plumbing
deploy/local/run.sh                    # every system, one at a time, on Docker with fakes; then combined
deploy/local/run.sh hindsight -- --dataset v2 --quick --limit 14 --repetitions 1
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
  `search()` for Mem0 and one read for every added system. The reads that reason with a model (Hindsight's
  `reflect`, Honcho's dialectic, Cognee's graph completion) are measured as what they are: one read, whose
  latency, cost and tokens include the model call.
- E-mail and app sessions are written with the WhatsApp source's key and their own `channel`, because
  the sandbox has no e-mail or app source; the memory records the event's channel, so the packs are the
  same, but a company would give each channel its own source and key.
- Niadra's freshness read verifies the voice call at V1 before the clock starts, as every accuracy probe
  does: an unproven call reads at V0, where the starter policy withholds order numbers.
- The third Mem0 column of the plan (its own defaults, `gpt-4o-mini` and `text-embedding-3-small`)
  needs an OpenAI embeddings key and is not in the default run.
- Five systems have no setting for a reasoning effort (Graphiti for a model name its client does not know,
  Memobase, Supermemory, MemOS, Redis Agent Memory Server): their gateway asks each of their calls for
  `low`, which is the only change the harness makes to what a system sends, and its counters say how many
  calls it changed (`reasoning_set`).
- A reasoning model's thinking counts toward the output limit a system sets for a call. A system whose
  limit is small for a reasoning model (Memobase asks for 1,024 tokens by default) can get a cut answer; the
  gateway counts them (`truncated`), and none of those limits is changed.
- Niadra is read across a network (its public address, or its private address in the VPC through the
  cell's ingress); every other system answers on the harness's own host. Their latency lines have no
  network in them; Niadra's do.
- The embedding line `encode` times the copy of the embedder on the harness's host, which Mem0 and the
  added systems call; Niadra's read service calls the cell's own copy, which that line does not time.
- Niadra's history navigation and ingestion run at 10 per second only (the production cap); the other
  systems at 10 and 25. The 25 per second lines have no Niadra counterpart.
- The added systems' ingestion lines use each system's documented write for a live exchange: an
  asynchronous one where the system has it (Graphiti's queue, Hindsight's `async` retain, Supermemory's
  queued document, MemOS's `async` add, Redis Agent Memory Server's working memory,
  Honcho's message batch, the LangMem service's queue, Cognee's `remember` in the background), so like
  Niadra's they answer before the memory is built; Memobase's insert goes to its buffer. Mem0's open source
  server has no such mode.
- Runs of different systems are put together by `bench combine`, with one validity rule for all (the
  first run's references, or those of the folder `--references` names); each system still ran alone on the host, at a different time, against the
  same dataset and configuration hash.
- A costly or slow system may run fewer repetitions or fewer cases (`--repetitions 1`, `--limit`) than the
  first run, and the first run may run fewer repetitions than another system: `bench combine` takes every
  run after the first, its source in `combined_from` and its line in `per_system` carry its `repetitions`
  and `cases`, and `accuracy_shared` scores every system on the cases all of them answered, so the page
  marks the difference rather than setting unequal runs side by side as equal.

