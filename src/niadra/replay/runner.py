"""The replay runner: cases, N executions, the pin check, the assertions and the report (`niadra.replay`)."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from niadra._ids import new_key
from niadra._transport import Request
from niadra.errors import APIError, NiadraError
from niadra.models.results import Context
from niadra.models.turns import TurnPins
from niadra.replay.assertions import Replayed, evaluate
from niadra.replay.playback import BlobError, Mode, Playback, playback
from niadra.turns.capture import TurnFrame
from niadra.turns.claims import check_turn
from niadra.turns.record import SDK
from niadra.turns.record import build as build_record

if TYPE_CHECKING:
    from niadra._async_client import AsyncNiadra
    from niadra._client import Niadra
    from niadra.models.state import ClaimContractSummary

PINS = ("prompts", "corpus_digest", "model", "assembler", "tool_schemas")
POLL = 30.0
"""Seconds a runner waits for a verdict the recorder has not decided yet."""


@dataclass(frozen=True)
class ReplayRun:
    """A run as Niadra judged it: `verdict` is the worst of its scenarios' (`pass`, `flaky`,
    `infrastructure_error`, `pin_mismatch`, `regression`), and `scenarios` holds each one's verdict with the
    statistics of each assertion (the replay spec, 8). A report Niadra refused for a pin that does not match
    is `refused`, with no `run_id`."""

    run_id: str
    status: str
    verdict: str | None
    scenarios: list[dict[str, Any]]

    @classmethod
    def of(cls, data: Mapping[str, Any]) -> ReplayRun:
        summary = data.get("summary") or {}
        return cls(data["run_id"], data["status"], data.get("verdict"), list(summary.get("scenarios", [])))

    @property
    def regressed(self) -> bool:
        return self.verdict == "regression"


@dataclass(frozen=True)
class ReplayInput:
    """What a replayed agent receives: the turn's input (`text`, masked), the conversation before it, the pack
    of the time as a `Context` (None when the record kept only its etag), and the recorded frame."""

    turn_id: str
    kind: str
    text: str | None
    history: list[dict[str, Any]]
    context: Context | None
    record: Mapping[str, Any]
    run: int
    paraphrase: bool = False


Agent = Callable[[ReplayInput], Any]
"""Answers one input: the text the agent emitted (or None), or an awaitable of it."""
Paraphrase = Callable[[str, int], str]
"""A paraphrase of the input for run `n`: intermittent results must hold with other words too."""


def pin_differences(
    recorded: Mapping[str, Any], running: Mapping[str, Any], required: Iterable[str], vary: Iterable[str]
) -> list[dict[str, Any]]:
    """The pins that differ (the replay spec, 4.2): every pin outside `vary` the recording requires, or both
    builds carry."""
    need, free = set(required), set(vary)
    out = []
    for name in PINS:
        if name in free:
            continue
        mine, theirs = recorded.get(name), running.get(name)
        compared = name in need or (mine is not None and theirs is not None)
        if compared and mine != theirs:
            out.append({"name": name, "recorded": mine, "running": theirs})
    return out


@dataclass
class _Plan:
    scenario_ids: list[str]
    runs: int
    mode: Mode
    vary: list[str]
    paraphrase: Paraphrase | None
    results: list[dict[str, Any]] = field(default_factory=list)


class _Runner:
    """The runner, whatever the client: requests go through `send`, a coroutine function."""

    def __init__(
        self,
        agent_factory: Callable[[], Agent],
        build_pins: TurnPins | Mapping[str, Any] | None,
        read: Callable[[str], bytes] | None,
        contract: Callable[[], ClaimContractSummary | None],
    ) -> None:
        self._factory = agent_factory
        pins = (
            build_pins.model_dump(mode="json", by_alias=True, exclude_none=True)
            if isinstance(build_pins, BaseModel)
            else dict(build_pins or {})
        )
        self.build: dict[str, Any] = {"pins": pins, "sdk": SDK}
        self._read = read
        self._contract = contract

    async def run(self, plan: _Plan, send: Callable[[Request], Awaitable[Any]]) -> ReplayRun:
        found = await send(
            Request("GET", "/v1/scenarios", params={"ids": ",".join(plan.scenario_ids), "limit": 50})
        )
        scenarios = {s["scenario_id"]: s for s in found.get("items", [])}
        for scenario_id in plan.scenario_ids:
            scenario = scenarios.get(scenario_id)
            if scenario is None:
                raise NiadraError(f"scenario {scenario_id} was not found")
            await self._scenario(plan, scenario, send)
        body = {
            "scenario_ids": plan.scenario_ids,
            "build": self.build,
            "mode": plan.mode,
            "runs": plan.runs,
            "vary": plan.vary,
            "results": plan.results,
        }
        try:
            answer = await send(Request("POST", "/v1/scenario-runs", json=body, idempotency_key=new_key()))
        except APIError as error:
            if error.code != "pin_mismatch":
                raise
            return _refused(plan)
        run = ReplayRun.of(answer)
        deadline = time.monotonic() + POLL
        while run.status != "done" and time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            run = ReplayRun.of(await send(Request("GET", f"/v1/scenario-runs/{run.run_id}")))
        return run

    async def _scenario(
        self, plan: _Plan, scenario: Mapping[str, Any], send: Callable[[Request], Awaitable[Any]]
    ) -> None:
        cases: list[tuple[str, dict[str, Any] | None, str, str | None]] = []
        for turn_id in scenario["turn_ids"]:
            cases.append(await self._case(plan, scenario["scenario_id"], turn_id, send))
        stopped: set[int] = set()
        for turn_id, case, status, error in cases:
            for run in range(plan.runs):
                if run in stopped:
                    continue
                if case is None:
                    plan.results.append(
                        _result(scenario["scenario_id"], turn_id, None, run, status, error=error)
                    )
                    continue
                result = await self._execute(plan, scenario["scenario_id"], case, run)
                plan.results.append(result)
                # A conversation replays until its first strong divergence.
                if plan.mode == "hermetic_conversation" and result["divergent_calls"]:
                    stopped.add(run)

    async def _case(
        self, plan: _Plan, scenario_id: str, turn_id: str, send: Callable[[Request], Awaitable[Any]]
    ) -> tuple[str, dict[str, Any] | None, str, str | None]:
        body = {"turn_id": turn_id, "scenario_id": scenario_id, "mode": plan.mode, "build": self.build}
        if plan.vary:
            body["vary"] = plan.vary
        try:
            case = await send(Request("POST", "/v1/replay/cases", json=body))
        except APIError as error:
            status = "pin_mismatch" if error.code == "pin_mismatch" else "infrastructure_error"
            return turn_id, None, status, error.code
        except NiadraError as error:
            return turn_id, None, "infrastructure_error", type(error).__name__
        recorded = (case.get("record") or {}).get("build", {}).get("pins", {})
        if pin_differences(recorded, self.build["pins"], case.get("required_pins", ()), plan.vary):
            return turn_id, None, "pin_mismatch", "pin_mismatch"
        return turn_id, case, "completed", None

    async def _execute(self, plan: _Plan, scenario_id: str, case: dict[str, Any], run: int) -> dict[str, Any]:
        record = case["record"]
        try:
            played = playback(record, self._read, case.get("mode", plan.mode))
        except BlobError as error:
            return _result(
                scenario_id, record["turn_id"], case["case_id"], run, "infrastructure_error", error=str(error)
            )
        entry = case.get("input") or {}
        text = entry.get("text")
        rephrased = False
        if plan.paraphrase is not None and text is not None:
            text, rephrased = plan.paraphrase(text, run), True
        frame = TurnFrame(
            None,
            agent=(record.get("agent") or {}).get("name", "agent"),
            kind=record.get("kind", "message"),
            conversation_id=record.get("conversation_id"),
            task_id=record.get("task_id"),
            pins=self.build["pins"],
        )
        frame.playback = played
        frame.flag("synthetic")
        given = ReplayInput(
            record["turn_id"],
            entry.get("kind", record.get("kind", "message")),
            text,
            list(case.get("history") or []),
            played.context,
            record,
            run,
            rephrased,
        )
        started = time.perf_counter()
        try:
            with frame:
                out = self._factory()(given)
                if inspect.isawaitable(out):
                    out = await out
                if isinstance(out, str) and out:
                    played.say(frame, out)
        except Exception as error:
            return _result(
                scenario_id,
                record["turn_id"],
                case["case_id"],
                run,
                "infrastructure_error",
                error=type(error).__name__,
            )
        latency = int((time.perf_counter() - started) * 1000)
        return self._judged(scenario_id, case, run, frame, played, rephrased, latency)

    def _judged(
        self,
        scenario_id: str,
        case: Mapping[str, Any],
        run: int,
        frame: TurnFrame,
        played: Playback,
        rephrased: bool,
        latency: int,
    ) -> dict[str, Any]:
        contract = self._contract()
        replayed = build_record(
            frame, "stored", claims=(lambda f: check_turn(f, contract)) if contract else None
        )
        languages = contract.languages if contract is not None else []
        turn = Replayed(
            replayed,
            "\n".join(played.said),
            frame.value_of,
            done=played.done,
            handoff=played.handoff,
            lang=languages[0] if languages else "pt",
        )
        outcomes = []
        for assertion in case.get("assertions") or []:
            outcome, detail = evaluate(assertion, turn)
            item = {"id": assertion["id"], "kind": assertion["kind"], "outcome": outcome}
            if detail is not None and outcome == "fail":
                item["detail"] = detail
            outcomes.append(item)
        return _result(
            scenario_id,
            case["record"]["turn_id"],
            case["case_id"],
            run,
            "completed",
            paraphrase=rephrased,
            assertions=outcomes,
            divergent=played.divergent,
            latency=latency,
        )


def _refused(plan: _Plan) -> ReplayRun:
    """Niadra refused the report for a pin that does not match: nothing was recorded, and the verdict is the
    runner's own."""
    scenarios = []
    for scenario_id in plan.scenario_ids:
        mine = [r for r in plan.results if r["scenario_id"] == scenario_id]
        scenarios.append(
            {
                "scenario_id": scenario_id,
                "verdict": "pin_mismatch",
                "completed": sum(r["status"] == "completed" for r in mine),
                "infrastructure_errors": sum(r["status"] == "infrastructure_error" for r in mine),
                "pin_mismatches": sum(r["status"] == "pin_mismatch" for r in mine),
            }
        )
    return ReplayRun("", "refused", "pin_mismatch", scenarios)


