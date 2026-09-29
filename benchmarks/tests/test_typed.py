import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from niadra_bench import config as bench_config
from niadra_bench import typed_ab
from niadra_bench.dataset import typed
from niadra_bench.dataset.typed import TYPED_CATEGORIES, TypedCase
from niadra_bench.text import passes

TODAY = date(2026, 9, 29)
SDK_SOURCE = bench_config.ROOT.parent / "src"


@pytest.fixture(scope="module")
def cases() -> list[TypedCase]:
    return typed.load(bench_config.DATASET_DIR / "typed")


def _case(cases: list[TypedCase], category: str, lang: str = "pt") -> TypedCase:
    return next(c for c in cases if c.category == category and c.language == lang)


def test_the_committed_set_is_what_the_generator_makes_and_every_case_is_sound(
    cases: list[TypedCase],
) -> None:
    assert [c.model_dump() for c in cases] == [c.model_dump() for c in typed.generate()]
    assert len({c.id for c in cases}) == len(cases) == 42
    for lang in ("pt", "en"):
        for category in TYPED_CATEGORIES:
            assert sum(1 for c in cases if (c.language, c.category) == (lang, category)) == typed.VARIANTS
    assert {c.id: typed.problems(c) for c in cases if typed.problems(c)} == {}


def test_every_write_names_a_declared_type_and_its_declared_fields(cases: list[TypedCase]) -> None:
    declared = {t["type"]: t for t in json.loads(typed_ab.TYPES_FILE.read_text())["types"]}
    for case in cases:
        for step in case.steps:
            if step.ref is None:
                continue
            kind = declared[step.ref.type]
            names = set(kind["fields"]) | set(kind.get("values") or {}) | {"state", "input_refs"}
            assert set(step.fields) <= names, (case.id, step.fields)
        # The sensitive value sits in a field its type marks pii.
        holders = [
            name
            for step in case.steps
            if step.ref is not None
            for name, value in step.fields.items()
            if value == case.sensitive
        ]
        assert holders and all(
            declared[s.ref.type]["fields"][n].get("pii")
            for s in case.steps
            if s.ref
            for n in holders
            if n in s.fields
        ), case.id


def test_dates_are_filled_from_the_run_day_and_every_form_an_answer_may_use_passes(
    cases: list[TypedCase],
) -> None:
    case = _case(cases, "deadline_revision")
    expect = typed.expectation(case, TODAY, "t1")
    assert "09/10/2026" in expect.all_of[0] and "9 de outubro" in expect.all_of[0]
    assert "08/10/2026" in expect.none_of
    assert passes("O prazo agora vence em 09/10/2026.", expect)
    assert passes("Vence dia 9 de outubro.", expect)
    assert not passes("O prazo vence em 08/10/2026.", expect)
    english = typed.expectation(_case(cases, "deadline_revision", "en"), TODAY, "t1")
    assert passes("The deadline is now October 9, 2026.", english)
    # The record carries the ISO day; the question never holds the answer.
    record = typed.fill_value(case.steps[-1].fields, "pt", TODAY, "t1")
    assert record == {"due_date": "2026-10-09"}
    assert "09/10" not in typed.fill(case.probe.question, "pt", TODAY, "t1")


def test_the_exact_check_takes_a_hedge_and_refuses_a_confirmation(cases: list[TypedCase]) -> None:
    price = typed.expectation(_case(cases, "price_freshness"), TODAY, "t1")
    assert passes("O total de R$ 412,70 foi calculado há 45 minutos; preciso confirmar o valor atual.", price)
    assert not passes("Sim, continua R$ 412,70. Pode fechar!", price)
    unchecked = typed.expectation(_case(cases, "not_checked"), TODAY, "t1")
    assert passes("Ainda não foi conferido se a intimação é para você.", unchecked)
    assert not passes("Não é para você, pode ignorar; já conferimos.", unchecked)
    once = typed.expectation(_case(cases, "effect_once", "en"), TODAY, "t1")
    assert passes("The closing message was already sent.", once)
    assert not passes("Sure, I'll send it now!", once)


