"""Replay in the emulator (`spec/replay.md`): scenarios from recorded turns, the case a runner asks for with
the pin check, and runs with their verdict, computed as the spec's section 8 says (the regression statistics
vectors run against `scenario_verdict`).

- `POST /v1/scenarios` keeps turns the emulator recorded and suggests assertions by the spec's rules when none
  come; `GET /v1/scenarios` lists them, or `?ids=`; `GET /v1/scenarios/{id}` reads one.
- `POST /v1/replay/cases` answers the case, or 422 `pin_mismatch` with the pins, 422 `not_replayable` with the
  reasons, 404 for a turn it does not know or outside the scenario named.
- `POST /v1/scenario-runs` checks the pins again, and answers the run with its verdict against the latest
  earlier run of each scenario whose verdict was `pass` or `flaky`, or against the recording.

`required_pins` is what the space's recording requires: `model` by default.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import comb
from typing import Any
from uuid import uuid4

from niadra.replay.runner import pin_differences
from niadra_mock.turns import TurnStore

ORDER = ("pass", "flaky", "infrastructure_error", "pin_mismatch", "regression")
KEPT = timedelta(days=180)


class ReplayError(Exception):
    """An answer the routes give as a problem: its status, code and extra members."""

    def __init__(self, status: int, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.extra = extra


def fisher(base_pass: int, base_fail: int, passed: int, failed: int) -> float:
    """One-sided Fisher exact test that the run fails more often than the baseline."""
    total, failures, drawn = base_pass + base_fail + passed + failed, base_fail + failed, passed + failed
    if failures == 0 or drawn == 0:
        return 1.0
    tail = sum(
        comb(failures, x) * comb(total - failures, drawn - x) for x in range(failed, min(failures, drawn) + 1)
    )
    return tail / comb(total, drawn)


def _rate(passed: int, failed: int) -> float:
    return passed / (passed + failed) if passed + failed else 1.0


def assertion_stats(aid: str, outcomes: Sequence[str], baseline: Sequence[int] | None) -> dict[str, Any]:
    passed, failed = outcomes.count("pass"), outcomes.count("fail")
    base_pass, base_fail = (baseline[0], baseline[1]) if baseline is not None else (passed + failed, 0)
    drop = round(_rate(base_pass, base_fail) - _rate(passed, failed), 6)
    p_value = round(fisher(base_pass, base_fail, passed, failed), 6)
    regression = failed >= 2 and drop >= 0.2 and p_value < 0.05
    return {
        "id": aid,
        "passed": passed,
        "failed": failed,
        "not_checked": outcomes.count("not_checked"),
        "baseline_passed": base_pass,
        "baseline_failed": base_fail,
        "drop": drop,
        "p_value": p_value,
        "regression": regression,
        "flaky": not regression and failed >= 1,
    }


def scenario_verdict(
    executions: Iterable[Mapping[str, Any]], baseline: Mapping[str, Sequence[int]] | None
) -> dict[str, Any]:
    """A scenario's verdict from its executions (`{status, paraphrase, outcomes}`) and its baseline."""
    runs = list(executions)
    completed = [e for e in runs if e["status"] == "completed"]
    outcomes: dict[str, list[str]] = {}
    for execution in completed:
        for aid, outcome in execution["outcomes"].items():
            outcomes.setdefault(aid, []).append(outcome)
    assertions = [assertion_stats(a, o, (baseline or {}).get(a)) for a, o in sorted(outcomes.items())]
    mismatches = sum(e["status"] == "pin_mismatch" for e in runs)
    if mismatches:
        verdict = "pin_mismatch"
    elif not completed:
        verdict = "infrastructure_error"
    elif any(a["regression"] for a in assertions):
        verdict = "regression"
    elif any(a["flaky"] for a in assertions):
        verdict = "flaky"
    else:
        verdict = "pass"
    return {
        "verdict": verdict,
        "completed": len(completed),
        "infrastructure_errors": sum(e["status"] == "infrastructure_error" for e in runs),
        "pin_mismatches": mismatches,
        "needs_paraphrase": any(a["passed"] and a["failed"] for a in assertions),
        "assertions": assertions,
    }