def _result(
    scenario_id: str,
    turn_id: str,
    case_id: str | None,
    run: int,
    status: str,
    *,
    paraphrase: bool = False,
    assertions: Sequence[Mapping[str, Any]] = (),
    divergent: int = 0,
    latency: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "scenario_id": scenario_id,
        "turn_id": turn_id,
        "run": run,
        "status": status,
        "paraphrase": paraphrase,
        "assertions": list(assertions),
        "divergent_calls": divergent,
    }
    if case_id is not None:
        result["case_id"] = case_id
    if latency is not None:
        result["latency_ms"] = latency
    if error is not None:
        result["error"] = error[:200]
    return result


class Replayer:
    """Runs scenarios with the sync client. `agent_factory()` makes a fresh agent for each execution: a
    callable taking a `ReplayInput` and returning the text it emitted, or a coroutine of it. `build` is the
    build this run runs (`Niadra.build()`); `read(pointer)` reads values kept by pointer, by default the
    client's content resolver."""

    def __init__(
        self,
        niadra: Niadra,
        agent_factory: Callable[[], Agent],
        *,
        build: TurnPins | Mapping[str, Any] | None = None,
        read: Callable[[str], bytes] | None = None,
    ) -> None:
        self._niadra = niadra
        reader = read if read is not None else (niadra.content.read if niadra.content.registered else None)
        self._runner = _Runner(agent_factory, build, _strict(reader), niadra._profile.contract)

    def run(
        self,
        scenario_ids: Sequence[str],
        *,
        runs: int = 5,
        mode: Mode = "hermetic_turn",
        vary: Sequence[str] = (),
        paraphrase: Paraphrase | None = None,
    ) -> ReplayRun:
        """Runs each turn of the scenarios `runs` times and returns the run with its verdict. Raises when
        Niadra refuses the report (a pin that does not match, 422)."""
        plan = _Plan(list(scenario_ids), runs, mode, list(vary), paraphrase)
        transport = self._niadra._transport
        self._niadra._refresh_profile()

        async def send(request: Request) -> Any:
            return await asyncio.to_thread(transport.request, request)

        return asyncio.run(self._runner.run(plan, send))