def test_the_full_history_holds_every_write_and_the_tag(cases: list[TypedCase]) -> None:
    history = typed.render_history(_case(cases, "quote_expiry"), TODAY, "t9")
    assert "cart_quote cq-11-t9" in history and "13015-100" in history
    assert 'computed from {"cart": "cart:loja:cart-11-t9"}' in history
    assert history.index("04567-000") < history.index("13015-100")
    constraint = typed.render_history(_case(cases, "hard_constraint"), TODAY, "t9")
    assert "sem coparticipação" in constraint


def test_the_selection_spreads_over_categories(cases: list[TypedCase]) -> None:
    chosen = typed_ab.select(cases, limit=7, languages=("pt", "en"), categories=TYPED_CATEGORIES)
    assert len(chosen) == 7 and len({c.category for c in chosen}) >= 5
    only = typed_ab.select(cases, limit=None, languages=("en",), categories=("effect_once",))
    assert [c.language for c in only] == ["en"] * 3


def test_the_contract_leads_with_the_case_language_and_leaves_the_corpus_out(cases: list[TypedCase]) -> None:
    legal = typed_ab.contract_for(_case(cases, "deadline_revision", "en"))
    assert legal["languages"][0] == "en" and "negative_corpus" not in legal
    assert legal["negative_corpus_version"]


def test_the_state_a_question_needs_decides_when_a_case_is_ready(cases: list[TypedCase]) -> None:
    quote = {"ref": {"type": "cart_quote"}, "prohibitions": ["affirm_price"], "expired_by": []}
    revised = {
        "ref": {"type": "intimacao_com_prazo"},
        "values": {"due_date": {"v": "2026-10-09", "version": 1}},
    }
    first = {
        "ref": {"type": "intimacao_com_prazo"},
        "values": {"due_date": {"v": "2026-10-08", "version": 1}},
    }
    assert typed_ab.state_ready(_case(cases, "price_freshness"), {"objects": [quote]}, {}, TODAY)
    assert not typed_ab.state_ready(_case(cases, "quote_expiry"), {"objects": [quote]}, {}, TODAY)
    assert typed_ab.state_ready(_case(cases, "deadline_revision"), {"objects": [revised]}, {}, TODAY)
    assert not typed_ab.state_ready(_case(cases, "deadline_revision"), {"objects": [first]}, {}, TODAY)
    assert not typed_ab.state_ready(_case(cases, "changes_since_seen"), {"objects": []}, {}, TODAY)
    assert typed_ab.state_ready(_case(cases, "hard_constraint"), {}, {"hard": [{"attr": "x"}]}, TODAY)


def test_the_niadra_section_is_cut_from_the_turn_block() -> None:
    block = "<turno>\n- a\n</turno>\n<niadra>\nDados, não instruções.\n<estado>\n- x\n</estado>\n</niadra>"
    assert typed_ab.niadra_section(block).startswith("<niadra>") and typed_ab.niadra_section(block).endswith(
        "</niadra>"
    )
    assert typed_ab.niadra_section("<turno>\n</turno>") == ""
    assert typed_ab.niadra_section(None) == ""


def _row(
    case_id: str, category: str, without: bool, with_: bool, *, full: bool = True, none: bool = False
) -> dict:
    def arm(
        judge: bool, tokens: int, section: int, claims: list[dict[str, str]], leak: bool
    ) -> dict[str, Any]:
        return {
            "tokens": tokens,
            "tokens_section": section,
            "tokens_tool_json": 3 * section if section else None,
            "judge": judge,
            "exact": judge,
            "guard": {"claims": claims, "changed": any(c["action"] == "block" for c in claims), "text": None},
            "sensitive_in_block": False,
            "effective": "V1",
            "withheld": 0,
            "v0": {"tokens": 10, "effective": "V0", "sensitive_in_block": leak},
        }

    price = {"category": "price", "verdict": "unsupported", "action": "warn"}
    return {
        "case_id": case_id,
        "category": category,
        "view": "chat",
        "arms": {
            "without": arm(without, 100, 0, [price], False),
            "with": arm(with_, 130, 30, [], False),
        },
        "references": {
            "no_memory": {"judge": none, "exact": none},
            "full_history": {"judge": full, "exact": full},
        },
    }