@dataclass
class _Run:
    run_id: str
    at: datetime
    summary: dict[str, Any]
    verdict: str


@dataclass
class ReplayStore:
    turns: TurnStore
    history: Callable[[str, datetime], list[dict[str, Any]]]
    """The conversation's public messages before a moment, oldest first, as `{role, text, at}`."""
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    required_pins: tuple[str, ...] = ("model",)
    scenarios: dict[str, dict[str, Any]] = field(default_factory=dict)
    runs: dict[str, _Run] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # Scenarios

    def create(self, body: Mapping[str, Any], origin: str = "manual") -> dict[str, Any]:
        turn_ids = list(body.get("turn_ids") or [])
        records = [self._record(t) for t in turn_ids]
        assertions = list(body.get("assertions") or []) or suggest(records)
        scenario_id = f"sc_{uuid4().hex[:12]}"
        scenario: dict[str, Any] = {
            "scenario_id": scenario_id,
            "name": body.get("name") or "Scenario",
            "origin": origin,
            "turn_ids": turn_ids,
            "assertions": [{"suggested": False, **a} for a in assertions],
            "status": "active",
            "version": 1,
            "created_at": self.clock().isoformat(),
        }
        with self._lock:
            self.scenarios[scenario_id] = scenario
        return scenario

    def get(self, scenario_id: str) -> dict[str, Any]:
        with self._lock:
            found = self.scenarios.get(scenario_id)
        if found is None:
            raise ReplayError(404, "not_found")
        return found

    def listed(self, ids: Sequence[str] | None) -> dict[str, Any]:
        with self._lock:
            items = (
                [self.scenarios[i] for i in ids if i in self.scenarios]
                if ids
                else list(self.scenarios.values())
            )
        return {"items": items, "next_cursor": None}

    # Cases

    def case(self, body: Mapping[str, Any]) -> dict[str, Any]:
        turn_id = body["turn_id"]
        scenario = self.get(body["scenario_id"]) if body.get("scenario_id") else None
        if scenario is not None and turn_id not in scenario["turn_ids"]:
            raise ReplayError(404, "not_found")
        record = self._record(turn_id)
        blockers = _blockers(record, self.required_pins)
        if blockers:
            raise ReplayError(422, "not_replayable", reasons=blockers)
        vary = list(body.get("vary") or [])
        running = (body.get("build") or {}).get("pins") or {}
        pins = pin_differences(record["build"]["pins"], running, self.required_pins, vary)
        if pins:
            raise ReplayError(422, "pin_mismatch", pins=pins)
        started = datetime.fromisoformat(record["started_at"])
        history = self.history(record.get("conversation_id") or "", started)[-50:]
        entry = next((h for h in reversed(history) if h["role"] == "customer"), None)
        assertions = [
            a for a in (scenario or {}).get("assertions", []) if a.get("turn_id") in (None, turn_id)
        ]
        case: dict[str, Any] = {
            "case_id": f"case_{uuid4().hex[:12]}",
            "turn_id": turn_id,
            "mode": body.get("mode") or "hermetic_turn",
            "vary": vary,
            "record": record,
            "required_pins": list(self.required_pins),
            "input": {"kind": record.get("kind", "message"), **({"text": entry["text"]} if entry else {})},
            "history": history,
            "assertions": assertions,
            "expires_at": (self.clock() + KEPT).isoformat(),
        }
        if scenario is not None:
            case["scenario_id"] = scenario["scenario_id"]
        return case

    # Runs

    def run(self, body: Mapping[str, Any]) -> dict[str, Any]:
        running = (body.get("build") or {}).get("pins") or {}
        vary = list(body.get("vary") or [])
        scenario_ids = list(body["scenario_ids"])
        for scenario_id in scenario_ids:
            for turn_id in self.get(scenario_id)["turn_ids"]:
                pins = pin_differences(
                    self._record(turn_id)["build"]["pins"], running, self.required_pins, vary
                )
                if pins:
                    raise ReplayError(422, "pin_mismatch", pins=pins)
        results = list(body["results"])
        scenarios = []
        for scenario_id in scenario_ids:
            executions = [_execution(r) for r in results if r.get("scenario_id") == scenario_id]
            verdict = scenario_verdict(executions, self._baseline(scenario_id))
            scenarios.append({"scenario_id": scenario_id, **verdict})
        worst: str = max((s["verdict"] for s in scenarios), key=ORDER.index, default="pass")
        run_id = f"run_{uuid4().hex[:12]}"
        summary: dict[str, Any] = {"scenarios": scenarios}
        with self._lock:
            self.runs[run_id] = _Run(run_id, self.clock(), summary, worst)
        return {"run_id": run_id, "status": "done", "verdict": worst, "summary": summary}

    def read_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            found = self.runs.get(run_id)
        if found is None:
            raise ReplayError(404, "not_found")
        return {"run_id": found.run_id, "status": "done", "verdict": found.verdict, "summary": found.summary}

    def _baseline(self, scenario_id: str) -> dict[str, list[int]] | None:
        with self._lock:
            runs = sorted(self.runs.values(), key=lambda r: r.at, reverse=True)
        for run in runs:
            for scenario in run.summary["scenarios"]:
                if scenario["scenario_id"] == scenario_id and scenario["verdict"] in ("pass", "flaky"):
                    return {a["id"]: [a["passed"], a["failed"]] for a in scenario["assertions"]}
        return None

    def _record(self, turn_id: str) -> dict[str, Any]:
        stored = self.turns.turns.get(turn_id)
        if stored is None:
            raise ReplayError(404, "not_found")
        return stored.record


