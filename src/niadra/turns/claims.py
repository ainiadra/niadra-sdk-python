"""The claim contract in the turn: what the agent said inside a turn, checked against what the turn holds,
and recorded in the record's `claims` (`spec/claim-contract.md`, section 12).

Count mode never changes an output. Every claim gets its verdict, and the act recorded is what was done: a
claim that stands (`matched`, `quoted_found`, `anchored`) records `none`, and any other one `count`, whatever
the contract's configured action would do. It runs on the sender, off the agent's path, on everything the
turn said that the guard did not see, and on demand (`conversation.claims.check()`). The guard
(`conversation.claims.guard()`, `niadra.turns.guard`) acts, and records the act it took.

The evidence is what the turn's tools returned, in their results and in the objects they showed:

- a field named like a role of a category is a value of that role and of that category's classes, so a
  number said with the role and a different value is a `mismatch`, and one with the same value `matched`;
- any other number, amount or date in them backs a number said with the same value, of the class the
  output gives it.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterable, Iterator, Sequence
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, overload

from pydantic import ValidationError

from niadra.claims import STANDING, Finding, Mention, Output, Turn, TurnValue, check, mentions, same_value
from niadra.claims.internal import InternalText
from niadra.claims.internal import record as internal_record
from niadra.claims.numbers import decimal_text
from niadra.models.state import ClaimContractSummary, ObjectRead
from niadra.models.turns import ClaimRecord
from niadra.turns.capture import Said, StateValue, TurnFrame, current_turn

if TYPE_CHECKING:
    from niadra.turns.guard import Guard, Guarded

logger = logging.getLogger("niadra")

MAX_TEXT = 200
"""A string of a result longer than this is prose, not a value: it is not read for numbers."""


def check_turn(
    frame: TurnFrame, contract: ClaimContractSummary, internal: InternalText | None = None
) -> list[dict[str, Any]]:
    """The claims of everything the turn said, as the record carries them."""
    found: list[dict[str, Any]] = []
    for said in list(frame.said):
        found += check_said(frame, contract, said, internal)
    return found


def check_said(
    frame: TurnFrame | None, contract: ClaimContractSummary, said: Said, internal: InternalText | None = None
) -> list[dict[str, Any]]:
    """The claims of one output in count mode: a claim that stands records `none`, any other `count`, and so
    does a passage of the company's own prompt it repeats."""
    found = findings_of(frame, contract, said)
    records = [
        r for f in found if (r := record_of(f, "none" if f.verdict in STANDING else "count")) is not None
    ]
    return records + [
        internal_record(span, ref, "count") for span, ref in passages(contract, internal, said.text)
    ]


def passages(
    contract: ClaimContractSummary, internal: InternalText | None, text: str
) -> list[tuple[tuple[int, int], str]]:
    """Where `text` repeats the company's prompt the contract names, with the prompt's version."""
    config = contract.internal_text
    if config is None or internal is None:
        return []
    return [
        (span, config.shingle_hashes_ref)
        for span in internal.passages(text, config.shingle_hashes_ref, config.n)
    ]


def findings_of(frame: TurnFrame | None, contract: ClaimContractSummary, said: Said) -> list[Finding]:
    """The claims of one output, with the verdict against what the turn holds and the act its category's
    action gives."""
    lang = contract.languages[0] if contract.languages else "pt"
    output = Output(said.text, lang, said.context, said.immutable, said.agent)
    spoken = [m for m in mentions(said.text, lang) if m.cls != "label"]
    turn = evidence(frame, contract, spoken, lang) if frame is not None else Turn()
    return check(contract.categories, output, turn)


