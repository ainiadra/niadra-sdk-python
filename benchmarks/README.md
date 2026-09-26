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
| 8 | History navigation (p50, p95, p99), open loop at 10 and 25 calls per second for 30 s over 20 seeded customers | `POST /v1/history/search` and `POST /v1/history/open`, from inside the cluster and through the public TLS address | `POST /search` and `GET /memories/{memory_id}`, from inside the cluster; both searches' question encoding alone as its own line (`encode`) |
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
- **Identity in two scenarios.** Mem0 does not resolve identity, so it runs with the same user id on
  every channel (`known_id`, its best case) and with each channel's own id (`per_channel_id`). Niadra
  receives the same handles in both.
- **Fresh customers.** Every repetition seeds new phone numbers, e-mails and ids, so extraction runs
  again and no memory has seen them. Each metric runs three times; the site shows the median and the
  range.
- **Frozen inputs.** `config/benchmark.toml`, `config/mem0.config.json` and `dataset/cases.jsonl` are
  committed; every result file carries their hashes, the harness commit, the package versions and the
  deployed Niadra server version. Dataset v2 adds `config/dataset.v2.toml` and `dataset/v2/cases.jsonl`;
  a v2 run's configuration hash also covers the v2 settings, and a v1 run's hash is computed exactly as
  before v2 existed.

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

Both searches start by encoding the question on the same embedding server (Niadra's read service calls
`niadra-models`; Mem0 calls it through the embedding proxy). So that a search's time can be read without
that step, metric 8 has a third line, `encode`: `POST /v1/embed` on `NIADRA_MODELS_URL` with the same
probe questions, at the same rates, from inside the cluster (system `embedder`, since both searches pay
it). Niadra's search line also keeps the steps its server names in `Server-Timing` (`server_timing`,
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
long and cost about twice as much (below).

## Layout

