"""Metric 3: what the memory layer costs per thousand conversations of `turns_per_conversation`
exchanges, with public prices (config [prices]) and the usage the run measured.

- Niadra: its public price, both ends of the range; the models it calls are included in it.
- Mem0 open source: the extraction model calls its add() made during seeding, counted by the LLM meter
  from the provider's usage numbers, divided by the add() calls that extracted; one add() per exchange.
  The rerank variant adds the reranker's model calls per search. Embeddings run on the same self-hosted
  model as Niadra's and the servers and database are not priced: this is the model spend only.
- Mem0 Platform: each monthly plan divided by the conversations its add() and search() quotas allow,
  with one add() and one search() per exchange.
- For every system, separately: what the injected memory block costs in the agent's own model (median
  tokens per turn x turns x the agent model's input price).
"""

from __future__ import annotations

from typing import Any

from niadra_bench.config import BenchConfig


def model_usd(config: BenchConfig, usage: dict[str, dict[str, int]]) -> float:
    total = 0.0
    for model, counts in usage.items():
        price_in, price_out = config.prices.model_price(model)
        total += (
            counts.get("prompt_tokens", 0) / 1e6 * price_in
            + counts.get("completion_tokens", 0) / 1e6 * price_out
        )
    return total


def meter_delta(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, dict[str, int]]:
    if not before or not after:
        return {}
    out: dict[str, dict[str, int]] = {}
    for model, counts in after.get("models", {}).items():
        prior = before.get("models", {}).get(model, {})
        out[model] = {k: int(v) - int(prior.get(k, 0)) for k, v in counts.items()}
    return out


def compute(
    config: BenchConfig,
    *,
    tokens_per_turn: dict[str, float | None],
    mem0_seed_usage: dict[str, dict[str, int]],
    mem0_infer_adds: int,
    rerank_usage: dict[str, dict[str, int]],
    rerank_searches: int,
    platform_measured: bool,
) -> list[dict[str, Any]]:
    turns = config.cost.turns_per_conversation
    per_thousand = turns * 1000
    agent_in, _ = config.prices.model_price(config.agent.model)

    def agent_prompt(system: str) -> float | None:
        tokens = tokens_per_turn.get(system)
        return None if tokens is None else round(tokens * per_thousand / 1e6 * agent_in, 4)

    rows: list[dict[str, Any]] = [
        {
            "system": "niadra",
            "variant": "price_low",
            "memory_usd_per_1000": config.prices.niadra.low,
            "agent_prompt_usd_per_1000": agent_prompt("niadra"),
            "basis": "public price, models included",
        },
        {
            "system": "niadra",
            "variant": "price_high",
            "memory_usd_per_1000": config.prices.niadra.high,
            "agent_prompt_usd_per_1000": agent_prompt("niadra"),
            "basis": "public price, models included",
        },
    ]
    if mem0_infer_adds:
        per_add = model_usd(config, mem0_seed_usage) / mem0_infer_adds
        rows.append(
            {
                "system": "mem0_oss",
                "variant": "models_only",
                "memory_usd_per_1000": round(per_add * per_thousand, 4),
                "agent_prompt_usd_per_1000": agent_prompt("mem0_oss"),
                "basis": "measured extraction model spend, servers not priced",
                "usd_per_add": round(per_add, 8),
            }
        )
        if rerank_searches:
            per_search = model_usd(config, rerank_usage) / rerank_searches
            rows.append(
                {
                    "system": "mem0_oss_rerank",
                    "variant": "models_only",
                    "memory_usd_per_1000": round((per_add + per_search) * per_thousand, 4),
                    "agent_prompt_usd_per_1000": agent_prompt("mem0_oss_rerank"),
                    "basis": "measured extraction and rerank model spend, servers not priced",
                    "usd_per_search": round(per_search, 8),
                }
            )
    for plan, (monthly, adds, searches) in config.prices.mem0_plans().items():
        capacity = min(adds // turns, searches // turns)
        rows.append(
            {
                "system": "mem0_platform",
                "variant": plan,
                "memory_usd_per_1000": round(monthly / capacity * 1000, 4) if capacity else None,
                "agent_prompt_usd_per_1000": agent_prompt("mem0_platform") if platform_measured else None,
                "basis": "public plan price over its monthly quota",
                "monthly_usd": monthly,
                "conversations_per_month": capacity,
            }
        )
    return rows
