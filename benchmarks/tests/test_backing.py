"""Values without a source (metrics/backing.py) and the guard lines' measurement (Niadra's answers
recorded back)."""

import json
from datetime import UTC, datetime

import httpx

from niadra_bench.identity import Identities
from niadra_bench.metrics import backing
from niadra_bench.metrics.accuracy import CaseRow
from niadra_bench.targets.niadra import Keys, NiadraTarget


def test_a_value_the_memory_holds_is_backed_and_an_invented_one_is_not() -> None:
    block = "Protocolo 7731902, troca aprovada; prazo até 15/10."
    assert backing.check("Seu protocolo é 7731902.", block, "Qual o protocolo?") == (1, [])
    checked, unbacked = backing.check("Seu protocolo é 7731999, até 15/10.", block, "Qual o protocolo?")
    assert (checked, unbacked) == (2, ["number"])
    # The customer's own words back what the agent repeats from them.
    assert backing.check("O pedido 48213 está a caminho.", "", "Cadê o pedido 48213?") == (1, [])
    # Words and one or two digits are not values.
    assert backing.check("Em 2 dias úteis, sem falta.", "", "") == (0, [])


def _row(system: str, category: str, kinds: list[str] | None, checked: int = 1, purpose: str = "answer"):
    return CaseRow(
        repetition=1, system=system, scenario=None, case_id=f"{system}-{category}", language="pt",
        category=category, view=None, purpose=purpose, tokens=0, retrieve_ms=0.0, error=None,
        context_has_answer=None, context_has_answer_loose=None, leak=None, answer="x", deterministic=None,
        judge=None, judge_reason=None, meta={}, backing_checked=checked, unbacked_kinds=kinds,
    )  # fmt: skip


def test_lines_count_values_without_a_source_per_thousand_answers() -> None:
    rows = [
        _row("mem0_oss", "continuity", ["number", "date"]),
        _row("mem0_oss", "continuity", []),
        _row("mem0_oss", "privacy", ["amount"]),
        _row("mem0_oss", "privacy", [], purpose="tokens"),
        _row("niadra", "continuity", []),
    ]
    lines = {line["system"]: line for line in backing.summarize(rows)}
    mem0 = lines["mem0_oss"]
    assert (mem0["answers"], mem0["unbacked"], mem0["answers_with_unbacked"]) == (3, 3, 2)
    assert mem0["per_1000_answers"] == 1000.0 and mem0["by_kind"] == {"amount": 1, "date": 1, "number": 1}
    assert mem0["by_category"]["continuity"]["per_1000_answers"] == 1000.0
    assert lines["niadra"]["per_1000_answers"] == 0.0
    summary = backing.aggregate([{"backing": list(lines.values())}, {"backing": [lines["niadra"]]}])
    assert summary is not None
    across = {r["system"]: r for r in summary["results"]}
    assert across["mem0_oss"]["per_1000_answers"]["runs"] == [1000.0, None]
    assert backing.aggregate([{"accuracy": []}]) is None  # a run from before the metric


async def test_measuring_guards_records_the_answer_as_the_agents_message(cases) -> None:
    posted: list[list[dict]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content)["items"])
        return httpx.Response(200, json={"accepted": 2, "errors": []})

    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    target = NiadraTarget(
        Keys({"whatsapp": "k-wa", "voice": "k-voice"}),
        base_url="http://niadra.test",
        transport_factory=lambda: httpx.MockTransport(answer),
        now=lambda: now,
        record_answers=True,
    )
    await target.start()
    case = cases[0]
    ids = Identities.for_case(case, "t1")
    await target.after_answer(
        case, ids, {"conversation": "bench-t1-conv"}, "Seu protocolo é 7731999.", 1, ["number"]
    )
    await target.after_answer(case, ids, {}, "no conversation, nothing sent", 0, [])
    await target.close()
    [items] = posted
    message, ended = items
    assert message["speaker"]["role"] == "ai_agent" and message["direction"] == "outbound"
    assert message["conversation_id"] == "bench-t1-conv" and message["content"]["text"].startswith("Seu")
    assert message["backing"] == {
        "checked": 1,
        "unbacked_values": [{"kind": "number"}],
        "guard_violations": [],
    }
    assert ended["type"] == "conversation.ended" and target.answers_recorded == 1