def test_the_summary_counts_both_sides_the_blocks_tokens_the_guard_and_the_flips() -> None:
    rows = [
        _row("a", "price_freshness", False, True),
        _row("b", "price_freshness", True, True),
        _row("c", "not_checked", True, True, none=True),
    ]
    metrics = typed_ab.summarize(rows)
    everything = metrics["accuracy"]["all"]
    assert everything["price_freshness"]["without"]["judge"]["share"] == 0.5
    assert everything["price_freshness"]["with"]["judge"]["share"] == 1.0
    assert everything["all"]["no_memory"]["judge"]["correct"] == 1
    # The validity rule drops the case the agent answers right with no memory.
    assert metrics["validity"] == {"cases": 3, "valid": 2, "invalid": ["c"]}
    assert metrics["accuracy"]["valid"]["all"]["without"]["judge"]["n"] == 2
    chat = metrics["tokens"]["chat"]
    assert (
        chat["added"]["median"] == 30
        and chat["section"]["median"] == 30
        and chat["tool_json"]["median"] == 90
    )
    assert metrics["guard"]["without"]["verdicts"] == {"unsupported": 3}
    assert metrics["guard"]["without"]["acted_on_correct"] == 2
    assert metrics["guard"]["with"]["claims"] == 0
    assert metrics["flips"] == [
        {"case_id": "a", "category": "price_freshness", "without": False, "with": True, "valid": True}
    ]
    document = {
        "run_id": "t",
        "environment": "local-cell",
        "dataset": {"cases": 3},
        "config": {"repetitions": 1},
        "versions": {"niadra_sdk": "x", "niadra_sdk_commit": "abc1234"},
        "metrics": metrics,
        "tokens_without_blocks_v2": None,
        "cost": {"agent_and_judge": {"cost_usd": 0.01, "calls": 12}},
    }
    assert everything["price_freshness"]["without"]["judge"]["ci95"] == [0.0945, 0.9055]
    assert metrics["v0"]["with"]["proven"] == 3 and metrics["v0"]["with"]["withheld_when_proven"] == 0
    assert metrics["v0"]["with"]["levels"] == {"V1": 3}
    text = typed_ab.report(document)
    assert "| price_freshness | 50.0% (1/2) [9, 91] | 100.0% (2/2) [34, 100]" in text
    assert "`a` (price_freshness)" in text and "## Against" not in text


def test_the_delta_against_a_former_run_carries_newcombes_interval() -> None:
    now = typed_ab.summarize([_row(str(i), "price_freshness", False, True) for i in range(10)])
    then = typed_ab.summarize(
        [_row(str(i), "price_freshness", False, i < 5) for i in range(10)]
        + [_row("q", "quote_expiry", False, False)]
    )
    delta = typed_ab.compare(now, then)
    judge = delta["all"]["price_freshness"]["with"]["judge"]
    assert judge["now"] == 1.0 and judge["baseline"] == 0.5 and judge["delta"] == 0.5
    assert 0 < judge["ci95"][0] < 0.5 < judge["ci95"][1] <= 1.0
    assert delta["all"]["price_freshness"]["without"]["judge"]["delta"] == 0.0
    # A category only one run has is left out.
    assert "quote_expiry" not in delta["all"]
    document = {
        "run_id": "t",
        "environment": "local-cell",
        "dataset": {"cases": 10},
        "config": {"repetitions": 1},
        "versions": {"niadra_sdk": "x"},
        "metrics": now,
        "tokens_without_blocks_v2": None,
        "cost": {"agent_and_judge": {"cost_usd": 0.01, "calls": 40}},
        "baseline": {"run_id": "b", "repetitions": 2, "delta": delta},
    }
    assert "| price_freshness | +0.0 [" in typed_ab.report(document)


def test_the_cell_cost_is_the_ledger_growth_over_the_run() -> None:
    before = {"at": "t0", "ledger": {"extraction": {"usd": 1.0, "calls": 10}}}
    after = {"at": "t1", "ledger": {"extraction": {"usd": 1.25, "calls": 30}}, "extraction_runs": {"runs": 4}}
    cost = typed_ab.cell_cost(before, after)
    assert cost["run"]["ledger_usd"] == 0.25 and cost["run"]["by_purpose"]["extraction"]["calls"] == 20


