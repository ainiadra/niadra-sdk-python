# Niadra benchmark

An open, reproducible benchmark of Niadra against Mem0, the memory layer most teams compare it with.
It measures what an engineer integrating a memory for customer-facing and internal agents needs to
know, with the same models on both sides, and publishes every result file. Where Niadra loses, the
number is published the same way.

Nothing here is a claim until it is measured inside the cloud region. Numbers from a laptop, from the
emulator or from a smoke run are never published; the site only imports result files whose
environment is `region`.

## What it measures

| # | Metric | Niadra | Mem0 |
|---|---|---|---|
| 1 | Latency of the context before the model call (p50, p95, p99), open loop at 10 and 25 reads per second for 30 s over 20 conversations | `POST /v1/context` with the conversation id, from inside the cluster and through the public TLS address | `POST /search` on its own REST server, `top_k` 10, `threshold` 0.1, from inside the cluster |
| 2 | Tokens the memory adds to the prompt per turn (`o200k_base`) | `system_block` and `turn_block`, `voice` and `chat` views | the search results in the format of Mem0's examples ("Based on previous conversations, I recall:") |
| 3 | Cost of the memory layer per thousand conversations of 10 exchanges | public price, both ends ($5 and $15), models included | open source: extraction model spend measured from the provider's usage numbers (servers not priced); Platform: public plans over their quotas |
| 4 | Cross-channel continuity accuracy on the synthetic dataset | the agent answers from Niadra's context | the same agent answers from Mem0's results |
| 5 | Privacy: sensitive value handed to an unverified (V0) conversation | counted in the memory block | counted in the memory block |
| 6 | Freshness: a WhatsApp message until the voice agent reads it | `track()` until `context(view="voice")` shows it | `add()` until `search()` shows it |
| 7 | Memory slow (2 s) or down (503) behind the same fault proxy | the SDK as it ships | an HTTP client at its defaults with `raise_for_status()` |
| 8 | History navigation (p50, p95, p99), open loop at 10 and 25 calls per second for 30 s over 20 seeded customers | `POST /v1/history/search` and `POST /v1/history/open`, from inside the cluster and through the public TLS address | `POST /search` and `GET /memories/{memory_id}`, from inside the cluster |
| 9 | Ingestion acknowledgement (p50, p95, p99), open loop at 10 and 25 writes per second for 30 s over 20 seeded customers | `POST /v1/batch` with one exchange until its `200`, from inside the cluster and through the public TLS address | `POST /memories` with the same exchange until its `200`, with `infer` (its default) and with `infer=False`, from inside the cluster |

Metrics 8 and 9 are timed exactly as metric 1: the same open loop (`latency.open_loop`: constant rate
whatever the answers do, one warm-up call per customer, the same HTTP client and limits), each line
alone, three repetitions, the median and the range across them. Every call that did not get the answer
the caller waits for is an error, counted by kind (`error_kinds`: an HTTP status, a timeout); a search
that answered with nothing, or Niadra's `text_only` search when its encoder did not answer in time,
stays in the percentiles and is counted apart (`empty`, `degraded`). They run after the other metrics
of each repetition, so metrics 1 to 7 are measured under the same conditions as in earlier runs.

Two references run through the same agent for every accuracy number: no memory at all, and the whole
raw history pasted into the prompt.

We do not run LoCoMo, LongMemEval or BEAM: they are long personal conversation sets and do not measure
what a Niadra buyer buys.

## How the comparison is kept fair

- **Same place.** Everything runs as pods of the cell's k3s cluster in us-east-2 (m7i-flex.large, RDS
  db.t4g.micro). Mem0 is its own REST server (`server/` of github.com/mem0ai/mem0 at tag v2.2.0) with
  `mem0ai[nlp]==2.2.0` and the spaCy model its README asks for, pgvector in its own database on the
  same RDS instance. Systems are measured one after the other, never at the same time.
- **Same models.** Both extract with `google/gemini-2.5-flash-lite` through OpenRouter. Mem0's
  embedder is the cell's own embedding server (`niadra-models`, `paraphrase-multilingual-MiniLM-L12-v2`,
  384 dimensions) through a small OpenAI-compatible proxy (`bench serve embed-proxy`). One agent
  (`openai/gpt-4.1-mini`, temperature 0, fixed prompt in `src/niadra_bench/agent.py`) answers every
  probe, and one judge with a published rubric grades it; an exact check that looks for the expected
  value in the answer is reported beside it.
