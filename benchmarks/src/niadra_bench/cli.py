"""`bench`: generate and check the dataset, run the benchmark, serve the helper services.

bench prepare [--check] [--dataset v1|v2]   write dataset/ (v1) and dataset/v2/ from the generator
                                            (or check the committed copies)
bench run [options]              seed, measure and write results/<date>-<id>/ (--dataset v1|v2)
bench report <results dir>       rebuild summary.json and summary.md from the repetitions of a run
bench ab [options]               a baseline and a candidate on the same cases, and the delta
                                 (--candidate-env KEY=VALUE, --same, --local-cell <niadra-back>)
bench typed [options]            the typed-object set on a running cell, with and without the blocks a
                                 read asks for (`include`)
bench combine <dir> <dir> ...    one results folder from runs of different systems (the temporary host
                                 runs one system at a time); the first folder's references decide validity
bench systems [--compose]        the systems added through adapters and their deploy/systems directory
bench serve embed-proxy|llm-meter|agentcore-proxy [--port N]    (fake-*: local smoke runs only)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from niadra_bench import config as bench_config
from niadra_bench.dataset import generate
from niadra_bench.dataset.typed import TYPED_CATEGORIES
from niadra_bench.dataset.validate import structural_problems
from niadra_bench.runner import CAPPED_LOOPS, METRICS, SYSTEMS, Options, Run, aggregate, load_cases


def _prepare(args: argparse.Namespace) -> int:
    config = bench_config.load()
    versions: tuple[str, ...] = (
        bench_config.DATASET_VERSIONS
        if args.dataset == "all"
        else ()
        if args.dataset == "typed"
        else (args.dataset,)
    )
    status = _prepare_typed(args.check) if args.dataset in ("all", "typed") else 0
    for version in versions:
        settings = bench_config.dataset_settings(config, version)
        directory = bench_config.dataset_dir(version)
        cases = generate.generate(settings)
        name = directory.relative_to(bench_config.ROOT)
        if args.check:
            path = directory / "cases.jsonl"
            committed = path.read_text() if path.exists() else ""
            fresh = "\n".join(case.model_dump_json() for case in cases) + "\n"
            if committed != fresh:
                print(f"{name}/cases.jsonl differs from what the generator makes; run `bench prepare`")
                status = 1
                continue
            problems = {c.id: p for c in generate.load(directory) if (p := structural_problems(c))}
            if problems:
                print(json.dumps(problems, indent=2))
                status = 1
                continue
            print(f"{name} ({version}) ok: {len(cases)} cases, every one passes the structural validity rule")
            continue
        manifest = generate.write(cases, directory, settings, generate.GENERATOR_VERSIONS[version])
        print(json.dumps({"dataset": version, **manifest}, indent=2))
    return status


async def until_stopped[T](work: Coroutine[Any, Any, T]) -> T:
    """Runs `work`, turning SIGTERM and SIGHUP (a stopped container or pod, a closed session) into a
    cancellation, so its `finally` blocks run: the run's billing key is revoked and a space flag it set
    is put back even when the run is stopped. SIGINT already cancels under asyncio.run."""
    task = asyncio.ensure_future(work)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, task.cancel)
    try:
        return await task
    finally:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.remove_signal_handler(sig)


def _prepare_typed(check: bool) -> int:
    """The typed-object set (`dataset/typed/`): written from its templates, or checked against them."""
    from niadra_bench.dataset import typed

    directory = bench_config.DATASET_DIR / "typed"
    cases = typed.generate()
    name = directory.relative_to(bench_config.ROOT)
    if not check:
        print(json.dumps({"dataset": "typed", **typed.write(cases, directory)}, indent=2))
        return 0
    path = directory / "cases.jsonl"
    fresh = "\n".join(case.model_dump_json() for case in cases) + "\n"
    if not path.exists() or path.read_text() != fresh:
        print(
            f"{name}/cases.jsonl differs from what the generator makes; run `bench prepare --dataset typed`"
        )
        return 1
    problems = {c.id: p for c in typed.load(directory) if (p := typed.problems(c))}
    if problems:
        print(json.dumps(problems, indent=2))
        return 1
    print(f"{name} (typed) ok: {len(cases)} cases, every one passes the structural validity rule")
    return 0


def _typed(args: argparse.Namespace) -> int:
    from niadra_bench import typed_ab

    try:
        bootstrap = Path(args.bootstrap) if args.bootstrap else typed_ab.env_bootstrap(args.cell_dir)
        options = typed_ab.TypedOptions(
            api=args.api,
            control=args.control,
            bootstrap=bootstrap,
            output=Path(args.output) if args.output else None,
            limit=args.limit,
            languages=tuple(lang for lang in ("pt", "en") if lang in _csv(args.languages, ("pt", "en"))),
            categories=tuple(c for c in TYPED_CATEGORIES if c in _csv(args.categories, TYPED_CATEGORIES)),
            repetitions=args.repetitions,
            v2_sample=args.v2_sample,
            cell_pg=args.cell_pg,
            agent=args.agent,
            baseline=Path(args.baseline) if args.baseline else None,
            levels=_levels(args.verify_level),
            derived_text=args.derived_text,
        )
        if args.agent == "llm":
            from niadra_bench import ab

            ab.bench_key()
        cases = typed_ab.select(
            typed_ab.load_cases(),
            limit=args.limit,
            languages=options.languages,
            categories=options.categories,
        )
        out = typed_ab.run(cases, options)
    except (typed_ab.TypedError, ValueError) as exc:
        print(f"bench typed: {exc}", file=sys.stderr)
        return 2
    print((out / "typed.md").read_text())
    print(f"results in {out}")
    return 0


def _levels(values: list[str] | None) -> dict[str, str]:
    """`sector=LEVEL` pairs: the level each sector's probe proves."""
    from niadra_bench import typed_ab

    out: dict[str, str] = {}
    for value in values or ():
        sector, _, level = value.partition("=")
        if sector not in typed_ab.SECTOR_CONTRACT or level not in typed_ab.LEVELS:
            raise ValueError(
                f"--verify-level {value}: expected <sector>=<level>, a sector of "
                f"{', '.join(typed_ab.SECTOR_CONTRACT)} and a level of {', '.join(typed_ab.LEVELS)}"
            )
        out[sector] = level
    return out


