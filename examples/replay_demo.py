"""A deterministic agent to replay: it quotes a health plan with its `quote` tool and answers a fixed text.

Record one of its turns, make a scenario of it, and run it again in your CI:

    niadra replay --agent examples.replay_demo:build_agent --build examples.replay_demo:BUILD \\
        --scenario <scenario id> --runs 5

No model and nothing random: a replay of it passes every time, so it checks the replay path itself. In the
replay, `quote` answers from the record and never runs. Needs the space's `turns` feature, and a key with
the `replay` scope for the scenario and the run.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from niadra import Handle, Niadra
from niadra.replay import ReplayInput

BUILD = Niadra.build(prompts={"quote": "v1"}, model="demo-model-1")
"""The build every run of the demo pins."""
PLAN = {"plan": "ouro", "lives": 2}


@Niadra.tool("quote")
def quote(plan: str, lives: int) -> dict[str, Any]:
    return {"plan": plan, "lives": lives, "price_full": 511.06}


def build_agent() -> Callable[[ReplayInput | None], str]:
    """A fresh agent: the replay makes one per execution."""

    def agent(_given: ReplayInput | None = None) -> str:
        found = quote(**PLAN)
        return f"O plano {found['plan']} para {found['lives']} vidas sai por R$ {found['price_full']:.2f}."

    return agent


def record_turn(niadra: Niadra, conversation_id: str, subject: Handle) -> str:
    """Records one turn of the demo agent, the turn a scenario starts from; returns its id."""
    with niadra.conversation(conversation_id, subject=subject, agent_id="sales") as conversation:
        conversation.customer("Quanto sai o plano ouro para duas vidas?")
        with conversation.turn(build=BUILD) as frame:
            conversation.agent(build_agent()())
    return frame.turn_id