class AsyncReplayer:
    """`Replayer` for the async client: `await AsyncReplayer(niadra, build_agent, build=...).run(...)`."""

    def __init__(
        self,
        niadra: AsyncNiadra,
        agent_factory: Callable[[], Agent],
        *,
        build: TurnPins | Mapping[str, Any] | None = None,
        read: Callable[[str], bytes] | None = None,
    ) -> None:
        self._niadra = niadra
        reader = read if read is not None else (niadra.content.read if niadra.content.registered else None)
        self._runner = _Runner(agent_factory, build, _strict(reader), niadra._profile.contract)

    async def run(
        self,
        scenario_ids: Sequence[str],
        *,
        runs: int = 5,
        mode: Mode = "hermetic_turn",
        vary: Sequence[str] = (),
        paraphrase: Paraphrase | None = None,
    ) -> ReplayRun:
        plan = _Plan(list(scenario_ids), runs, mode, list(vary), paraphrase)
        await self._niadra._refresh_profile()
        return await self._runner.run(plan, self._niadra._transport.request)


def _strict(read: Callable[[str], bytes | None] | None) -> Callable[[str], bytes] | None:
    """A reader that raises instead of answering None, so a value it could not read is an infrastructure
    error."""
    if read is None:
        return None

    def strict(pointer: str) -> bytes:
        found = read(pointer)
        if found is None:
            raise BlobError("the content resolver could not read a value")
        return found

    return strict