def _csv(value: str, allowed: tuple[str, ...]) -> set[str]:
    chosen = {v.strip() for v in value.split(",") if v.strip()}
    unknown = chosen - set(allowed)
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown: {', '.join(sorted(unknown))}; choose from {', '.join(allowed)}"
        )
    return chosen


def _run(args: argparse.Namespace) -> int:
    config = bench_config.load()
    options = Options(
        systems=_csv(args.systems, SYSTEMS),
        metrics=_csv(args.metrics, METRICS),
        repetitions=args.repetitions or config.run.repetitions,
        dry_run=args.dry_run or args.mock,
        limit=args.limit,
        quick=args.quick,
        output=Path(args.output) if args.output else None,
        dataset=args.dataset,
        niadra_record_answers=args.niadra_guards == "measure",
        references=not args.no_references,
        max_settle_s=args.max_settle_s,
        lifted_caps=frozenset(_csv(args.lift_caps, CAPPED_LOOPS)) if args.lift_caps else frozenset(),
    )
    if args.mock:
        import httpx
        from niadra_mock import MOCK_KEY, MockApp

        mock = MockApp()
        options.niadra_transport = lambda: httpx.ASGITransport(app=mock.asgi)
        options.niadra_base_url = "http://niadra-mock"
        import os

        os.environ.setdefault("NIADRA_API_KEY", MOCK_KEY)
    run = Run(config, load_cases(args.dataset), options)
    try:
        out = asyncio.run(until_stopped(run.execute()))
    except asyncio.CancelledError:
        print("stopped: every target was closed (billing key revoked, flags put back)", file=sys.stderr)
        return 130
    print(f"results in {out}")
    return 0