class ClaimCheck:
    """`conversation.claims`: the claim contract in the agent's process. `check()` classifies and counts;
    `guard()` and `guard_text()` act, as the contract's actions say (`niadra.turns.guard`)."""

    def __init__(
        self,
        contract: Callable[[], ClaimContractSummary | None],
        *,
        refresh: Callable[[], Awaitable[object]] | None = None,
        internal: InternalText | None = None,
    ) -> None:
        self._contract = contract
        self._refresh = refresh
        self._internal = internal
        self._warming: asyncio.Task[object] | None = None

    def warm(self) -> None:
        """Starts reading the profile that carries the contract, in the background (the async client)."""
        if self._refresh is None or (self._warming is not None and not self._warming.done()):
            return
        with contextlib.suppress(RuntimeError):
            self._warming = asyncio.get_running_loop().create_task(_quietly(self._refresh()))

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
            records = check_said(frame, contract, Said(text, context, immutable, agent), self._internal)
        except Exception:
            logger.warning("niadra: the claim check failed", exc_info=True)
            if frame is not None:
                frame.incomplete()
            return []
        if frame is not None:
            frame.add_claims(records)
        return [ClaimRecord.model_validate(r) for r in records]

    def guard_text(
        self, text: str, *, context: str = "chat", immutable: bool | None = None, agent: str | None = None
    ) -> Guarded:
        """`text` as the contract's actions leave it, with its claims recorded in the current turn. An
        immutable output (the contract's `outputs.immutable`, or `immutable=True`) never changes: a block
        sends it to a person (`review`). Without a contract the text goes as it is."""
        from niadra.turns.guard import guard_text

        contract = self._contract()
        if contract is None:
            return Guarded(text)
        return guard_text(
            contract,
            current_turn(),
            text,
            context=context,
            immutable=immutable,
            agent=agent,
            internal=self._internal,
        )

    @overload
    def guard(
        self,
        stream: AsyncIterable[str],
        *,
        context: str = ...,
        immutable: bool | None = ...,
        agent: str | None = ...,
        hold: float = ...,
        message: float = ...,
    ) -> AsyncIterator[str]: ...

    @overload
    def guard(
        self,
        stream: Iterable[str],
        *,
        context: str = ...,
        immutable: bool | None = ...,
        agent: str | None = ...,
        hold: float = ...,
        message: float = ...,
    ) -> Iterator[str]: ...

    def guard(
        self,
        stream: AsyncIterable[str] | Iterable[str],
        *,
        context: str = "chat",
        immutable: bool | None = None,
        agent: str | None = None,
        hold: float = 0.150,
        message: float = 0.300,
    ) -> AsyncIterator[str] | Iterator[str]:
        """The stream of the agent's answer, text chunks, as it may reach the customer: what could start a
        claim is held until its sentence ends (at most `hold` seconds, and `message` in total), checked and
        let go as the contract's actions leave it. An async stream gives an async iterator, any other an
        iterator. Without a contract the chunks pass untouched."""
        from niadra.turns.guard import Guard, guard_stream

        make = functools.partial(
            Guard,
            context=context,
            immutable=immutable,
            agent=agent,
            hold=hold,
            message=message,
            internal=self._internal,
        )
        if isinstance(stream, AsyncIterable):
            return self._guard_async(stream, current_turn(), make)
        contract = self._contract()
        if contract is None:
            return iter(stream)
        return guard_stream(make(contract, current_turn()), stream)

    async def _guard_async(
        self,
        stream: AsyncIterable[str],
        frame: TurnFrame | None,
        make: Callable[[ClaimContractSummary, TurnFrame | None], Guard],
    ) -> AsyncIterator[str]:
        from niadra.turns.guard import guard_async_stream

        if self._refresh is not None:
            await _quietly(self._warming if self._warming is not None else self._refresh())
        contract = self._contract()
        if contract is None:
            async for chunk in stream:
                yield chunk
            return
        async for piece in guard_async_stream(make(contract, frame), stream):
            yield piece


async def _quietly(work: Awaitable[object]) -> object:
    """Awaits `work`; a failure is the profile's to report, never the agent's."""
    try:
        return await work
    except Exception:
        return None


def state_values(objects: Iterable[ObjectRead]) -> list[StateValue]:
    """The fields of objects a state read served, as the claim check's evidence. A masked or unknown value
    backs nothing."""
    out: list[StateValue] = []
    for item in objects:
        ref = f"{item.ref.type}:{item.ref.namespace}:{item.ref.id}"
        gaps = tuple(item.declared_gaps)
        for name, field in item.fields.items():
            if field.masked or field.logic != "yes" or field.v is None:
                continue
            out.append(StateValue(ref, name, field.v, field.claim_safe, field.role, gaps))
    return out


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
    for item in list(frame.state):
        # A field read from state backs a claim only while it is fresh enough for one.
        name = item.role if item.role in roles else item.field
        for key, leaf in _leaves(item.value, name):
            found = _values(key, leaf, item.ref, None, roles, spoken, lang)
            values += [
                replace(v, fresh=item.claim_safe, name=item.field, declared_gaps=item.declared_gaps)
                for v in found
            ]
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
    call_id: str | None,
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


def record_of(finding: Finding, act: str) -> dict[str, Any] | None:
    """A finding as the turn record's `claims` carries it, with what was done (`act`); None when it cannot be
    written."""
    record: dict[str, Any] = {
        "category": finding.category,
        "span": [finding.start, finding.end],
        "verdict": finding.verdict,
        "action": act,
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
