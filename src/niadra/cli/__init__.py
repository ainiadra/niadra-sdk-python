"""The `niadra` command, for the company's own infrastructure and CI.

    niadra resolver-worker --resolvers company.pricing:resolvers
    niadra replay --agent company.agent:build_agent --build company.agent:BUILD --scenario sc_1 --runs 5
    niadra types derive --dsn postgresql://reader@replica/erp --table public.orders --out order.json
    niadra contract test --contract claim-contract.json --examples tests/claims/examples.json
    niadra counterfactual --tools company.tools:TOOLS --tool search_products --element hard --scenario sc_1

The commands that talk to Niadra read the key from `NIADRA_API_KEY` (and `NIADRA_BASE_URL`, when set).
`module:name` names a Python object: for `--resolvers`, a `Resolvers` or a function that registers the
resolvers on the one it gets; for `--agent`, a function that makes a fresh agent; for `--build`, the build the
run runs (`Niadra.build()`, or a mapping of its pins); for `--tools`, a mapping of tool names to the company's
functions, and for `--bindings`, one of tool names to their bindings.

`niadra replay` exits with 0 when the verdict is `pass` or `flaky`, 1 for `regression`, and 2 for
`pin_mismatch` or `infrastructure_error`. `niadra types derive --check` and `niadra contract test` exit with 0
when everything holds, 1 when it does not (drift, a phrase that triggers) and 2 when they could not run: see
`niadra.cli.types` and `niadra.cli.contract`. `niadra counterfactual` prints the report Niadra answered and
exits with 0, or 2 when it could not run.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import sys
from collections.abc import Sequence
from typing import Any

from niadra._client import Niadra
from niadra.cli import contract, types
from niadra.resolvers import Resolvers

EXIT = {"pass": 0, "flaky": 0, "regression": 1}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="niadra", description="Niadra's command line.")
    commands = parser.add_subparsers(dest="command", required=True)
    worker = commands.add_parser("resolver-worker", help="serve the space's refresh requests")
    worker.add_argument("--resolvers", required=True, help="module:name of the resolvers")
    worker.add_argument("--once", action="store_true", help="serve what waits now, then stop")
    worker.add_argument("--limit", type=int, default=50, help="requests leased at a time")
    worker.add_argument("--poll", type=float, default=2.0, help="seconds between polls when none wait")
    replay = commands.add_parser("replay", help="run scenarios again and print the verdict")
    replay.add_argument("--agent", required=True, help="module:name of the function that makes an agent")
    replay.add_argument("--build", required=True, help="module:name of the build the run runs")
    replay.add_argument("--scenario", action="append", required=True, help="a scenario id; repeat for more")
    replay.add_argument("--runs", type=int, default=5)
    replay.add_argument(
        "--mode", default="hermetic_turn", choices=["hermetic_turn", "hermetic_conversation", "era_memory"]
    )
    replay.add_argument("--vary", action="append", default=[], help="a pin the run changes on purpose")
    counterfactual = commands.add_parser(
        "counterfactual", help="a tool's recorded calls with and without an element"
    )
    counterfactual.add_argument("--tools", required=True, help="module:name of the tools, by name")
    counterfactual.add_argument("--tool", required=True, help="the tool to run")
    counterfactual.add_argument(
        "--element", required=True, choices=["constraints", "hard", "size", "exclude"]
    )
    counterfactual.add_argument(
        "--turn", action="append", default=[], help="a recorded turn id; repeat for more"
    )
    counterfactual.add_argument("--scenario", action="append", default=[], help="a scenario's turns")
    counterfactual.add_argument("--bindings", help="module:name of the tools' bindings, by name")
    counterfactual.add_argument(
        "--safe", action="append", default=[], help="a tool that may run again as it is"
    )
    counterfactual.add_argument("--k", type=int, default=10)
    counterfactual.add_argument("--label", help="a name for the run, such as the commit")
    types.add(commands)
    contract.add(commands)
    args = parser.parse_args(argv)
    clients: list[Niadra] = []

    def client() -> Niadra:
        if not clients:
            clients.append(Niadra())
        return clients[0]

    try:
        if args.command == "types":
            return types.run(args, client)
        if args.command == "contract":
            return contract.run(args, client)
        if args.command == "resolver-worker":
            return _worker(client(), args)
        if args.command == "counterfactual":
            return _counterfactual(client(), args)
        return _replay(client(), args)
    finally:
        for niadra in clients:
            niadra.close()


def _worker(niadra: Niadra, args: argparse.Namespace) -> int:
    from niadra.cli.worker import ResolverWorker

    found = _load(args.resolvers)
    if isinstance(found, Resolvers):
        niadra.resolvers = found
    else:
        found(niadra.resolvers)
    worker = ResolverWorker(niadra, limit=args.limit, poll=args.poll)
    if args.once:
        print(f"pushed {worker.run_once()} objects")
        return 0
    with contextlib.suppress(KeyboardInterrupt):
        worker.run()
    return 0


def _replay(niadra: Niadra, args: argparse.Namespace) -> int:
    from niadra.replay import Replayer

    run = Replayer(niadra, _load(args.agent), build=_load(args.build)).run(
        args.scenario, runs=args.runs, mode=args.mode, vary=args.vary
    )
    print(json.dumps({"run_id": run.run_id, "verdict": run.verdict, "scenarios": run.scenarios}, indent=2))
    return EXIT.get(run.verdict or "", 2)


def _counterfactual(niadra: Niadra, args: argparse.Namespace) -> int:
    from niadra.errors import NiadraError
    from niadra.replay import Counterfactual

    if not args.turn and not args.scenario:
        print("niadra: --turn or --scenario is needed", file=sys.stderr)
        return 2
    bindings = _load(args.bindings) if args.bindings else None
    runner = Counterfactual(niadra, _load(args.tools), bindings=bindings, safe=args.safe)
    try:
        run = runner.run(
            args.turn,
            tool=args.tool,
            element=args.element,
            scenario_ids=args.scenario,
            k=args.k,
            label=args.label,
        )
    except NiadraError as error:
        print(f"niadra: {error}", file=sys.stderr)
        return 2
    print(json.dumps({**run.report, "untouched": run.untouched, "unread": run.unread}, indent=2))
    return 0


def _load(target: str) -> Any:
    module, _, name = target.partition(":")
    if not module or not name:
        raise SystemExit(f"{target!r}: expected module:name")
    sys.path.insert(0, ".")
    return getattr(importlib.import_module(module), name)