def _ab(args: argparse.Namespace) -> int:
    from datetime import datetime

    from niadra_bench import ab

    try:
        options = ab.AbOptions(
            baseline_env=ab.parse_env(args.baseline_env),
            candidate_env=ab.parse_env(args.candidate_env),
            same=args.same,
            candidate_config=Path(args.candidate_config) if args.candidate_config else None,
            dataset=args.dataset,
            metrics=_csv(args.metrics, METRICS),
            repetitions=args.repetitions,
            limit=args.limit,
            quick=args.quick,
            agent=args.agent,
            local_cell=Path(args.local_cell).resolve() if args.local_cell else None,
            candidate_cell=Path(args.candidate_cell).resolve() if args.candidate_cell else None,
            real_models=args.real_models,
            model_cache=not args.no_model_cache,
            mock=args.mock,
            output=Path(args.output) if args.output else None,
            now=datetime.fromisoformat(args.now) if args.now else None,
            max_minutes=args.max_minutes,
            label=args.label,
        )
        runner = ab.Ab(load_cases(args.dataset), options)
        out = asyncio.run(runner.execute())
    except ab.AbError as exc:
        print(f"bench ab: {exc}", file=sys.stderr)
        return 2
    document = json.loads((out / "ab.json").read_text())
    print((out / "ab.md").read_text())
    print(f"results in {out}")
    verdict = document.get("determinism")
    return 1 if verdict is not None and not verdict["passed"] else 0


def _report(args: argparse.Namespace) -> int:
    directory = Path(args.directory)
    summary_path = directory / "summary.json"
    summary = json.loads(summary_path.read_text())
    reps = [json.loads(p.read_text()) for p in sorted(directory.glob("rep-*.json"))]
    summary["metrics"] = aggregate(reps)
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    from niadra_bench import report_summary

    (directory / "summary.md").write_text(report_summary.markdown(summary, reps))
    print(f"rewrote {summary_path} and summary.md from {len(reps)} repetitions")
    return 0


def _combine(args: argparse.Namespace) -> int:
    from niadra_bench.combine import combine

    directories = [Path(d) for d in args.directories]
    first = json.loads((directories[0] / "summary.json").read_text())
    cases = {c.id: c for c in load_cases(first["dataset"].get("version", bench_config.DEFAULT_DATASET))}
    output = Path(args.output) if args.output else bench_config.RESULTS_DIR
    references = Path(args.references) if args.references else None
    target = combine(directories, output, cases, references)
    summary = json.loads((target / "summary.json").read_text())
    print(f"combined into {target}")
    print(f"validity from the references of {summary['dataset']['references']['run_id']}")
    shared = summary["dataset"]["shared"]
    print(f"cases every system answered: {shared['cases']}, valid {shared['valid']['median']}")
    for system, source in summary["per_system"].items():
        reps, count = source["repetitions"], source["cases"]
        print(f"  {system}: {reps} repetition(s), {count} cases ({source['run_id']})")
    return 0


def _stack(args: argparse.Namespace) -> int:
    from niadra_bench.stack import stack

    output = Path(args.output) if args.output else bench_config.RESULTS_DIR
    print(f"stacked into {stack([Path(d) for d in args.directories], output)}")
    return 0


def _systems(args: argparse.Namespace) -> int:
    """The systems added through adapters, one per line: key, deploy/systems directory, name."""
    from niadra_bench.systems import REGISTRY

    for key, adapter in REGISTRY.items():
        if args.compose:
            print(f"{key} {adapter.compose}")
        else:
            print(f"{key}\t{adapter.compose}\t{adapter.title} {adapter.version}")
    return 0