- **Mem0 as documented.** One `add()` per exchange with both messages (the README pattern; the v3
  extraction reads both roles in one call), system records as raw memories (`infer=False`), one
  `search()` per turn. No parameter tuned for this dataset. The rerank column (`mem0_oss_rerank`)
  builds `mem0.Memory` in-process on the same store with an LLM reranker on the same model, because
  the REST server's `/search` has no rerank parameter.
- **Identity in two scenarios.** Mem0 does not resolve identity, so it runs with the same user id on
  every channel (`known_id`, its best case) and with each channel's own id (`per_channel_id`). Niadra
  receives the same handles in both.
- **Fresh customers.** Every repetition seeds new phone numbers, e-mails and ids, so extraction runs
  again and no memory has seen them. Each metric runs three times; the site shows the median and the
  range.
- **Frozen inputs.** `config/benchmark.toml`, `config/mem0.config.json` and `dataset/cases.jsonl` are
  committed; every result file carries their hashes, the harness commit, the package versions and the
  deployed Niadra server version.

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
- **Paths.** Inside the cluster the calls go to each service's own address: `NIADRA_CLUSTER_URL`
  (the `read` service) for navigation and `NIADRA_CLUSTER_INGEST_URL` (the `ingest` service) for the
  acknowledgement; through the public TLS address they go where the SDK sends them.

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

## Layout

```
config/                 frozen configuration and Mem0's configuration
dataset/                the committed dataset and its manifest
src/niadra_bench/       the harness (`bench` command)
tests/                  unit tests, and a dry run against niadra-mock
deploy/Dockerfile       the harness image
deploy/mem0/Dockerfile  Mem0's REST server at v2.2.0
deploy/k8s/             secrets, Mem0's databases, Mem0 and the proxies, the run Job
deploy/run-in-region.sh builds, deploys, runs and collects on the k3s machine
deploy/local/           the same pipeline on Docker with fakes, for a smoke run
results/                published runs: results/<date>-<id>/{summary.json,rep-N.json,cases-repN.jsonl}
```

## Running it in the region

Requires the AWS session of the account that runs the cell (`aws login --profile niadra`), from a
checkout of niadra-infra (for its `scripts/lib.sh`). The script runs on the k3s machine through
Systems Manager, as the infra scripts do; it builds both images there from this repository and imports
them into k3s (nothing is pushed to a registry).

Secrets it reads, by name only:

- `niadra/platform/openrouter` in AWS Secrets Manager (property `api_key`), through the cluster's
  `ClusterSecretStore` `niadra`, into the Kubernetes secret `bench-openrouter`;