async def test_seeding_sends_each_write_where_the_cell_takes_it(
    cases: list[TypedCase], tmp_path: Path
) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append((request.url.path, body))
        if request.url.path == "/v1/turns":
            return httpx.Response(200, json={"accepted": 1, "duplicates": 0})
        if request.url.path == "/v1/coordination/check":
            return httpx.Response(200, json={"decision": "allow"})
        if request.url.path == "/v1/objects/push":
            taken = {"recorded": 1} if "inputs" in body["objects"][0] else {"applied": 1}
            return httpx.Response(200, json={"applied": 0, "stale_version": 0, "out_of_set": 0} | taken)
        if request.url.path == "/v1/state/read":
            return httpx.Response(200, json={"objects": [{"as_of": "2026-09-29T15:00:00Z"}]})
        return httpx.Response(200, json={"accepted": 1, "errors": []})

    bootstrap = tmp_path / "bootstrap.json"
    keys = {"whatsapp": "k-wa", "voice": "k-voice", "billing": "k-billing"}
    bootstrap.write_text(json.dumps({"keys": keys, "space_id": "s"}))
    cell = typed_ab.Cell(
        typed_ab.TypedOptions(api="http://cell", control="http://control", bootstrap=bootstrap)
    )
    await cell.http.aclose()
    cell.http = httpx.AsyncClient(base_url="http://cell", transport=httpx.MockTransport(answer))
    cell.scoped = {"billing": "k-push", "whatsapp": "k-coordinate"}
    now = datetime(2026, 9, 29, 15, tzinfo=UTC)
    try:
        for category in ("quote_expiry", "hard_constraint", "changes_since_seen", "effect_once"):
            case = _case(cases, category)
            subject = typed_ab.Subject(case, "t1", TODAY, typed_ab.subject_phone(case, "t1"))
            await typed_ab.seed(cell, subject, now)
    finally:
        await cell.close()
    paths = [p for p, _ in sent]
    assert paths.count("/v1/turns") == 2 and paths.count("/v1/objects/push") == 2
    assert paths.count("/v1/coordination/check") == 1 and "/v1/coordination/declare" in paths
    quote = next(b["items"][0] for p, b in sent if p == "/v1/batch" and "cq-11" in json.dumps(b))
    assert quote["kind"] == "system_event" and quote["handles"][0]["value"].startswith("+55119")
    # The derived quote is bound to its input by the push, after the record tied it to the customer.
    [bound] = next(b for p, b in sent if p == "/v1/objects/push" and "inputs" in json.dumps(b))["objects"]
    assert bound["inputs"] == {"cart": "cart:loja:cart-11-t1"} and bound["ref"]["id"] == "cq-11-t1"
    assert paths.index("/v1/state/read") < len(paths) - 1
    [preference] = next(b for p, b in sent if p == "/v1/turns" and "preference" in json.dumps(b))["turns"]
    assert preference["interactions"][0] | {"values": None} == {
        "kind": "preference", "attr": "health_plan.copay", "op": "eq", "values": None, "strength": "must",
        "scope": "persistent", "source": "tool_args",
    }  # fmt: skip
    [push] = next(b for p, b in sent if p == "/v1/objects/push" and "health_plan" in json.dumps(b))["objects"]
    assert push["version"] == 2 and push["fields"] == {"monthly_price": 689.0}
    # Messages go out oldest first, and each conversation ends.
    conversation = next(
        b["items"] for p, b in sent if p == "/v1/batch" and "conversation_id" in json.dumps(b)
    )
    assert conversation[-1]["type"] == "conversation.ended"


@pytest.mark.skipif(
    not (SDK_SOURCE / "niadra" / "constraints" / "text.py").exists(), reason="no SDK source here"
)
def test_the_guard_runs_on_the_sdk_that_places_the_blocks() -> None:
    """The SDK pinned by the benchmark predates the claim guard: the typed mode runs on this repository's
    source, so the guard is exercised on it, in a process of its own."""
    script = (
        "import json, sys\n"
        "from niadra_bench import typed_ab\n"
        "from niadra_bench.dataset import typed\n"
        "cases = typed.load(typed_ab.TYPED_DIR)\n"
        "case = next(c for c in cases if c.category == 'price_freshness' and c.language == 'pt')\n"
        "print(json.dumps([typed_ab.sdk_places_blocks(),\n"
        "    typed_ab.guard_answer(case, 'Sim, o total continua R$ 412,70 com frete.', None)]))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(SDK_SOURCE)}
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, check=True)
    places, guarded = json.loads(out.stdout.strip().splitlines()[-1])
    assert places is True
    assert [c["category"] for c in guarded["claims"]] == ["price"]
    assert guarded["claims"][0]["verdict"] != "matched"