```
config/                 frozen configuration and Mem0's configuration
dataset/                the committed dataset v1 and its manifest; dataset/v2/, the same for v2
src/niadra_bench/       the harness (`bench` command)
tests/                  unit tests, and a dry run against niadra-mock
deploy/Dockerfile       the harness image
deploy/mem0/Dockerfile  Mem0's REST server at v2.2.0
deploy/k8s/             secrets, Mem0's databases, Mem0 and the proxies, the run Job
deploy/run-in-region.sh builds, deploys, runs and collects on the k3s machine
deploy/local/           the same pipeline on Docker with fakes, for a smoke run
results/                published runs: results/<date>-<id>/{summary.json,rep-N.json,cases-repN.jsonl}
results/ab/             A/B runs in the region: results/ab/<date>-<id>/{ab.json,ab.md,baseline/,candidate/}
results/local/          A/B runs on a local cell or the emulator (not committed)
config/ab.toml          `bench ab`'s ceiling, apart from benchmark.toml so the published hash stays
deploy/local/cell_server.py  the local cell `bench ab --local-cell` runs in a niadra-back checkout
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
and `mem0_oss_rerank`, on dataset v1.

Dataset v2, and Niadra's `memory_v2` space flag (the read path of memory v2 ships behind it, off by
default). `--niadra-memory-v2 on|off` sets the flag for the run through the control API, with the
bootstrap's admin account, as a configuration diff the same person approves, before seeding; it puts the
previous value back when the run ends. The flag lives at `settings/memory_v2` (`<document type>/<dotted
field>`); `NIADRA_MEMORY_V2_FLAG` overrides that. Without the option the space is left as it is. Two runs
with different flag values must not overlap in the same space. `summary.json` records the choice
(`config.niadra_memory_v2`: `on`, `off` or `unchanged`) and the dataset (`dataset.version`).

```bash
bench start run --dataset v2 --niadra-memory-v2 off && on_ops /tmp/bench.sh 300    # memory v2 off
bench start run --dataset v2 --niadra-memory-v2 on && on_ops /tmp/bench.sh 300     # then on, same cases
```

Memory requests of the benchmark's pods are what each one held under the benchmark on 25/09/2026, so a
run fits beside production with monitoring on: the harness 352 MiB (it held 333, peak 383), Mem0's
server 384 MiB (379, peak 424), the embedding proxy 48 MiB (44, peak 54), the meter 64 MiB (49, peak
52). Limits stay above the peaks with room: 1 GiB for the harness and Mem0 (dataset v2's longer customers
make the harness's rows and Mem0's prompts larger), 128 MiB for the proxy and the meter. A pod past its
request is the first the node evicts under pressure, never a production pod. `mem0_platform` needs a `MEM0_API_KEY` from a free Mem0 account, added to the
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

Since niadra-back bb4ecaa (2026-09-25) the extraction gate sends to the model the short exchanges that
settle a value, so Niadra extracts about 980 of the 986 conversation sessions of a repetition instead of
about 174, several times the spend above. Before a run, make sure the sandbox space's daily extraction
ceiling covers three repetitions (it is $0.50 a day unless the tenant's contract sets
`llm_daily_micros:extraction`; the operator sets it with `PUT /v1/quotas`). Past the ceiling, extraction
waits for the next day and the settle step sees nothing change, so the accuracy pass would read
half-built memory. A prompt change also re-extracts up to 1,000 recent sessions per space after the
deploy, from the same ceiling.

Three repetitions: about 4 h to 4 h 45 and about $20 (budget $30 for retries).

Dataset v2 (356 cases, about 2,330 conversations per repetition instead of 986), estimated from the table
above by the number of conversations and cases, not yet measured: seeding and settling Niadra 20 to 40
min, seeding Mem0 35 to 45 min and about $6.20, the accuracy pass 35 to 45 min and about $2.20, the rest
unchanged. About 2 h to 2 h 30 and about $11 per repetition; three repetitions, 6 to 7 h 30 and about $33
(budget $45). The sandbox's daily extraction ceiling must cover about 7,000 sessions (three repetitions). No AWS resource is
created beyond Kubernetes objects and two small databases on the existing RDS instance; the machine
and the database are the ones already running.

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
- **The region** (neither option, from the run Job): the Niadra of `NIADRA_BOOTSTRAP`. The harness
  changes only what the bootstrap's admin account changes through the control API, the space's settings:
  `NIADRA_MEMORY_V2` (as `--niadra-memory-v2` does, put back at the end of each side's repetition). A
  setting of the read deployment's process, such as `NIADRA_SEMANTIC_CHANNEL`, is refused there: that
  deployment also serves production; that A/B runs on a local cell until the region has a read deployment
  of the benchmark's own. The two sides seed different customers (the same cases) into the same space,
  one after the other. `[ab] max_minutes` in `config/ab.toml` (450) stops an A/B before a repetition that
  would pass it, under the Job's 8 hours, and the report says it is incomplete.

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

# In the region (see "Running it in the region"; `collect` copies results/ab/<date>-<id>/):
bench start ab --same --dataset v2 && on_ops /tmp/bench.sh 300
bench start ab --candidate-env NIADRA_MEMORY_V2=on --dataset v2 && on_ops /tmp/bench.sh 300
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
uv run bench prepare --check           # both committed datasets are what the generator makes, and valid
uv run bench run --mock --systems niadra --quick --limit 28 --repetitions 1 --output /tmp/bench   # no network
uv run bench run --mock --systems niadra --dataset v2 --quick --limit 72 --repetitions 1 --output /tmp/bench
uv run bench ab --same --mock --quick --limit 28 --repetitions 1                    # the A/B plumbing
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
- The billing agent's source is created with the bootstrap's admin account only when the run pod has
  `NIADRA_CONTROL_URL` (the region Job sets it). Without it (a local run, niadra-mock) the actions go
  through the sandbox's starter billing source; any refused action is still listed under
  `seed.niadra.refused_actions`, and `seed.niadra.declared_operations` says which setup the run used.
- The agent answers from one read, with no tools. Niadra's voice guide also gives the agent
  `search_customer_history` for older matters; the benchmark measures the pack alone, as it measures one
  `search()` for Mem0.
- E-mail and app sessions are written with the WhatsApp source's key and their own `channel`, because
  the sandbox has no e-mail or app source; the memory records the event's channel, so the packs are the
  same, but a company would give each channel its own source and key.
- Niadra's freshness read verifies the voice call at V1 before the clock starts, as every accuracy probe
  does: an unproven call reads at V0, where the starter policy withholds order numbers.
- The third Mem0 column of the plan (its own defaults, `gpt-4o-mini` and `text-embedding-3-small`)
  needs an OpenAI embeddings key and is not in the default run.