- `niadra-sandbox-bootstrap` in the `niadra` namespace (the sandbox tenant's source keys), mounted
  read-only into the run pod;
- `niadra-db-init` (the database master user) and the `niadra-cell` config map, only in the job that
  creates Mem0's databases;
- `bench-mem0`, created by the script with random values: Mem0's database password, its admin API key
  and its JWT secret.

```bash
cd niadra-infra && source scripts/lib.sh
RUN=https://raw.githubusercontent.com/ainiadra/niadra-sdk-python/main/benchmarks/deploy/run-in-region.sh
bench() { printf 'curl -fsSL %s | bash -s -- %s\n' "$RUN" "$*" > /tmp/bench.sh; }

# 1. Build both images on the machine, import them into k3s, create Mem0's databases, start Mem0,
#    the embedding proxy and the meter (about 10 minutes the first time).
bench up && on_ops /tmp/bench.sh 1800

# 2. A smoke run first: 14 cases, one repetition, short latency (about 10 minutes, under $1).
bench start run --systems niadra,mem0_oss,mem0_oss_rerank --limit 14 --repetitions 1 --quick && on_ops /tmp/bench.sh 300
bench status && on_ops /tmp/bench.sh 120        # repeat until the Job is Complete
bench collect && ops_query /tmp/bench.sh        # read the summary: every metric present, no errors

# 3. The full run: every case, three repetitions (duration and cost below).
bench start && on_ops /tmp/bench.sh 300
bench status && on_ops /tmp/bench.sh 120        # repeat until the Job is Complete

# 4. Collect: copies the run folder to s3://<cell bucket>/benchmarks/<date>-<id>/ and prints the summary.
bench collect && ops_query /tmp/bench.sh
aws s3 cp --recursive s3://<cell bucket>/benchmarks/<date>-<id>/ ../niadra-sdk-python/benchmarks/results/<date>-<id>/

# 5. When done: remove every benchmark object and Mem0's databases.
bench down --drop-db && on_ops /tmp/bench.sh 600
```

`start` takes any `bench run` arguments. Without arguments it runs every metric on `niadra`, `mem0_oss`
and `mem0_oss_rerank`. `mem0_platform` needs a `MEM0_API_KEY` from a free Mem0 account, added to the
Job by hand; it is not part of the default run. `BENCH_REF` picks another branch or tag of this
repository.

Expected duration and cost of the full run (240 cases, three repetitions), from the smoke run's call
counts and OpenRouter's public prices on 2026-09-24:

| Step, per repetition | Time | OpenRouter spend |
|---|---|---|
| Seed Niadra and wait for its extraction to settle | 10 to 20 min | about $0.35 (Niadra's own extraction) |
| Seed Mem0, both scenarios (about 1,500 `add()` each, about 8,600 prompt tokens per call) | 15 to 20 min | about $2.70 |
| Accuracy pass: 7 systems x 240 cases, agent and judge, rerank searches | 25 to 30 min | about $1.50 |
| Latency, freshness, resilience | about 8 min | none |
| History navigation and ingestion acknowledgement (about 1,100 `add()` with `infer` on Mem0; Niadra extracts the conversations the writes open, in the background) | 13 to 16 min | about $2 (almost all Mem0's `add_infer`) |
| **Total per repetition** | **about 1 h 15 to 1 h 35** | **about $6.50** |

Three repetitions: about 4 h to 4 h 45 and about $20 (budget $30 for retries). No AWS resource is
created beyond Kubernetes objects and two small databases on the existing RDS instance; the machine
and the database are the ones already running.

## Publishing

1. Commit `results/<date>-<id>/` here (summary, repetitions, and the per-case rows that let anyone
   audit every grade).
2. In niadra-frontend: `node scripts/import-benchmark.mjs ../niadra-sdk-python/benchmarks/results/<date>-<id>/summary.json`.
   The importer refuses anything but a `region` run. The page `/benchmark` then shows the numbers,
   and the prerender puts it in the sitemap and llms.txt. The menu entry (Recursos) and the footer
   link are added by hand once the owner has read the results.
3. Where Niadra loses on a metric, add one line with the most likely reason to `site_notes` in the
   imported file (`{"pt": {"latency": "..."}, "en": {...}}`); the page prints it under that chart.

## Local checks and a smoke run without keys

```bash
uv sync
uv run pytest -q                       # unit tests and a dry run against niadra-mock
uv run bench prepare --check           # the committed dataset is what the generator makes, and valid
uv run bench run --mock --quick --limit 28 --repetitions 1 --output /tmp/bench   # Niadra only, no network
docker compose -f deploy/local/compose.yaml up --build --exit-code-from harness harness
```

The compose file runs the whole pipeline (Mem0's real server, pgvector, the proxies, niadra-mock and
every metric) with fake LLM and embedding servers, so it needs no key. It proves the plumbing only.

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
- The sandbox tenant's billing source (the one key with the `act` scope) declares a closed list of
  operations, `credit` only, so Niadra refuses the agent actions of the dataset that record `refund`,
  `refund_fee`, `reimburse` or `redeliver` (26 of the 32 promise cases). The harness writes the rest of
  each history, including the system event that confirms the action, and lists every refused action
  under `seed.niadra.refused_actions` of each repetition. Mem0 receives those actions as raw memories.
- Niadra's freshness read verifies the voice call at V1 before the clock starts, as every accuracy probe
  does: an unproven call reads at V0, where the starter policy withholds order numbers.
- The third Mem0 column of the plan (its own defaults, `gpt-4o-mini` and `text-embedding-3-small`)
  needs an OpenAI embeddings key and is not in the default run.
