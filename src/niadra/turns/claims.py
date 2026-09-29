"""The claim contract in count mode: what the agent said inside a turn, checked against what the turn holds,
and recorded in the record's `claims` (`spec/claim-contract.md`, section 12).

Count mode never changes an output. Every claim gets its verdict, and the act recorded is what was done: a
claim that stands (`matched`, `quoted_found`, `anchored`) records `none`, and any other one `count`, whatever
the contract's configured action would do once a guard acts on it. The check runs on the sender, off the
agent's path, unless the agent asks for it (`conversation.claims.check()`).

The evidence is what the turn's tools returned, in their results and in the objects they showed:

- a field named like a role of a category is a value of that role and of that category's classes, so a
  number said with the role and a different value is a `mismatch`, and one with the same value `matched`;
- any other number, amount or date in them backs a number said with the same value, of the class the
  output gives it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from niadra.claims import STANDING, Finding, Mention, Output, Turn, TurnValue, check, mentions, same_value
from niadra.claims.numbers import decimal_text
from niadra.models.state import ClaimContractSummary
from niadra.models.turns import ClaimRecord
from niadra.turns.capture import Said, TurnFrame, current_turn

logger = logging.getLogger("niadra")

MAX_TEXT = 200
"""A string of a result longer than this is prose, not a value: it is not read for numbers."""


def check_turn(frame: TurnFrame, contract: ClaimContractSummary) -> list[dict[str, Any]]:
    """The claims of everything the turn said, as the record carries them."""
    found: list[dict[str, Any]] = []
    for said in list(frame.said):
        found += check_said(frame, contract, said)
    return found


def check_said(frame: TurnFrame | None, contract: ClaimContractSummary, said: Said) -> list[dict[str, Any]]:
    lang = contract.languages[0] if contract.languages else "pt"
    output = Output(said.text, lang, said.context, said.immutable, said.agent)
    spoken = [m for m in mentions(said.text, lang) if m.cls != "label"]
    turn = evidence(frame, contract, spoken, lang) if frame is not None else Turn()
    return [r for f in check(contract.categories, output, turn) if (r := _record(f)) is not None]


class ClaimCheck:
    """`conversation.claims`: the contract's check, on demand, in count mode."""

    def __init__(self, contract: Callable[[], ClaimContractSummary | None]) -> None:
        self._contract = contract

    def check(
        self, text: str, *, context: str = "chat", immutable: bool = False, agent: str | None = None
    ) -> list[ClaimRecord]:
        """Classifies and counts the claims of `text` (a message before it goes, a document before it is
        saved) against what the current turn holds, and records them in the turn. Never changes `text`,
        never raises: without a contract, or when the check fails, there is nothing to report."""
        contract = self._contract()
        if contract is None:
            return []
        frame = current_turn()
        try:
            records = check_said(frame, contract, Said(text, context, immutable, agent))
        except Exception:
            logger.warning("niadra: the claim check failed", exc_info=True)
            if frame is not None:
                frame.incomplete()
            return []
        if frame is not None:
            frame.add_claims(records)
        return [ClaimRecord.model_validate(r) for r in records]


def evidence(frame: TurnFrame, contract: ClaimContractSummary, spoken: Sequence[Mention], lang: str) -> Turn:
    """What the turn holds that a claim can stand on: the values its tools returned, and their names."""
    roles: dict[str, set[str]] = {}
    for category in contract.categories:
        for role in category.detect.roles:
            roles.setdefault(role, set()).update(category.detect.classes)
    values: list[TurnValue] = []
    tools: list[str] = []
    for call in frame.calls_snapshot():
        if call.get("kind") != "tool":
            continue
        tools.append(call.get("name") or "")
        sources: list[tuple[str | None, Any]] = [
            (o.get("ref"), o.get("fields", {})) for o in call.get("observations", ())
        ]
        result = frame.value_of(call.get("result_model"))
        if result is not None:
            sources.append((None, result))
        for ref, data in sources:
            for key, leaf in _leaves(data):
                values += _values(key, leaf, ref, call["call_id"], roles, spoken, lang)
    return Turn(values=tuple(values), tools=tuple(tools))


def _leaves(value: Any, key: str | None = None) -> Iterator[tuple[str | None, Any]]:
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _leaves(v, str(k))
    elif isinstance(value, list):
        for v in value:
            yield from _leaves(v, key)
    else:
        yield key, value


def _values(
    key: str | None,
    leaf: Any,
    ref: str | None,
    call_id: str,
    roles: dict[str, set[str]],
    spoken: Sequence[Mention],
    lang: str,
) -> list[TurnValue]:
    found: list[tuple[str, dict[str, str]]] = []
    if isinstance(leaf, (int, float)) and not isinstance(leaf, bool):
        try:
            found = [("", {"amount": decimal_text(Decimal(repr(leaf)))})]
        except (InvalidOperation, ValueError):
            return []
    elif isinstance(leaf, str) and len(leaf) <= MAX_TEXT:
        found = [(m.cls, value) for m in mentions(leaf, lang) if (value := m.value()) is not None]
    object_type = ref.split(":", 1)[0] if ref else None
    out: list[TurnValue] = []
    for cls, value in found:
        role = key if key in roles and (not cls or cls in roles[key]) else None
        if role is not None:
            classes = [cls] if cls else sorted(roles[role])
        else:
            said = {m.cls for m in spoken if (m.value() is not None and same_value(m.value() or {}, value))}
            classes = sorted(said if not cls else said & {cls})
        out += [TurnValue(c, value, role, True, object_type, key, call_id, ref) for c in classes]
    return out


def _record(finding: Finding) -> dict[str, Any] | None:
    """A finding as the turn record's `claims` carries it, in count mode; None when it cannot be written."""
    record: dict[str, Any] = {
        "category": finding.category,
        "span": [finding.start, finding.end],
        "verdict": finding.verdict,
        "action": "none" if finding.verdict in STANDING else "count",
    }
    if finding.cls is not None:
        record.update({"class": finding.cls, "nature": finding.nature})
        if finding.role is not None:
            record["role"] = finding.role
        if finding.value:
            record["value"] = dict(finding.value)
    evidence = finding.evidence
    if isinstance(evidence, TurnValue):
        record["evidence"] = {
            k: v
            for k, v in (("call_id", evidence.call_id), ("field", evidence.name), ("ref", evidence.ref))
            if v
        }
    for attempt in (record, _plain(record)):
        try:
            ClaimRecord.model_validate(attempt)
        except ValidationError:
            continue
        return attempt
    logger.debug("niadra: a claim could not be recorded: %s", json.dumps(record)[:200])
    return None


def _plain(record: dict[str, Any]) -> dict[str, Any]:
    """The record without what failed to fit the schema: a value or a field name of another shape."""
    return {k: v for k, v in record.items() if k not in ("value", "evidence")}
