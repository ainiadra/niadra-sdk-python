"""`bench typed ab`: the typed-object set (`dataset/typed`) with and without the blocks a read asks for.

Every case is seeded once per repetition into a running cell and then asked twice, by the same agent and
judge as every other accuracy number: once with a plain read (`without`), once with `include: ["state",
"constraints"]` (`with`), which the SDK places in the turn block as its `<niadra>` section. The two sides
read the same customer, so the only difference is the text the blocks add. Each side is also read at V0
(no proof of identity), where no case's sensitive value may reach the agent.

What it reports, per side and category (`typed.json`, `typed.md`):
- the judge's and the exact check's accuracy, on every case and on the cases the validity rule keeps (right
  with the whole history, wrong with no memory, as in dataset v2);
- tokens per turn (`o200k_base`) of the memory block, the tokens the blocks add, and the tokens the same
  data would take as a tool's JSON result, which an agent without the blocks would fetch in a round trip;
- the claim guard's verdicts and acts on every answer (the SDK's `guard_text` with the sector's example
  contract; the side with the blocks gives the guard the values they placed, as a turn does), and how often
  it acted on an answer the judge graded correct;
- reads at V0 whose block held the case's sensitive value;
- the coordination check for the effect cases (a second attempt must be refused);
- what the agent and the judge cost (OpenRouter's `usage`), and the cell's own model spend (its ledger and
  its extraction runs, read from the cell's PostgreSQL with `--cell-pg`, as `cell-cost.json`).

With `--v2-sample N`, N cases of dataset v2 are seeded too and read without `include` in both views: the
tokens of a turn that asks for no block. Their customers are the same bytes in every run, so with
`--baseline` (a run of the old code) the token check is paired: each case read by the new code against the
same case read by the old one, and each typed case's plain read against its own in the baseline.

The cell is a local one, `niadra-infra/scripts/local-e2e.sh <niadra-back checkout> --keep`: the harness
declares the typed set's object types (`config/typed.object-types.json`) and turns on the agent features in
its sandbox space with the bootstrap's admin account. The English cases go to a sandbox space of their own
whose language is English (a project the harness creates once, with the same three sources), so every
text the cell writes for them (the pack, the blocks, the slots) is in the customer's language. The SDK must
be one that places the blocks in the turn block (this repository's source: `PYTHONPATH=../src`); the
benchmark's pinned release does not.
Results go to `results/typed/<date>-<id>/`; the environment is `local-cell`, and nothing here is imported by
the site.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import statistics
import subprocess
import time
import uuid
from collections import Counter
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import niadra
from niadra import AsyncNiadra, CacheOptions, Content, EventItem, ObjectRef, Speaker, SpeakerRef, phone
from niadra.models.events import ConversationEndedItem
from niadra.models.state import ClaimContractSummary
from niadra.turns.capture import TurnFrame
from niadra.turns.claims import block_values
from niadra.turns.guard import guard_text

from niadra_bench import config as bench_config
from niadra_bench.agent import AGENT_PROMPT, TYPED_JUDGE_PROMPT, ChatClient
from niadra_bench.config import BenchConfig, ModelCall
from niadra_bench.dataset import generate as generate_v2
from niadra_bench.dataset import typed
from niadra_bench.dataset.model import Case, Expectation
from niadra_bench.dataset.typed import Item, Step, TypedCase
from niadra_bench.identity import Identities, _subscriber
from niadra_bench.runner import _tokenizer
from niadra_bench.sources import ControlPlane, wait_until_served
from niadra_bench.stats import median_interval, share_difference, wilson
from niadra_bench.targets.niadra import Keys, NiadraTarget
from niadra_bench.text import contains, passes

SCHEMA = "niadra-bench.typed.v1"
TYPED_DIR = bench_config.DATASET_DIR / "typed"
TYPES_FILE = bench_config.CONFIG_DIR / "typed.object-types.json"
RESULTS = bench_config.RESULTS_DIR / "typed"
#: The example claim contracts this repository vendors from the specification, one per sector.
CONTRACTS = bench_config.ROOT.parent / "spec" / "examples" / "claim-contract"
SECTOR_CONTRACT = {"retail": "retail", "legal": "legal", "health_plan_sales": "health-plan-sales"}
FEATURES = ("turns", "signals", "state", "claims", "coordination")
INCLUDE = ("state", "constraints")
ARMS: dict[str, tuple[str, ...] | None] = {"without": None, "with": INCLUDE}
#: The token criterion: a turn that asks for no block reads within 5% of what the old code read for it.
TOKEN_TOLERANCE = 0.05
SPACE_TIMEZONE = "America/Sao_Paulo"
READ_TIMEOUT_S = 5.0
KEY_SCOPES: dict[str, tuple[str, ...] | None] = {
    "billing": ("track", "context", "state:push"),
    "whatsapp": ("track", "context", "coordinate"),
    "voice": None,
}
"""The scopes of each source's key; None is the audience's defaults (the bootstrap's own voice key)."""
#: The English cases' space: a project of the sandbox tenant, created once, in the bootstrap's region.
ENGLISH_PROJECT = {"name": "Typed set, English", "slug": "typed-set-en"}
ENGLISH_SETTINGS = {"locale": "en-US"}
SOURCE_FIELDS = ("name", "audience", "channel", "purposes", "verification_ceiling", "trusted_action_ops")
ACTED = frozenset({"block", "rewrite", "warn"})
LEVELS = ("V1", "V2", "V3")

log = logging.getLogger("niadra_bench")


class TypedError(RuntimeError):
    """A typed A/B that cannot run as asked."""


def load_cases(directory: Path = TYPED_DIR) -> list[TypedCase]:
    return typed.load(directory)


def select(
    cases: list[TypedCase], *, limit: int | None, languages: Sequence[str], categories: Sequence[str]
) -> list[TypedCase]:
    """The cases asked for; `limit` spreads over the set so every category shows up."""
    chosen = [c for c in cases if c.language in languages and c.category in categories]
    if limit and limit < len(chosen):
        step = len(chosen) / limit
        chosen = [chosen[int(i * step)] for i in range(limit)]
    return chosen


@dataclass
class TypedOptions:
    api: str
    control: str
    bootstrap: Path
    output: Path | None = None
    limit: int | None = None
    languages: tuple[str, ...] = ("pt", "en")
    categories: tuple[str, ...] = typed.TYPED_CATEGORIES
    repetitions: int = 1
    concurrency: int = 4
    settle_quiet_s: float = 30.0
    settle_timeout_s: float = 300.0
    v2_sample: int = 0
    cell_pg: str | None = None
    agent: str = "llm"
    baseline: Path | None = None
    #: The level each sector's probe proves, V1 unless named. A context read hands its blocks the pack's
    #: verification gate: while the pack holds an item back for a higher level, the state block leaves out
    #: what changed since seen and the constraints block says nothing of the subject.
    levels: dict[str, str] = field(default_factory=dict)


@dataclass
class Subject:
    """One case's customer in one repetition."""

    case: TypedCase
    tag: str
    today: date
    phone: str
    level: str = "V1"

    @property
    def handle(self) -> dict[str, str]:
        return {"type": "phone_e164", "value": self.phone}

    def conversation(self, name: str) -> str:
        return f"typed-{self.tag}-{self.case.id}-{name}"

    def ref(self, step: Step) -> dict[str, str]:
        assert step.ref is not None
        return {"type": step.ref.type, "namespace": step.ref.namespace, "id": self.fill(step.ref.id)}

    def item_ref(self, item: Item) -> str:
        """An item's compact ref (`type:namespace:id`), its id filled."""
        return f"{item.ref.type}:{item.ref.namespace}:{self.fill(item.ref.id)}"

    def fill(self, text: str) -> str:
        return typed.fill(text, self.case.language, self.today, self.tag)

    def values(self, value: Any) -> Any:
        return typed.fill_value(value, self.case.language, self.today, self.tag)


def subject_phone(case: TypedCase, tag: str) -> str:
    seed = f"{case.id}:{tag}"
    if case.language == "pt":
        return f"+55119{_subscriber(seed)}"
    area = 200 + int(_subscriber(seed + ":area")[:3]) % 700
    return f"+1{area}5{_subscriber(seed)[1:]}"


def contract_for(case: TypedCase) -> dict[str, Any]:
    """The sector's example contract as the SDK keeps it (no negative corpus), the case's language first."""
    document = json.loads((CONTRACTS / f"{SECTOR_CONTRACT[case.sector]}.json").read_text())
    corpus = document.pop("negative_corpus", None) or {}
    languages = [case.language, *[lang for lang in document["languages"] if lang != case.language]]
    return {**document, "languages": languages, "negative_corpus_version": corpus.get("version")}


def guard_answer(case: TypedCase, answer: str, state: Any | None, constraints: Any | None) -> dict[str, Any]:
    """The SDK's claim guard on the answer, as a mutable chat output; the values the read's blocks placed in
    the turn block are the turn's evidence, as `context()` records them inside a turn."""
    contract = ClaimContractSummary.model_validate(contract_for(case))
    frame = TurnFrame(None, agent=case.probe.agent)
    if state is not None or constraints is not None:
        frame.observe_state(block_values(state, constraints))
    guarded = guard_text(contract, frame, answer, context="chat")
    claims = [
        {"category": c.category, "verdict": str(c.verdict), "action": str(c.action)} for c in guarded.claims
    ]
    return {"claims": claims, "changed": guarded.changed, "text": guarded.text if guarded.changed else None}


def niadra_section(turn_block: str | None) -> str:
    """The `<niadra>` section the SDK adds for the blocks, or ""."""
    text = turn_block or ""
    start, end = text.find("<niadra>"), text.find("</niadra>")
    return text[start : end + len("</niadra>")] if start >= 0 and end > start else ""


@dataclass
class Space:
    """One sandbox space of the cell, in one language, with a key per source."""

    language: str
    space_id: str
    keys: dict[str, str]
    clients: dict[str, AsyncNiadra] = field(default_factory=dict)


def declared_types(document: dict[str, Any], lang: str) -> list[dict[str, Any]]:
    """The typed set's object types as a space of `lang` declares them: each field by the label the config
    gives it in that language (`labels`), else as written."""
    labels = (document.get("labels") or {}).get(lang) or {}
    types = []
    for declared in document["types"]:
        fields = {
            name: {**spec, "label": labels[f"{declared['type']}.{name}"]}
            if f"{declared['type']}.{name}" in labels
            else spec
            for name, spec in declared["fields"].items()
        }
        types.append({**declared, "fields": fields})
    return types


class Cell:
    """The running cell's data API and control plane, as the sandbox's sources and its admin: the
    bootstrap's space for the Portuguese cases, and an English one for the English cases."""

    def __init__(self, options: TypedOptions) -> None:
        self.options = options
        self.document = json.loads(options.bootstrap.read_text())
        self.keys = {str(k): str(v) for k, v in self.document["keys"].items()}
        self.http = httpx.AsyncClient(base_url=options.api, timeout=30.0)
        self.control = ControlPlane(options.control, self.document, httpx.AsyncClient(timeout=30.0))
        self.spaces: dict[str, Space] = {"pt": Space("pt", str(self.document["space_id"]), dict(self.keys))}

    def headers(self, source: str, lang: str = "pt") -> dict[str, str]:
        return {"authorization": f"Bearer {self.spaces[lang].keys[source]}"}

    def client(self, channel: str, lang: str = "pt") -> Any:
        """The SDK's async client of the channel's source in the language's space; `Any`, since its `include`
        and blocks are newer than the benchmark's pinned release."""
        space = self.spaces[lang]
        source = "voice" if channel == "voice" and "voice" in space.keys else "whatsapp"
        if source not in space.clients:
            space.clients[source] = AsyncNiadra(
                space.keys[source],
                base_url=self.options.api,
                channel=channel,
                cache=CacheOptions(enabled=False),
            )
        return space.clients[source]

    async def document_of(self, kind: str, space_id: str) -> dict[str, Any]:
        response = await self.control._call("GET", f"/v1/config/{kind}", params={"space_id": space_id})
        return dict(_ok(response, 200)["document"])

    async def approve(self, kind: str, document: dict[str, Any], reason: str, space_id: str) -> None:
        body = {"space_id": space_id, "type": kind, "document": document, "reason": reason}
        created = _ok(await self.control._call("POST", "/v1/config/diffs", json=body), 201)
        _ok(await self.control._call("POST", f"/v1/config/diffs/{created['diff_id']}/approve"), 200)

    async def setup(self, languages: Sequence[str]) -> dict[str, Any]:
        """For each language's space: the agent features on, the typed set's object types declared, and a
        key per source with the scopes its writes need; then waits until the cell serves all of it."""
        document = json.loads(TYPES_FILE.read_text())
        names = {t["type"] for t in document["types"]}
        out: dict[str, Any] = {"types": sorted(names), "spaces": {}}
        for lang in languages:
            space = self.spaces.get(lang) or await self._english_space()
            declared = declared_types(document, lang)
            features = await self.document_of("features", space.space_id)
            enabled = set(features.get("enabled") or ())
            if not set(FEATURES) <= enabled:
                wanted = {"enabled": sorted(enabled | set(FEATURES))}
                await self.approve("features", wanted, "benchmark: typed set", space.space_id)
            registry = await self.document_of("object-types", space.space_id)
            current = registry.get("types") or []
            if not all(t in current for t in declared):
                kept = [t for t in current if t.get("type") not in names]
                types = {**registry, "types": [*kept, *declared]}
                await self.approve("object-types", types, "benchmark: typed set", space.space_id)
            await self._keys(space)
            await self._until_types_served(names, lang)
            out["spaces"][lang] = {"space_id": space.space_id, "features": sorted(enabled | set(FEATURES))}
        return out

    async def _keys(self, space: Space) -> None:
        """A key per source of the space with the scopes its writes need, served before it is used."""
        listed = _ok(await self.control._call("GET", "/v1/sources", params={"space_id": space.space_id}), 200)
        for name, scopes in KEY_SCOPES.items():
            if scopes is None and name in space.keys:
                continue
            source_id = next(s["source_id"] for s in listed if s["name"] == name)
            body = {"scopes": list(scopes)} if scopes is not None else {}
            issued = await self.control._call("POST", f"/v1/sources/{source_id}/keys", json=body)
            space.keys[name] = str(_ok(issued, 201)["secret"])
        for key in space.keys.values():
            await wait_until_served(self.http, self.options.api, key)

    async def _english_space(self) -> Space:
        """The English cases' sandbox space: created once, with the bootstrap space's sources and English as
        its language (the time zone stays, so dates read as in the Portuguese space)."""
        projects = _ok(await self.control._call("GET", "/v1/projects"), 200)
        found = next((p for p in projects if p["slug"] == ENGLISH_PROJECT["slug"]), None)
        if found is None:
            region = next(p["region"] for p in projects if p["project_id"] == self.document["project_id"])
            body = {**ENGLISH_PROJECT, "region": region}
            found = _ok(await self.control._call("POST", "/v1/projects", json=body), 201)
        space_id = str(next(s["space_id"] for s in found["spaces"] if s["environment"] == "sandbox"))
        await self._same_sources(space_id)
        settings = await self.document_of("settings", space_id)
        if any(settings.get(k) != v for k, v in ENGLISH_SETTINGS.items()):
            await self.approve("settings", {**settings, **ENGLISH_SETTINGS}, "benchmark: typed set", space_id)
        space = Space("en", space_id, {})
        self.spaces["en"] = space
        return space

    async def _same_sources(self, space_id: str) -> None:
        """The bootstrap space's sources of `KEY_SCOPES`, as they are there, in `space_id`."""
        mine = {
            s["name"]
            for s in _ok(await self.control._call("GET", "/v1/sources", params={"space_id": space_id}), 200)
        }
        params = {"space_id": self.spaces["pt"].space_id}
        for source in _ok(await self.control._call("GET", "/v1/sources", params=params), 200):
            if source["name"] not in KEY_SCOPES or source["name"] in mine:
                continue
            copied = {k: source[k] for k in SOURCE_FIELDS}
            _ok(await self.control._call("POST", "/v1/sources", json={**copied, "space_id": space_id}), 201)

    async def _until_types_served(self, names: set[str], lang: str, timeout_s: float = 180.0) -> None:
        """A declaration reaches the cell with the next snapshot: the SDK profile lists it then."""
        deadline = time.monotonic() + timeout_s
        while True:
            response = await self.http.get("/v1/sdk/profile", headers=self.headers("voice", lang))
            if response.status_code == 200:
                served = {t.get("type") for t in response.json().get("types") or ()}
                if names <= served:
                    return
            if time.monotonic() > deadline:
                raise TypedError(f"the typed set's types were not served after {timeout_s:.0f} s")
            await asyncio.sleep(3)

    async def close(self) -> None:
        for space in self.spaces.values():
            for client in space.clients.values():
                await client.close(timeout=2.0)
        await self.http.aclose()
        await self.control.http.aclose()


async def _post(
    cell: Cell,
    path: str,
    body: dict[str, Any],
    source: str,
    lang: str,
    *,
    timeout_s: float = 120.0,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A write in the language's space, again while the cell answers 404 (a feature not there yet), 429 or
    503."""
    deadline = time.monotonic() + timeout_s
    auth = cell.headers(source, lang)
    while True:
        response = await cell.http.post(path, json=body, headers={**auth, **(headers or {})})
        if response.status_code in (200, 201, 207):
            return dict(response.json()) if response.content else {}
        if response.status_code not in (404, 429, 503) or time.monotonic() > deadline:
            raise TypedError(f"POST {path} -> {response.status_code}: {response.text[:300]}")
        await asyncio.sleep(3)


async def seed(cell: Cell, subject: Subject, now: datetime) -> None:
    """The case's writes, oldest first, each at its time before the question."""
    case = subject.case
    for index, step in enumerate(sorted(case.steps, key=lambda s: -s.minutes_ago)):
        at = now - timedelta(minutes=step.minutes_ago)
        key = f"typed-{subject.tag}-{case.id}-{index}"
        match step.kind:
            case "conversation":
                await _conversation(cell, subject, step, at, key)
            case "record":
                event = EventItem(
                    kind="system_event",
                    idempotency_key=key,
                    channel="erp",
                    handles=[phone(subject.phone)],
                    object_refs=[ObjectRef(**subject.ref(step))],
                    speaker=SpeakerRef(role=Speaker.SYSTEM),
                    canonical_type=f"{step.ref.type}.updated" if step.ref else None,
                    fields=subject.values(step.fields),
                    occurred_at=at,
                ).model_dump(mode="json", exclude_none=True)
                body = await _post(cell, "/v1/batch", {"items": [event]}, "billing", case.language)
                if body.get("errors"):
                    raise TypedError(f"{case.id}: record refused: {body['errors'][:2]}")
            case "present" | "preference":
                await _turn(cell, subject, step, at, key)
            case "push":
                await _push(cell, subject, step, at)
            case "effect":
                await _effect(cell, subject, step, key)


async def _state_read(cell: Cell, ref: dict[str, str], lang: str) -> dict[str, Any]:
    response = await cell.http.post(
        "/v1/state/read", json={"refs": [ref]}, headers=cell.headers("voice", lang)
    )
    objects = response.json().get("objects") if response.status_code == 200 else None
    return dict(objects[0]) if objects else {}


async def _push(cell: Cell, subject: Subject, step: Step, at: datetime, timeout_s: float = 300.0) -> None:
    """The platform's push. A shared object takes it once the turn that showed it put it in the working set
    (`out_of_set` until the turns worker applied it); a customer's object is recorded as a system event, and
    the next write waits until the cell bound it to its inputs."""
    assert step.version is not None
    ref = subject.ref(step)
    item: dict[str, Any] = {
        "ref": ref,
        "version": step.version,
        "fields": subject.values(step.fields),
        "provenance": {"source": "live", "source_observed_at": at.isoformat()},
    }
    if step.inputs:
        item["inputs"] = {name: subject.fill(value) for name, value in step.inputs.items()}
    deadline = time.monotonic() + timeout_s
    while True:
        answer = await _post(cell, "/v1/objects/push", {"objects": [item]}, "billing", subject.case.language)
        if answer.get("applied") or answer.get("recorded"):
            break
        if time.monotonic() > deadline:
            raise TypedError(f"{subject.case.id}: push not taken: {answer}")
        await asyncio.sleep(3)
    # The recorded push is applied by the workers, after the record that tied the object to the customer: its
    # observation time moves the object's `as_of`, and only then may a change of an input expire it.
    while step.inputs and not _as_of_reached(await _state_read(cell, ref, subject.case.language), at):
        if time.monotonic() > deadline:
            raise TypedError(f"{subject.case.id}: the derived object was never bound to its inputs")
        await asyncio.sleep(3)


def _as_of_reached(found: dict[str, Any], at: datetime) -> bool:
    stamp = found.get("as_of")
    return bool(stamp) and datetime.fromisoformat(str(stamp).replace("Z", "+00:00")) >= at - timedelta(
        seconds=1
    )


async def _conversation(cell: Cell, subject: Subject, step: Step, at: datetime, key: str) -> None:
    conversation = subject.conversation(step.conversation or "c")
    items = []
    for i, message in enumerate(step.messages):
        customer = message.role == "customer"
        items.append(
            EventItem(
                kind="message",
                idempotency_key=f"{key}-m{i}",
                channel=step.channel,
                conversation_id=conversation,
                handles=[phone(subject.phone)],
                speaker=SpeakerRef(role=Speaker.CUSTOMER)
                if customer
                else SpeakerRef(role=Speaker.AI_AGENT, id=step.agent),
                direction="inbound" if customer else "outbound",
                content=Content(text=subject.fill(message.text)),
                occurred_at=at + timedelta(seconds=20 * i),
            ).model_dump(mode="json", exclude_none=True)
        )
    ended = ConversationEndedItem(
        idempotency_key=f"{key}-end",
        conversation_id=conversation,
        occurred_at=at + timedelta(seconds=20 * len(step.messages)),
    )
    items.append(ended.model_dump(mode="json", exclude_none=True))
    source = "voice" if step.channel == "voice" else "whatsapp"
    body = await _post(cell, "/v1/batch", {"items": items}, source, subject.case.language)
    if body.get("errors"):
        raise TypedError(f"{subject.case.id}: conversation refused: {body['errors'][:2]}")


async def _turn(cell: Cell, subject: Subject, step: Step, at: datetime, key: str) -> None:
    """An agent's turn record: the tool call that saw a shared object and showed it, or whose arguments
    carried the customer's filter."""
    stamp = at.isoformat()
    call: dict[str, Any] = {"call_id": "c1", "kind": "tool", "name": step.tool}
    interactions: list[dict[str, Any]] = []
    if step.kind == "present":
        shown = [(subject.item_ref(item), subject.values(item.fields)) for item in step.items]
        call["observations"] = [
            {"ref": ref, "fields": fields, "provenance": {"source": "live", "source_observed_at": stamp}}
            for ref, fields in shown
        ]
        interactions.append(
            {
                "kind": "presented",
                "exposure_id": str(uuid.uuid4()),
                "list_id": "results",
                "visible_k": len(shown),
                "items": [
                    {"pos": pos, "ref": ref, "shown": fields}
                    for pos, (ref, fields) in enumerate(shown, start=1)
                ],
                "delivered_at": stamp,
            }
        )
    else:
        assert step.preference is not None
        interactions.append(
            {
                "kind": "preference",
                "attr": step.preference.attr,
                "op": step.preference.op,
                "values": step.preference.values,
                "strength": "must",
                "scope": "persistent",
                "source": "tool_args",
            }
        )
    turn = {
        "turn_id": key,
        "conversation_id": subject.conversation(step.conversation or "c"),
        "agent": {"name": step.agent},
        "started_at": stamp,
        "content_mode": "hash_only",
        "calls": [call],
        "interactions": interactions,
    }
    body = await _post(cell, "/v1/turns", {"turns": [turn]}, "whatsapp", subject.case.language)
    if body.get("accepted", 0) + body.get("duplicates", 0) != 1:
        raise TypedError(f"{subject.case.id}: turn not taken: {body}")


def _effect_check(subject: Subject, step: Step) -> dict[str, Any]:
    return {
        "subject": subject.handle,
        "agent": step.agent,
        "intent": step.effect,
        "channel": step.channel,
        "direction": "outbound",
        "purpose": "service",
        "effect_key": f"{step.effect}:{subject.conversation(step.conversation or 'c')}",
    }


async def _effect(cell: Cell, subject: Subject, step: Step, key: str) -> None:
    """The agent checks the effect, carries it out and settles it `done`."""
    check = _effect_check(subject, step)
    lang = subject.case.language
    first = await _post(cell, "/v1/coordination/check", check, "whatsapp", lang)
    if first.get("decision") != "allow":
        raise TypedError(f"{subject.case.id}: the first attempt was not allowed: {first}")
    body = {
        "kind": "effect",
        "subject": subject.handle,
        "agent": step.agent,
        "detail": {"effect_key": check["effect_key"], "state": "done", "attempt": 1},
    }
    await _post(
        cell, "/v1/coordination/declare", body, "whatsapp", lang, headers={"idempotency-key": f"{key}-done"}
    )


def state_ready(case: TypedCase, view: dict[str, Any], constraints: dict[str, Any], today: date) -> bool:
    """Whether the cell holds what the case's question is about."""
    objects = view.get("objects") or []

    def of(kind: str) -> list[dict[str, Any]]:
        return [o for o in objects if (o.get("ref") or {}).get("type") == kind]

    match case.category:
        case "price_freshness":
            return any(o.get("prohibitions") for o in of("cart_quote"))
        case "quote_expiry":
            return any(o.get("expired_by") for o in of("cart_quote"))
        case "deadline_revision":
            # The last due date the court's system sent, whatever version number the value got.
            revised = [s.fields["due_date"] for s in case.steps if "due_date" in s.fields][-1]
            wanted = typed.fill_value(revised, case.language, today, "")
            return any(
                ((o.get("values") or {}).get("due_date") or {}).get("v") == wanted
                for o in of("intimacao_com_prazo")
            )
        case "not_checked":
            return bool(of("intimacao_com_prazo"))
        case "changes_since_seen":
            return bool(view.get("changes_since_seen"))
        case "hard_constraint":
            return bool(constraints.get("hard"))
    return True


async def settle(cell: Cell, subjects: list[Subject], quiet_s: float, timeout_s: float) -> dict[str, Any]:
    """Waits until every case's objects hold what its question is about and no customer's context changed for
    `quiet_s` (the conversations are extracted in the background)."""
    started = time.monotonic()
    last: dict[str, tuple[str, str]] = {}
    quiet_since = time.monotonic()
    ready: dict[str, bool] = {}
    limit = asyncio.Semaphore(4)

    async def one(subject: Subject) -> tuple[str, str]:
        async with limit:
            lang = subject.case.language
            if not ready.get(subject.case.id):
                auth = cell.headers("voice", lang)
                view = await cell.http.post("/v1/state/view", json={"subject": subject.handle}, headers=auth)
                constraints: dict[str, Any] = {}
                if subject.case.category == "hard_constraint":
                    asked = await cell.http.post(
                        "/v1/constraints", json={"subject": subject.handle}, headers=auth
                    )
                    constraints = asked.json() if asked.status_code == 200 else {}
                body = view.json() if view.status_code == 200 else {}
                ready[subject.case.id] = state_ready(subject.case, body, constraints, subject.today)
            context = await cell.client("voice", lang).context(
                phone(subject.phone), verification=subject.level, timeout=READ_TIMEOUT_S, use_cache=False
            )
            return context.version, context.etag

    rounds = 0
    while True:
        rounds += 1
        versions = await asyncio.gather(*(one(s) for s in subjects))
        current = {s.case.id: v for s, v in zip(subjects, versions, strict=True)}
        if current != last or not all(ready.get(s.case.id) for s in subjects):
            quiet_since = time.monotonic() if current != last else quiet_since
            last = current
        elapsed = time.monotonic() - started
        if all(ready.get(s.case.id) for s in subjects) and time.monotonic() - quiet_since >= quiet_s:
            return {"settled": True, "seconds": round(elapsed, 1), "rounds": rounds, "not_ready": []}
        if elapsed >= timeout_s:
            missing = sorted(s.case.id for s in subjects if not ready.get(s.case.id))
            return {"settled": False, "seconds": round(elapsed, 1), "rounds": rounds, "not_ready": missing}
        await asyncio.sleep(5)


async def gather_limited[T](at_once: int, work: list[Coroutine[Any, Any, T]]) -> list[T]:
    """Every coroutine, `at_once` at a time, in order."""
    limit = asyncio.Semaphore(at_once)

    async def one(item: Coroutine[Any, Any, T]) -> T:
        async with limit:
            return await item

    return list(await asyncio.gather(*(one(w) for w in work)))


async def seed_or_fail(cell: Cell, subject: Subject, now: datetime) -> tuple[str, str | None]:
    """A case the cell would not take is reported and left out, never half-asked."""
    try:
        await seed(cell, subject, now)
    except (TypedError, httpx.HTTPError) as exc:
        return subject.case.id, f"{type(exc).__name__}: {str(exc)[:300]}"
    return subject.case.id, None


async def probe_or_fail(
    cell: Cell, subject: Subject, grader: Grader, count: Callable[[str], int]
) -> dict[str, Any]:
    try:
        return await probe(cell, subject, grader, count)
    except (TypedError, httpx.HTTPError, RuntimeError) as exc:
        case = subject.case
        why = f"{type(exc).__name__}: {str(exc)[:300]}"
        return {"case_id": case.id, "language": case.language, "category": case.category, "probe_error": why}


class Grader:
    """The benchmark's agent and judge (config [agent] and [judge]), with the typed set's judge rule."""

    def __init__(self, config: BenchConfig, chat: ChatClient, mode: str) -> None:
        self.agent_call: ModelCall = config.agent
        self.judge_call: ModelCall = config.judge
        self.chat = chat
        self.mode = mode

    async def answer(self, case: TypedCase, question: str, memory: str) -> str:
        if self.mode == "context":
            return memory
        channel = case.probe.channel
        system = AGENT_PROMPT.format(company=case.company, channel=channel, memory=memory or "(empty)")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": question}]
        return await self.chat.complete(self.agent_call, messages)

    async def grade(
        self, question: str, expect: Expectation, rule: str, answer: str
    ) -> tuple[bool | None, str]:
        if self.mode == "context":
            return None, "no judge in a context-only run"
        prompt = TYPED_JUDGE_PROMPT.format(
            question=question, reference=expect.reference_answer, rule=rule, answer=answer or "(no answer)"
        )
        raw = await self.chat.complete(self.judge_call, [{"role": "user", "content": prompt}])
        try:
            verdict = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
            return bool(verdict["correct"]), str(verdict.get("reason", ""))[:300]
        except (ValueError, KeyError):
            return None, "unparseable verdict"


async def probe(cell: Cell, subject: Subject, grader: Grader, count: Callable[[str], int]) -> dict[str, Any]:
    """Both sides' reads, answers and grades for one case, the two references, and the V0 reads."""
    case = subject.case
    question = subject.fill(case.probe.question)
    expect = typed.expectation(case, subject.today, subject.tag)
    rule = subject.fill(case.judge_rule)
    client = cell.client(case.probe.channel, case.language)
    handle = phone(subject.phone)
    view = "voice" if case.probe.channel == "voice" else "chat"
    row: dict[str, Any] = {
        "case_id": case.id,
        "language": case.language,
        "sector": case.sector,
        "category": case.category,
        "question": question,
        "reference_answer": expect.reference_answer,
        "view": view,
        "level": subject.level,
        "arms": {},
    }
    for arm, include in ARMS.items():
        conversation = subject.conversation(f"probe-{arm}")
        await client.verify(
            case.probe.verify_method, subject.level, handle=handle, conversation_id=conversation
        )
        context = await client.context(
            handle,
            view=view,
            verification=subject.level,
            conversation_id=conversation,
            query=question,
            include=list(include) if include else None,
            timeout=READ_TIMEOUT_S,
            use_cache=False,
        )
        memory = "\n\n".join(part for part in (context.system_block, context.turn_block) if part)
        section = niadra_section(context.turn_block)
        answer = await grader.answer(case, question, memory)
        judge, reason = await grader.grade(question, expect, rule, answer)
        state, constraints = (context.state, context.constraints) if include else (None, None)
        tool_json = ""
        if include:
            parts = [
                p.model_dump_json(exclude_none=True)
                for p in (context.state, context.constraints)
                if p is not None
            ]
            tool_json = "\n".join(parts)
        v0 = await client.context(
            handle,
            view=view,
            verification="V0",
            conversation_id=subject.conversation(f"probe-{arm}-v0"),
            query=question,
            include=list(include) if include else None,
            timeout=READ_TIMEOUT_S,
            use_cache=False,
        )
        v0_memory = "\n\n".join(part for part in (v0.system_block, v0.turn_block) if part)
        row["arms"][arm] = {
            "tokens": count(memory),
            "tokens_section": count(section),
            "tokens_tool_json": count(tool_json) if tool_json else None,
            "error": context.error,
            "effective": str(context.verification.effective.value),
            "withheld": context.withheld,
            "degraded": getattr(context, "degraded", None),
            "section": section or None,
            "answer": answer,
            "judge": judge,
            "judge_reason": reason,
            "exact": passes(answer, expect),
            "guard": guard_answer(case, answer, state, constraints)
            if answer
            else {"claims": [], "changed": False, "text": None},
            "sensitive_in_block": contains(memory, case.sensitive),
            "v0": {
                "tokens": count(v0_memory),
                "effective": str(v0.verification.effective.value),
                "sensitive_in_block": contains(v0_memory, case.sensitive),
            },
            "context": memory,
        }
    history = typed.render_history(case, subject.today, subject.tag)
    row["references"] = {}
    for name, memory in (("no_memory", ""), ("full_history", history)):
        answer = await grader.answer(case, question, memory)
        judge, reason = await grader.grade(question, expect, rule, answer)
        row["references"][name] = {
            "answer": answer,
            "judge": judge,
            "judge_reason": reason,
            "exact": passes(answer, expect),
        }
    effects = [s for s in case.steps if s.kind == "effect"]
    if effects:
        check = _effect_check(subject, effects[0])
        again = await _post(cell, "/v1/coordination/check", check, "whatsapp", case.language)
        row["coordination"] = {"decision": again.get("decision"), "reasons": again.get("reasons") or []}
    return row


async def v2_tokens(cell: Cell, sample: int, now: datetime, count: Callable[[str], int]) -> dict[str, Any]:
    """Dataset v2 cases read with no block, in both views: a turn that asks for none. The customers are the
    same bytes in every run of the same sample (their tag comes from the cases), so a run of the old code
    and one of the new read the same subjects, case by case."""
    cases: list[Case] = generate_v2.load(bench_config.dataset_dir("v2"))
    chosen = cases[:: max(1, len(cases) // sample)][:sample]
    target = NiadraTarget(
        Keys(dict(cell.keys), cell.document),
        base_url=cell.options.api,
        now=lambda: now,
        settle_quiet_s=20.0,
        settle_timeout_s=900.0,
        concurrency=4,
    )
    tag = "tv2" + hashlib.sha256(",".join(c.id for c in chosen).encode()).hexdigest()[:5]
    pairs = [(c, Identities.for_case(c, tag)) for c in chosen]
    await target.start()
    try:
        limit = asyncio.Semaphore(4)

        async def one(case: Case, ids: Identities) -> None:
            async with limit:
                await target.seed(case, ids)

        await asyncio.gather(*(one(c, i) for c, i in pairs))
        settled = await target.settle(pairs)
        reads: dict[str, dict[str, int]] = {"voice": {}, "chat": {}}
        for case, ids in pairs:
            for view in ("voice", "chat"):
                got = await target.retrieve(case, ids, view=view)
                reads[view][case.id] = count(got.text)
    finally:
        await target.close()
    out: dict[str, Any] = {"cases": len(chosen), "tag": tag, "settle": settled, "reads": reads}
    for view, found in reads.items():
        values = list(found.values())
        out[view] = {"median": statistics.median(values) if values else None, "p95": _p95(values)}
    return out


def paired(now: dict[str, int], before: dict[str, int]) -> dict[str, Any] | None:
    """The same reads by the new code and by the old: each pair's relative change, its median with the
    median's 95% interval, and whether that median stays within `TOKEN_TOLERANCE`."""
    keys = sorted(k for k in set(now) & set(before) if before[k])
    if not keys:
        return None
    changes = [now[k] / before[k] - 1 for k in keys]
    median = statistics.median(changes)
    interval = median_interval(changes)
    return {
        "pairs": len(keys),
        "before_median": statistics.median(before[k] for k in keys),
        "now_median": statistics.median(now[k] for k in keys),
        "median_change": round(median, 4),
        "ci95": [round(b, 4) for b in interval] if interval else None,
        "changed": sum(1 for k in keys if now[k] != before[k]),
        "within_5pct": abs(median) <= TOKEN_TOLERANCE,
    }


def paired_tokens(document: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """A turn that asks for no block, new code against old on the same subjects, per view: the dataset v2
    sample case by case, and each typed case's plain read by case and repetition."""
    out: dict[str, Any] = {}
    sources = {
        "v2_sample": ((document.get("tokens_without_blocks_v2") or {}).get("reads"),
                      (baseline.get("tokens_without_blocks_v2") or {}).get("reads")),
        "typed_without": (document["metrics"]["tokens"].get("without_reads"),
                          baseline["metrics"].get("tokens", {}).get("without_reads")),
    }  # fmt: skip
    for name, (now, before) in sources.items():
        for view in ("voice", "chat"):
            found = paired((now or {}).get(view) or {}, (before or {}).get(view) or {})
            if found is not None:
                out.setdefault(name, {})[view] = found
    return out


def cell_spend(
    conninfo: str | None, space_ids: Sequence[str], since: datetime | None = None
) -> dict[str, Any] | None:
    """The cell's own model spend over the run's spaces, from its PostgreSQL: the ledger (every purpose, per
    day) and, with `since`, the extraction runs created after it. None without `--cell-pg` or `psql`."""
    psql = shutil.which("psql") or next(
        (str(p) for p in Path("/opt/homebrew/opt/postgresql@17/bin").glob("psql")), None
    )
    if not conninfo or not psql:
        return None

    def query(database: str, sql: str) -> list[list[str]]:
        out = subprocess.run(  # noqa: S603
            [psql, f"{conninfo} dbname={database}", "-tAF", "\t", "-c", sql],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if out.returncode != 0:
            raise TypedError(f"psql: {out.stderr.strip()[:200]}")
        return [line.split("\t") for line in out.stdout.splitlines() if line.strip()]

    ledger: dict[str, dict[str, Any]] = {}
    runs = Counter[str]()
    for space_id in space_ids:
        [[db]] = query("niadra_cell", f"SELECT db_name FROM space_databases WHERE space_id = '{space_id}'")  # noqa: S608 - the control plane's id
        for purpose, micros, calls in query(
            db, "SELECT purpose, sum(cost_micros), sum(calls) FROM llm_spend GROUP BY purpose"
        ):
            found = ledger.setdefault(purpose, {"usd": 0.0, "calls": 0})
            found["usd"] += int(micros) / 1e6
            found["calls"] += int(calls)
        if since is not None:
            # The only value in the statement is a timestamp this process made.
            runs_sql = (
                "SELECT count(*), count(*) FILTER (WHERE input_tokens > 0), coalesce(sum(input_tokens), 0), "  # noqa: S608
                "coalesce(sum(cached_tokens), 0), coalesce(sum(output_tokens), 0), "
                "coalesce(sum(cost_micros), 0) FROM extraction_runs "
                "WHERE created_at >= '" + since.isoformat() + "'"
            )
            [row] = query(db, runs_sql)
            names = (
                "runs",
                "with_a_model_call",
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "micros",
            )
            runs.update({name: int(value) for name, value in zip(names, row, strict=True)})
    out: dict[str, Any] = {"at": datetime.now(UTC).isoformat(), "ledger": ledger}
    if since is not None:
        spent = runs.pop("micros", 0)
        out["extraction_runs"] = {**{k: runs[k] for k in sorted(runs)}, "usd": spent / 1e6}
    return out


def _p95(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]


def _grades(rows: list[dict[str, Any]], side: str, name: str) -> dict[str, Any]:
    """The judge's and the exact check's share of right answers of one side or reference."""
    return {
        grade: _share(rows, lambda r: r[side][name][grade])  # noqa: B023 - used before the loop moves
        for grade in ("judge", "exact")
    }


def _share(rows: list[dict[str, Any]], pick: Callable[[dict[str, Any]], bool | None]) -> dict[str, Any]:
    """The share of right answers with its 95% Wilson interval. The repetitions of one case are not
    independent draws, so the interval is narrower than a set of new cases would give."""
    graded = [v for v in (pick(r) for r in rows) if v is not None]
    interval = wilson(sum(graded), len(graded))
    return {
        "n": len(graded),
        "correct": sum(graded),
        "share": round(sum(graded) / len(graded), 4) if graded else None,
        "ci95": [round(b, 4) for b in interval] if interval else None,
    }


def valid(row: dict[str, Any]) -> bool:
    """The v2 validity rule: right with the whole history and wrong with no memory."""
    refs = row["references"]
    return refs["full_history"]["judge"] is True and refs["no_memory"]["judge"] is False


def summarize(answered: list[dict[str, Any]]) -> dict[str, Any]:
    """The figures of `typed.json`, per side and category; a case whose read failed is listed apart."""
    rows = [r for r in answered if "arms" in r]
    categories = [c for c in typed.TYPED_CATEGORIES if any(r["category"] == c for r in rows)]
    out: dict[str, Any] = {"accuracy": {}, "tokens": {}, "guard": {}, "v0": {}, "flips": [], "validity": {}}
    out["probe_errors"] = {r["case_id"]: r["probe_error"] for r in answered if "probe_error" in r}
    if not rows:
        return out
    kept = [r for r in rows if valid(r)]
    out["validity"] = {
        "cases": len(rows),
        "valid": len(kept),
        "invalid": sorted(r["case_id"] for r in rows if not valid(r)),
    }
    for scope, pool in (("all", rows), ("valid", kept)):
        table: dict[str, Any] = {}
        for category in [*categories, "all"]:
            chosen = [r for r in pool if category in ("all", r["category"])]
            table[category] = {
                **{arm: _grades(chosen, "arms", arm) for arm in ARMS},
                **{ref: _grades(chosen, "references", ref) for ref in ("no_memory", "full_history")},
            }
        out["accuracy"][scope] = table
    for view in ("voice", "chat"):
        chosen = [r for r in rows if r["view"] == view]
        if not chosen:
            continue
        without = [r["arms"]["without"]["tokens"] for r in chosen]
        with_ = [r["arms"]["with"]["tokens"] for r in chosen]
        added = [r["arms"]["with"]["tokens"] - r["arms"]["without"]["tokens"] for r in chosen]
        section = [r["arms"]["with"]["tokens_section"] for r in chosen]
        tool = [r["arms"]["with"]["tokens_tool_json"] or 0 for r in chosen]
        out["tokens"].setdefault("without_reads", {})[view] = {
            f"{r['case_id']}#{r['repetition']}": r["arms"]["without"]["tokens"] for r in chosen
        }
        out["tokens"][view] = {
            "cases": len(chosen),
            "without": {"median": statistics.median(without), "p95": _p95(without)},
            "with": {"median": statistics.median(with_), "p95": _p95(with_)},
            "added": {"median": statistics.median(added), "p95": _p95(added), "max": max(added)},
            "section": {"median": statistics.median(section), "p95": _p95(section)},
            "tool_json": {"median": statistics.median(tool), "p95": _p95(tool)},
            "no_section": sum(1 for s in section if s == 0),
        }
    for category in [*categories, "all"]:
        chosen = [r for r in rows if category in ("all", r["category"])]
        out["tokens"].setdefault("added_by_category", {})[category] = statistics.median(
            [r["arms"]["with"]["tokens"] - r["arms"]["without"]["tokens"] for r in chosen]
        )
    for arm in ARMS:
        claims = [c for r in rows for c in r["arms"][arm]["guard"]["claims"]]
        acted = [r for r in rows if any(c["action"] in ACTED for c in r["arms"][arm]["guard"]["claims"])]
        out["guard"][arm] = {
            "claims": len(claims),
            "verdicts": dict(sorted(Counter(c["verdict"] for c in claims).items())),
            "actions": dict(sorted(Counter(c["action"] for c in claims).items())),
            "by_category": dict(sorted(Counter(f"{c['category']}|{c['verdict']}" for c in claims).items())),
            "answers_acted_on": len(acted),
            "acted_on_correct": sum(1 for r in acted if r["arms"][arm]["judge"] is True),
            "answers_changed": sum(1 for r in rows if r["arms"][arm]["guard"]["changed"]),
        }
        out["v0"][arm] = {
            "reads": len(rows),
            "effective_v0": sum(1 for r in rows if r["arms"][arm]["v0"]["effective"] == "V0"),
            "sensitive_in_block": sum(1 for r in rows if r["arms"][arm]["v0"]["sensitive_in_block"]),
            "sensitive_in_block_v1": sum(1 for r in rows if r["arms"][arm]["sensitive_in_block"]),
            "proven": sum(1 for r in rows if r["arms"][arm]["effective"] == r.get("level", "V1")),
            "withheld_when_proven": sum(1 for r in rows if r["arms"][arm].get("withheld")),
            "levels": dict(sorted(Counter(r.get("level", "V1") for r in rows).items())),
        }
    for r in rows:
        before, after = r["arms"]["without"]["judge"], r["arms"]["with"]["judge"]
        if before is not None and after is not None and before != after:
            out["flips"].append(
                {
                    "case_id": r["case_id"],
                    "category": r["category"],
                    "without": before,
                    "with": after,
                    "valid": valid(r),
                }
            )
    coordination = [r["coordination"] for r in rows if "coordination" in r]
    if coordination:
        out["coordination"] = {
            "checks": len(coordination),
            "refused_as_done": sum(
                1 for c in coordination if c["decision"] == "deny" and "effect_done" in c["reasons"]
            ),
        }
    return out


def compare(metrics: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Each side's share against a baseline run's, per scope, grade and category, with Newcombe's 95%
    interval of the difference; a category either run lacks is left out."""
    out: dict[str, Any] = {}
    for scope, table in metrics["accuracy"].items():
        before = baseline["accuracy"].get(scope) or {}
        for category, row in table.items():
            if category not in before:
                continue
            for arm in ARMS:
                for grade in ("judge", "exact"):
                    now, was = row[arm][grade], before[category][arm][grade]
                    interval = share_difference(now["correct"], now["n"], was["correct"], was["n"])
                    if interval is None:
                        continue
                    delta = now["correct"] / now["n"] - was["correct"] / was["n"]
                    out.setdefault(scope, {}).setdefault(category, {}).setdefault(arm, {})[grade] = {
                        "now": now["share"],
                        "baseline": was["share"],
                        "delta": round(delta, 4),
                        "ci95": [round(b, 4) for b in interval],
                    }
    return out


def load_baseline(directory: Path) -> dict[str, Any]:
    """A former run's `typed.json`: its id, where it ran, and its metrics."""
    path = directory / "typed.json"
    if not path.exists():
        raise TypedError(f"no typed.json in {directory}")
    return dict(json.loads(path.read_text()))


class TypedAb:
    def __init__(
        self, cases: list[TypedCase], options: TypedOptions, config: BenchConfig | None = None
    ) -> None:
        self.cases = cases
        self.options = options
        self.config = config or bench_config.load()
        self.chat = ChatClient()
        self.run_id = datetime.now(UTC).strftime("%Y-%m-%d") + "-" + uuid.uuid4().hex[:6]

    async def execute(self) -> Path:
        options = self.options
        baseline = load_baseline(options.baseline) if options.baseline else None
        count = _tokenizer(self.config.tokens.encoding)
        cell = Cell(options)
        grader = Grader(self.config, self.chat, options.agent)
        started = datetime.now(UTC)
        languages = sorted({c.language for c in self.cases})
        rows: list[dict[str, Any]] = []
        settles: list[dict[str, Any]] = []
        try:
            setup = await cell.setup(languages)
            space_ids = [cell.spaces[lang].space_id for lang in languages]
            spend_before = cell_spend(options.cell_pg, space_ids)
            for repetition in range(1, options.repetitions + 1):
                tag = f"{self.run_id[-6:]}r{repetition}"
                now = datetime.now(UTC)
                today = now.astimezone(ZoneInfo(SPACE_TIMEZONE)).date()
                subjects = [
                    Subject(c, tag, today, subject_phone(c, tag), options.levels.get(c.sector, "V1"))
                    for c in self.cases
                ]
                failed = await gather_limited(
                    options.concurrency, [seed_or_fail(cell, s, now) for s in subjects]
                )
                seed_failed = {case_id: why for case_id, why in failed if why}
                subjects = [s for s in subjects if s.case.id not in seed_failed]
                settled = await settle(cell, subjects, options.settle_quiet_s, options.settle_timeout_s)
                settles.append({"repetition": repetition, **settled, "seed_failed": seed_failed})
                log.info("repetition %s settled: %s", repetition, settles[-1])
                probed = await gather_limited(
                    options.concurrency, [probe_or_fail(cell, s, grader, count) for s in subjects]
                )
                rows += [{"repetition": repetition, **row} for row in probed]
            sample = (
                await v2_tokens(cell, options.v2_sample, datetime.now(UTC), count)
                if options.v2_sample
                else None
            )
        finally:
            await cell.close()
            await self.chat.close()
        spend_after = cell_spend(options.cell_pg, space_ids, since=started)
        document: dict[str, Any] = {
            "schema": SCHEMA,
            "run_id": self.run_id,
            "environment": "local-cell",
            "started_at": started.isoformat(),
            "ended_at": datetime.now(UTC).isoformat(),
            "dataset": {
                "name": "typed",
                "sha256": _sha(TYPED_DIR / "cases.jsonl"),
                "cases": len(self.cases),
                "generator_version": typed.GENERATOR_VERSION,
            },
            "config": {
                "arms": {arm: list(include) if include else None for arm, include in ARMS.items()},
                "agent": self.config.agent.model_dump(),
                "judge": self.config.judge.model_dump(),
                "agent_mode": options.agent,
                "cell_models": self.config.models.niadra,
                "object_types_sha256": _sha(TYPES_FILE),
                "tokens": self.config.tokens.encoding,
                "repetitions": options.repetitions,
                "levels": {sector: options.levels.get(sector, "V1") for sector in SECTOR_CONTRACT},
            },
            "versions": {
                "niadra_sdk": niadra.__version__,
                "niadra_sdk_commit": _source_commit(Path(niadra.__file__)),
            },
            "setup": setup,
            "settle": settles,
            "metrics": summarize(rows),
            "tokens_without_blocks_v2": sample,
            "cost": {"agent_and_judge": self.chat.usage()},
        }
        if baseline is not None:
            document["baseline"] = {
                "run_id": baseline["run_id"],
                "versions": baseline.get("versions"),
                "repetitions": baseline["config"]["repetitions"],
                "delta": compare(document["metrics"], baseline["metrics"]),
                "tokens": paired_tokens(document, baseline),
            }
        output = options.output or RESULTS / self.run_id
        output.mkdir(parents=True, exist_ok=True)
        (output / "typed.json").write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        with (output / "cases.jsonl").open("w") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        if spend_after is not None:
            cost = cell_cost(spend_before, spend_after)
            (output / "cell-cost.json").write_text(json.dumps(cost, indent=2) + "\n")
            document["cost"]["cell"] = cost["run"]
            (output / "typed.json").write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        (output / "typed.md").write_text(report(document))
        return output


def cell_cost(before: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, Any]:
    """The cell's model spend over the run: the ledger's growth per purpose and the extraction runs."""
    grown = {}
    for purpose, now in after["ledger"].items():
        was = (before or {}).get("ledger", {}).get(purpose, {"usd": 0.0, "calls": 0})
        grown[purpose] = {"usd": round(now["usd"] - was["usd"], 6), "calls": now["calls"] - was["calls"]}
    return {
        "what": "The cell's own model spend over the run (its extraction model and its decision model, "
        "which the ledger books under extraction), read from the local cell's PostgreSQL: the spend ledger "
        "before and after the run, and the extraction runs created during it. On this cell the models run on "
        "the benchmark's OpenRouter key.",
        "snapshots": [(before or {}).get("at"), after["at"]],
        "run": {
            "ledger_usd": round(sum(p["usd"] for p in grown.values()), 6),
            "by_purpose": grown,
            "extraction_runs": after.get("extraction_runs"),
        },
    }


def report(document: dict[str, Any]) -> str:
    """`typed.md`: the tables of `typed.json`."""
    metrics = document["metrics"]
    if "all" not in metrics["accuracy"]:
        errors = [f"- `{k}`: {v}" for k, v in metrics.get("probe_errors", {}).items()]
        return (
            "\n".join([f"# Typed-object set {document['run_id']}", "", "No case was answered.", "", *errors])
            + "\n"
        )
    lines = [
        f"# Typed-object set {document['run_id']}",
        "",
        f"- Where: `{document['environment']}`; {document['dataset']['cases']} cases, "
        f"{document['config']['repetitions']} repetition(s); SDK {document['versions']['niadra_sdk']} "
        f"(source {document['versions'].get('niadra_sdk_commit') or 'release'}).",
        '- `without`: a read with no `include`; `with`: `include: ["state", "constraints"]`, the same '
        "customer.",
        "- Validity rule (right with the whole history, wrong with no memory): "
        f"{metrics['validity']['valid']} of {metrics['validity']['cases']} cases.",
        "- Brackets: the 95% Wilson interval, in percent. The repetitions of a case are not independent "
        "draws, so it is narrower than new cases would give.",
        "",
    ]
    for scope, title in (("all", "every case"), ("valid", "valid cases")):
        for grade in ("judge", "exact"):
            lines += [
                f"## Accuracy by category, {grade}, {title}",
                "",
                "| Category | without | with | no_memory | full_history |",
                "|---|---|---|---|---|",
            ]
            for category, row in metrics["accuracy"][scope].items():
                cells = [_pct(row[name][grade]) for name in ("without", "with", "no_memory", "full_history")]
                lines.append(f"| {category} | " + " | ".join(cells) + " |")
            lines.append("")
    lines += [
        "## Tokens per turn (o200k_base)",
        "",
        "| View | cases | without, median | with, median | added, median (p95) | `<niadra>` section, median "
        "| same data as a tool's JSON, median |",
        "|---|---|---|---|---|---|---|",
    ]
    for view in ("voice", "chat"):
        t = metrics["tokens"].get(view)
        if t:
            lines.append(
                f"| {view} | {t['cases']} | {t['without']['median']} | {t['with']['median']} | "
                f"{t['added']['median']} ({t['added']['p95']}) | {t['section']['median']} | "
                f"{t['tool_json']['median']} |"
            )
    lines += [
        "",
        "Added tokens by category (median): "
        + ", ".join(f"{k} {v}" for k, v in metrics["tokens"]["added_by_category"].items())
        + ".",
        "",
    ]
    sample = document.get("tokens_without_blocks_v2")
    if sample:
        lines += [
            f"## A turn that asks for no block (dataset v2 sample, {sample['cases']} cases)",
            "",
            "| View | median | p95 |",
            "|---|---|---|",
        ]
        lines += [
            f"| {view} | {sample[view]['median']} | {sample[view]['p95']} |" for view in ("voice", "chat")
        ]
        lines.append("")
    lines += _paired_lines((document.get("baseline") or {}).get("tokens"))
    lines += [
        "## Claim guard",
        "",
        "| Side | claims | verdicts | actions | answers acted on | acted on a correct answer |",
        "|---|---|---|---|---|---|",
    ]
    for arm, g in metrics["guard"].items():
        lines.append(
            f"| {arm} | {g['claims']} | {_counts(g['verdicts'])} | {_counts(g['actions'])} | "
            f"{g['answers_acted_on']} | {g['acted_on_correct']} |"
        )
    lines += [
        "",
        "## Reads at V0",
        "",
        "| Side | reads | served at V0 | sensitive value in the block |",
        "|---|---|---|---|",
    ]
    for arm, v in metrics["v0"].items():
        lines.append(f"| {arm} | {v['reads']} | {v['effective_v0']} | {v['sensitive_in_block']} |")
    lines += [
        "",
        "The answered reads, at the level each case proved ("
        + ", ".join(f"{sector} {level}" for sector, level in document["config"].get("levels", {}).items())
        + "), per side: "
        + "; ".join(
            f"{arm} {v.get('proven', '-')} of {v['reads']} served at the level proved, "
            f"{v.get('withheld_when_proven', '-')} with an item withheld, "
            f"{v['sensitive_in_block_v1']} with the sensitive value"
            for arm, v in metrics["v0"].items()
        )
        + ".",
    ]
    if "coordination" in metrics:
        c = metrics["coordination"]
        lines += [
            "",
            f"Coordination: {c['refused_as_done']} of {c['checks']} second attempts refused as done.",
        ]
    lines += ["", "## Cases that changed verdict (judge)", ""]
    lines += [
        f"- `{f['case_id']}` ({f['category']}): without {f['without']}, with {f['with']}"
        f"{'' if f['valid'] else ' (not valid)'}"
        for f in metrics["flips"]
    ] or ["- none"]
    lines += _baseline_lines(document.get("baseline"))
    cost = document["cost"]
    lines += [
        "",
        "## Cost",
        "",
        f"- Agent and judge: US$ {cost['agent_and_judge']['cost_usd']:.4f} "
        f"({cost['agent_and_judge']['calls']} calls).",
    ]
    if "cell" in cost:
        lines.append(f"- The cell's models (ledger): US$ {cost['cell']['ledger_usd']:.4f}.")
    return "\n".join(lines) + "\n"


def _pct(share: dict[str, Any]) -> str:
    if share["share"] is None:
        return "-"
    interval = share.get("ci95")
    bounds = f" [{100 * interval[0]:.0f}, {100 * interval[1]:.0f}]" if interval else ""
    return f"{100 * share['share']:.1f}% ({share['correct']}/{share['n']}){bounds}"


def _points(value: float) -> str:
    return f"{100 * value:+.1f}"


def _baseline_lines(baseline: dict[str, Any] | None) -> list[str]:
    """The delta against a former run, in percentage points, with Newcombe's 95% interval."""
    if not baseline:
        return []
    lines = [
        "",
        f"## Against {baseline['run_id']} ({baseline['repetitions']} repetition(s))",
        "",
        "Percentage points, now minus then, with the 95% interval of the difference (Newcombe). An interval "
        "that crosses 0 does not show a change.",
    ]
    for scope, title in (("all", "every case"), ("valid", "valid cases")):
        table = baseline["delta"].get(scope)
        if not table:
            continue
        lines += [
            "",
            f"### {title}",
            "",
            "| Category | without, judge | with, judge | without, exact | with, exact |",
            "|---|---|---|---|---|",
        ]
        for category, row in table.items():
            cells = []
            for grade in ("judge", "exact"):
                for arm in ARMS:
                    d = row.get(arm, {}).get(grade)
                    cells.append(
                        "-"
                        if d is None
                        else f"{_points(d['delta'])} [{_points(d['ci95'][0])}, {_points(d['ci95'][1])}]"
                    )
            lines.append(f"| {category} | {cells[0]} | {cells[1]} | {cells[2]} | {cells[3]} |")
    return lines


def _paired_lines(tokens: dict[str, Any] | None) -> list[str]:
    """A turn that asks for no block, this run's code against the baseline's on the same subjects."""
    if not tokens:
        return []
    lines = [
        "## A turn that asks for no block, against the baseline's code (paired)",
        "",
        "The same subjects read by both: each pair's change in tokens, the median change with its 95% "
        f"interval. The criterion: the median within {100 * TOKEN_TOLERANCE:.0f}%.",
        "",
        "| Reads | View | pairs | baseline, median | now, median | pairs that changed | median change "
        "| within |",
        "|---|---|---|---|---|---|---|---|",
    ]
    names = {"v2_sample": "dataset v2 sample", "typed_without": "typed cases, `without`"}
    for name, views in tokens.items():
        for view, t in views.items():
            interval = f" [{_points(t['ci95'][0])}, {_points(t['ci95'][1])}]" if t["ci95"] else ""
            within = "yes" if t["within_5pct"] else "no"
            lines.append(
                f"| {names[name]} | {view} | {t['pairs']} | {t['before_median']} | {t['now_median']} | "
                f"{t['changed']} | {_points(t['median_change'])}%{interval} | {within} |"
            )
    return [*lines, ""]


def _counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in counts.items()) or "none"


def _source_commit(module: Path) -> str | None:
    """The commit of the SDK source the run imported, when it is a git checkout (not an installed release)."""
    out = subprocess.run(  # noqa: S603
        ["git", "-C", str(module.parent), "rev-parse", "--short", "HEAD"],  # noqa: S607
        capture_output=True, text=True, timeout=10, check=False,
    )  # fmt: skip
    return out.stdout.strip() or None if out.returncode == 0 and "site-packages" not in str(module) else None


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ok(response: httpx.Response, status: int) -> Any:
    if response.status_code != status:
        request = response.request
        raise TypedError(
            f"{request.method} {request.url.path} -> {response.status_code}: {response.text[:300]}"
        )
    return response.json()


def run(cases: list[TypedCase], options: TypedOptions) -> Path:
    return asyncio.run(TypedAb(cases, options).execute())


def env_bootstrap(directory: str | None) -> Path:
    """The local cell's bootstrap document (`LOCAL_E2E_DIR/bootstrap.json`)."""
    base = Path(directory or os.environ.get("LOCAL_E2E_DIR") or "")
    path = base / "bootstrap.json"
    if not path.exists():
        raise TypedError(f"no bootstrap document at {path}: start the cell with local-e2e.sh --keep")
    return path
