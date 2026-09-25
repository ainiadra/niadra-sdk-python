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
| **Total per repetition** | **about 1 h to 1 h 20** | **about $4.50** |

Three repetitions: about 3 h 30 to 4 h and about $15 (budget $25 for retries). No AWS resource is
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
- The rerank column is measured in-process, so it has accuracy, tokens and cost but no latency line.
- The third Mem0 column of the plan (its own defaults, `gpt-4o-mini` and `text-embedding-3-small`)
  needs an OpenAI embeddings key and is not in the default run.
