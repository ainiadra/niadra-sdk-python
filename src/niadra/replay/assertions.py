"""The structural assertions of a replay (the replay spec, section 5), evaluated on the replayed turn's record
and the text it emitted. A kind or an argument this runner cannot evaluate, or one whose evidence the turn did
not produce, is `not_checked`, which never fails."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from niadra.claims import STANDING

Outcome = Literal["pass", "fail", "not_checked"]

DENIALS = {
    "pt": ("não temos", "não encontrei", "não há", "indisponível", "esgotado", "não está disponível"),
    "en": ("we don't have", "couldn't find", "not available", "out of stock", "unavailable", "no results"),
    "es": ("no tenemos", "no encontré", "no hay", "no disponible", "agotado"),
}
PROMISES = {
    "pt": ("vou enviar", "vou verificar", "te retorno", "vou providenciar", "vou encaminhar"),
    "en": ("i will send", "i'll send", "i will check", "i'll check", "we will get back"),
    "es": ("le enviaré", "voy a verificar", "le aviso", "voy a enviar"),
}


class Replayed:
    """What an assertion reads: the replayed record, the text the turn emitted, the effect keys it declared
    done and whether it handed off."""

    def __init__(
        self,
        record: Mapping[str, Any],
        text: str,
        values: Callable[[str | None], Any],
        *,
        done: Mapping[str, int],
        handoff: bool,
        lang: str,
    ) -> None:
        self.record = record
        self.text = text.lower()
        self.values = values
        self.done = done
        self.handoff = handoff or bool((record.get("output") or {}).get("handoff_id"))
        self.lang = lang

    @property
    def tools(self) -> list[Mapping[str, Any]]:
        return [c for c in self.record.get("calls") or [] if c.get("kind") == "tool"]

    def says(self, phrases: Sequence[str]) -> bool:
        return any(p.lower() in self.text for p in phrases)


def evaluate(assertion: Mapping[str, Any], turn: Replayed) -> tuple[Outcome, str | None]:
    """An assertion's outcome, with a short detail when it fails (never personal data)."""
    check = _KINDS.get(str(assertion.get("kind")))
    if check is None:
        return "not_checked", "kind unknown to this runner"
    try:
        return check(dict(assertion.get("args") or {}), turn)
    except (KeyError, TypeError, ValueError):
        return "not_checked", "arguments this runner cannot read"


def _passes(ok: bool, detail: str) -> tuple[Outcome, str | None]:
    return ("pass", None) if ok else ("fail", detail)


def _tool_called(args: dict[str, Any], turn: Replayed) -> tuple[Outcome, str | None]:
    tool, least = args["tool"], int(args.get("min", 1))
    calls = sum(1 for c in turn.tools if c.get("name") == tool and c.get("status") == "ok")
    return _passes(calls >= least, f"{calls} ok calls of {tool}, {least} wanted")


def _tool_not_called(args: dict[str, Any], turn: Replayed) -> tuple[Outcome, str | None]:
    tool = args["tool"]
    return _passes(not any(c.get("name") == tool for c in turn.tools), f"{tool} was called")


def _hard_respected(_args: dict[str, Any], turn: Replayed) -> tuple[Outcome, str | None]:
    applied = [c["applied"] for c in turn.tools if c.get("applied")]
    if not applied:
        return "not_checked", None
    return _passes(all(a.get("violations", 0) == 0 for a in applied), "a call violated a hard constraint")


def _showed(call: Mapping[str, Any], turn: Replayed) -> bool:
    if call.get("observations"):
        return True
    result = turn.values(call.get("result_model"))
    if isinstance(result, list):
        return bool(result)
    if isinstance(result, dict):
        return any(isinstance(v, list) and v for v in result.values())
    return False


def _no_denial_with_results(args: dict[str, Any], turn: Replayed) -> tuple[Outcome, str | None]:
    lang = args.get("lang") or turn.lang
    showed = any(_showed(c, turn) for c in turn.tools)
    return _passes(not showed or not turn.says(DENIALS[lang]), "a denial while a tool showed results")


def _claims(turn: Replayed) -> list[Mapping[str, Any]]:
    return list(turn.record.get("claims") or [])


def _claims_traced(_args: dict[str, Any], turn: Replayed) -> tuple[Outcome, str | None]:
    claims = _claims(turn)
    if not claims:
        return "not_checked", None
    untraced = sum(1 for c in claims if not c.get("evidence"))
    return _passes(untraced == 0, f"{untraced} claims without evidence")