def _execution(result: Mapping[str, Any]) -> dict[str, Any]:
    """One result is one execution of one case."""
    return {
        "status": result["status"],
        "paraphrase": bool(result.get("paraphrase", False)),
        "outcomes": {a["id"]: a["outcome"] for a in result.get("assertions", [])},
    }


def _blockers(record: Mapping[str, Any], required: Iterable[str]) -> list[dict[str, str]]:
    reasons = []
    if record.get("content_mode") == "hash_only":
        reasons.append({"reason": "content_mode", "value": "hash_only"})
    if record.get("fidelity", "gold") != "gold":
        reasons.append({"reason": "fidelity", "value": record["fidelity"]})
    if record.get("completeness", "complete") != "complete":
        reasons.append({"reason": "completeness", "value": record["completeness"]})
    pins = record.get("build", {}).get("pins", {})
    reasons += [{"reason": "missing_pin", "value": name} for name in required if name not in pins]
    return reasons


def suggest(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The assertions the recorder suggests from each turn's frame (the replay spec, 3.4)."""
    out: list[dict[str, Any]] = []
    for position, record in enumerate(records, start=1):
        prefix, turn_id = f"t{position}", record["turn_id"]
        tools = [c for c in record.get("calls", []) if c.get("kind") == "tool"]
        for name in sorted({c["name"] for c in tools if c.get("status") == "ok"}):
            out.append(_suggested(f"{prefix}.tool_called.{name}", "tool_called", turn_id, tool=name))
        if any(c.get("applied") for c in tools):
            out.append(_suggested(f"{prefix}.hard_respected", "hard_respected", turn_id))
        if any(c.get("observations") for c in tools):
            out.append(_suggested(f"{prefix}.no_denial_with_results", "no_denial_with_results", turn_id))
        if record.get("claims"):
            out.append(_suggested(f"{prefix}.claims_traced", "claims_traced", turn_id))
            out.append(_suggested(f"{prefix}.claims_match_state", "claims_match_state", turn_id))
        if record.get("effects"):
            out.append(_suggested(f"{prefix}.effect_once", "effect_once", turn_id))
        if (record.get("output") or {}).get("handoff_id"):
            out.append(_suggested(f"{prefix}.handoff_when", "handoff_when", turn_id, expected=True))
        if tools:
            out.append(_suggested(f"{prefix}.budget", "budget", turn_id, max_tool_calls=2 * len(tools)))
    return out


def _suggested(aid: str, kind: str, turn_id: str, **args: Any) -> dict[str, Any]:
    return {"id": aid, "kind": kind, "args": args, "turn_id": turn_id, "suggested": True}