def _serve(args: argparse.Namespace) -> int:
    from niadra_bench.services.asgi import App
    from niadra_bench.services.serve import serve_forever

    app: App
    if args.service == "embed-proxy":
        from niadra_bench.services.embed_proxy import EmbedProxy

        app = EmbedProxy()
    elif args.service == "llm-meter":
        from niadra_bench.services.llm_meter import LlmMeter

        app = LlmMeter()
    elif args.service == "agentcore-proxy":
        from niadra_bench.services.agentcore_proxy import AgentCoreProxy

        app = AgentCoreProxy()
    elif args.service == "fake-agentcore":
        # Local smoke runs only: never behind a published number.
        from niadra_bench.services.fakes import FakeAgentCore

        app = FakeAgentCore()
    elif args.service == "forward":
        # A plain forwarder (the fault proxy with no fault) to FORWARD_UPSTREAM: for a server that only
        # answers requests from its own host (deploy/systems/supermemory).
        import os

        from niadra_bench.services.fault_proxy import Fault, FaultProxy

        app = FaultProxy(os.environ["FORWARD_UPSTREAM"], Fault())
    else:
        # Local smoke runs only: never behind a published number.
        from niadra_bench.services.fakes import FakeLlm, FakeModels

        app = FakeLlm() if args.service == "fake-llm" else FakeModels()
    serve_forever(app, args.host, args.port)
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="bench", description="Niadra's public benchmark against Mem0 and other memory systems."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="generate the dataset")
    prepare.add_argument(
        "--check", action="store_true", help="fail if the committed dataset is stale or invalid"
    )
    prepare.add_argument(
        "--dataset",
        choices=[*bench_config.DATASET_VERSIONS, "typed", "all"],
        default="all",
        help="default: all",
    )
    prepare.set_defaults(func=_prepare)

    typed_run = sub.add_parser(
        "typed", help="the typed-object set on a running cell, with and without the blocks of `include`"
    )
    typed_run.add_argument("--api", required=True, help="the cell's data API (local-e2e: 127.0.0.1:<port+9>)")
    typed_run.add_argument("--control", required=True, help="the control plane (local-e2e: 127.0.0.1:<port>)")
    typed_run.add_argument(
        "--bootstrap", help="the sandbox's bootstrap.json (default: <--cell-dir>/bootstrap.json)"
    )
    typed_run.add_argument("--cell-dir", help="the local cell's LOCAL_E2E_DIR (default: $LOCAL_E2E_DIR)")
    typed_run.add_argument("--cell-pg", help="libpq settings of the cell's PostgreSQL, for its model spend")
    typed_run.add_argument("--limit", type=int, help="cases spread over the set")
    typed_run.add_argument("--languages", default="pt,en")
    typed_run.add_argument("--categories", default=",".join(TYPED_CATEGORIES))
    typed_run.add_argument("--repetitions", type=int, default=1)
    typed_run.add_argument("--v2-sample", type=int, default=0, help="dataset v2 cases read with no block")
    typed_run.add_argument("--agent", choices=["llm", "context"], default="llm")
    typed_run.add_argument("--output", help="default: results/typed/<date>-<id>")
    typed_run.add_argument("--baseline", help="a former run's results folder, to report the delta against")
    typed_run.add_argument(
        "--derived-text",
        action="store_true",
        help="turn on the object-types document's derived_fields_in_text: the state lines say derived fields",
    )
    typed_run.add_argument(
        "--verify-level",
        action="append",
        help="<sector>=<level>: the level that sector's probe proves (default V1); repeatable",
    )
    typed_run.set_defaults(func=_typed)

    run = sub.add_parser("run", help="seed every system and measure")
    run.add_argument("--systems", default="niadra,mem0_oss", help=f"comma list of {', '.join(SYSTEMS)}")
    run.add_argument("--metrics", default=",".join(METRICS), help=f"comma list of {', '.join(METRICS)}")
    run.add_argument("--repetitions", type=int, default=None)
    run.add_argument("--limit", type=int, default=None, help="only the first N cases (smoke runs)")
    run.add_argument(
        "--quick", action="store_true", help="short latency, freshness and resilience (smoke runs)"
    )
    run.add_argument(
        "--dry-run", action="store_true", help="no model calls: the agent answers with the memory block"
    )
    run.add_argument(
        "--mock", action="store_true", help="Niadra is the in-process niadra-mock (implies --dry-run)"
    )
    run.add_argument("--output", default=None, help="results directory (default: results/)")
    run.add_argument(
        "--no-references",
        action="store_true",
        help="skip the two references (no memory, full history): for a run that `bench combine` puts "
        "after one that asked them, whose references decide validity for every system",
    )
    run.add_argument(
        "--dataset",
        choices=bench_config.DATASET_VERSIONS,
        default=bench_config.DEFAULT_DATASET,
        help="dataset version (default: v1, the dataset of the published runs)",
    )
    run.add_argument(
        "--niadra-guards",
        choices=["off", "measure"],
        default="off",
        help="measure: record every Niadra probe answer back to it as the agent's message, so its "
        "measurement counts the values agents contradicted and the server writes guard lines in later reads",
    )
    run.add_argument(
        "--max-settle-s",
        type=float,
        default=None,
        help="the longest an added system's settle may wait (a campaign's wall-clock cap); a settle cut "
        "short is recorded as settled: false and the run scores what the system has",
    )
    run.add_argument(
        "--lift-caps",
        default=None,
        help=f"comma list of {', '.join(CAPPED_LOOPS)}: Niadra's timed loops at their own section's rates, "
        "past the cap of config [production], for a temporary cell measured beside a campaign whose "
        "frozen configuration capped them; recorded as config.lifted_caps",
    )
    run.set_defaults(func=_run)

    ab = sub.add_parser("ab", help="a baseline and a candidate on the same cases, and the delta")
    ab.add_argument(
        "--candidate-env",
        action="append",
        metavar="KEY=VALUE",
        help="a Niadra server setting the candidate runs with (repeatable), "
        "e.g. NIADRA_SEMANTIC_CHANNEL=models",
    )
    ab.add_argument(
        "--baseline-env",
        action="append",
        metavar="KEY=VALUE",
        help="a setting of the baseline (default: the setting's default for every key the candidate sets)",
    )
    ab.add_argument(
        "--candidate-config", default=None, help="a TOML file whose sections override benchmark.toml's"
    )
    ab.add_argument(
        "--same", action="store_true", help="determinism check: the candidate is the baseline again"
    )
    ab.add_argument(
        "--local-cell",
        default=None,
        metavar="NIADRA_BACK",
        help="run each side on a local cell of this niadra-back checkout (deploy/local/cell_server.py)",
    )
    ab.add_argument(
        "--candidate-cell",
        default=None,
        metavar="NIADRA_BACK",
        help="the candidate's own niadra-back checkout (a branch), beside --local-cell's for the baseline",
    )
    ab.add_argument(
        "--real-models",
        action="store_true",
        help="with --local-cell: extract and decide with production's Luna and Jev over OpenRouter (the "
        "benchmark's key from Secrets Manager), caching every answer under results/local/model-cache/",
    )
    ab.add_argument(
        "--no-model-cache",
        action="store_true",
        help="with --real-models: ask the provider every time, and read or write no cached answer",
    )
    ab.add_argument("--mock", action="store_true", help="both sides are niadra-mock (only with --same)")
    ab.add_argument(
        "--agent",
        choices=["context", "llm"],
        default=None,
        help="context: the memory block is the answer, no model; llm: the benchmark's agent and judge "
        "(default: context with --local-cell or --mock, else llm)",
    )
    ab.add_argument(
        "--dataset",
        choices=bench_config.DATASET_VERSIONS,
        default=bench_config.DEFAULT_DATASET,
        help="dataset version (default: v1)",
    )
    ab.add_argument(
        "--metrics",
        default="accuracy,tokens,privacy,cost,latency,history,ingest",
        help=f"comma list of {', '.join(METRICS)}",
    )
    ab.add_argument("--repetitions", type=int, default=None)
    ab.add_argument("--limit", type=int, default=None, help="only N cases spread over the dataset")
    ab.add_argument("--quick", action="store_true", help="short latency lines (smoke runs)")
    ab.add_argument("--output", default=None, help="results root (default: results/ab, or results/local/ab)")
    ab.add_argument(
        "--now", default=None, help="the instant both sides seed from, ISO 8601 (default: this hour)"
    )
    ab.add_argument("--max-minutes", type=float, default=None, help="default: [ab] max_minutes")
    ab.add_argument("--label", default=None, help="a name for the A/B, in the report")
    ab.set_defaults(func=_ab)

    report = sub.add_parser("report", help="rebuild summary.json and summary.md from a run's repetitions")
    report.add_argument("directory")
    report.set_defaults(func=_report)

    combined = sub.add_parser("combine", help="one results folder from runs of different systems")
    combined.add_argument("directories", nargs="+", help="results folders; the first one's references count")
    combined.add_argument(
        "--references",
        default=None,
        help="the folder whose references decide validity, instead of the first folder's",
    )
    combined.add_argument("--output", default=None, help="where the new folder goes (default: results/)")
    combined.set_defaults(func=_combine)

    stacked = sub.add_parser(
        "stack",
        help="one folder from separate runs of the same systems, their repetitions one after the other",
    )
    stacked.add_argument("directories", nargs="+", help="results folders, in repetition order")
    stacked.add_argument("--output", default=None, help="where the new folder goes (default: results/)")
    stacked.set_defaults(func=_stack)

    systems = sub.add_parser("systems", help="list the systems added through adapters")
    systems.add_argument("--compose", action="store_true", help="key and compose directory only")
    systems.set_defaults(func=_systems)

    serve = sub.add_parser("serve", help="run a helper service")
    serve.add_argument(
        "service",
        choices=[
            "embed-proxy",
            "llm-meter",
            "forward",
            "agentcore-proxy",
            "fake-llm",
            "fake-models",
            "fake-agentcore",
        ],
    )
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - a pod's service port
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(func=_serve)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