def _claims_match_state(_args: dict[str, Any], turn: Replayed) -> tuple[Outcome, str | None]:
    claims = _claims(turn)
    if not claims:
        return "not_checked", None
    off = sorted({c.get("verdict", "") for c in claims if c.get("verdict") not in STANDING})
    return _passes(not off, "verdicts " + ", ".join(off))


def _no_promise_without_action(args: dict[str, Any], turn: Replayed) -> tuple[Outcome, str | None]:
    lang = args.get("lang") or turn.lang
    acted = bool(turn.record.get("effects")) or bool(turn.done) or turn.handoff
    return _passes(not turn.says(PROMISES[lang]) or acted, "a promise with no effect or handoff")


def _expected_in_topk(args: dict[str, Any], turn: Replayed) -> tuple[Outcome, str | None]:
    ref, k, list_id = args["ref"], int(args["k"]), args.get("list_id")
    shown = [
        i
        for i in turn.record.get("interactions") or []
        if i.get("kind") == "presented" and (list_id is None or i.get("list_id") == list_id)
    ]
    if not shown:
        return "not_checked", None
    found = any(
        item.get("ref") == ref and item.get("pos", k + 1) <= k for i in shown for item in i.get("items", [])
    )
    return _passes(found, f"not in the top {k}")


def _handoff_when(args: dict[str, Any], turn: Replayed) -> tuple[Outcome, str | None]:
    expected = bool(args.get("expected", True))
    return _passes(turn.handoff == expected, "handoff" if turn.handoff else "no handoff")


def _effect_once(args: dict[str, Any], turn: Replayed) -> tuple[Outcome, str | None]:
    done = dict(turn.done)
    for effect in turn.record.get("effects") or []:
        if effect.get("state") == "done":
            done.setdefault(effect["key"], 1)
    key = args.get("key")
    if key is not None:
        return _passes(done.get(key, 0) == 1, f"{done.get(key, 0)} times done")
    return _passes(all(n <= 1 for n in done.values()), "an effect done twice")


def _budget(args: dict[str, Any], turn: Replayed) -> tuple[Outcome, str | None]:
    calls = turn.record.get("calls") or []
    models = [c for c in calls if c.get("kind") == "model"]
    tokens = sum((c.get("tokens") or {}).get("in", 0) + (c.get("tokens") or {}).get("out", 0) for c in models)
    counts = {
        "max_tool_calls": len(turn.tools),
        "max_model_calls": len(models),
        "max_tokens": tokens,
        "max_cost_usd": (turn.record.get("cost") or {}).get("usd"),
        "max_latency_ms": turn.record.get("latency_ms"),
    }
    limits = {k: v for k, v in args.items() if k in counts}
    if not limits:
        return "not_checked", "no limit given"
    if any(counts[k] is None for k in limits):
        return "not_checked", None
    over = [k for k, v in limits.items() if counts[k] > v]
    return _passes(not over, "over " + ", ".join(over))


def _lexicon(args: dict[str, Any], turn: Replayed) -> tuple[Outcome, str | None]:
    include, exclude = args.get("must_include") or [], args.get("must_not_include") or []
    if not include and not exclude:
        return "not_checked", "no phrase given"
    missing = [p for p in include if p.lower() not in turn.text]
    present = [p for p in exclude if p.lower() in turn.text]
    return _passes(not missing and not present, f"{len(missing)} missing, {len(present)} forbidden")


def _tools_offered_match(_args: dict[str, Any], _turn: Replayed) -> tuple[Outcome, str | None]:
    return "not_checked", "the tools offered to the model are not in the record"


_KINDS: dict[str, Callable[[dict[str, Any], Replayed], tuple[Outcome, str | None]]] = {
    "tool_called": _tool_called,
    "tool_not_called": _tool_not_called,
    "hard_respected": _hard_respected,
    "no_denial_with_results": _no_denial_with_results,
    "claims_traced": _claims_traced,
    "claims_match_state": _claims_match_state,
    "no_promise_without_action": _no_promise_without_action,
    "expected_in_topk": _expected_in_topk,
    "handoff_when": _handoff_when,
    "effect_once": _effect_once,
    "budget": _budget,
    "lexicon": _lexicon,
    "tools_offered_match": _tools_offered_match,
}
