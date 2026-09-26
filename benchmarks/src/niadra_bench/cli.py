"""`bench`: generate and check the dataset, run the benchmark, serve the helper services.

bench prepare [--check] [--dataset v1|v2]   write dataset/ (v1) and dataset/v2/ from the generator
                                            (or check the committed copies)
bench run [options]              seed, measure and write results/<date>-<id>/ (--dataset v1|v2,
                                 --niadra-memory-v2 on|off)
bench report <results dir>       rebuild summary.json from the repetitions of a run
bench ab [options]               a baseline and a candidate on the same cases, and the delta
                                 (--candidate-env KEY=VALUE, --same, --local-cell <niadra-back>)
bench serve embed-proxy|llm-meter [--port N]    (fake-llm and fake-models: local smoke runs only)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from niadra_bench import config as bench_config
from niadra_bench.dataset import generate
from niadra_bench.dataset.validate import structural_problems
from niadra_bench.runner import METRICS, SYSTEMS, Options, Run, aggregate, load_cases


def _prepare(args: argparse.Namespace) -> int:
    config = bench_config.load()
    versions = bench_config.DATASET_VERSIONS if args.dataset == "all" else (args.dataset,)
    status = 0
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
        memory_v2=None if args.niadra_memory_v2 is None else args.niadra_memory_v2 == "on",
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
    out = asyncio.run(run.execute())
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
    print(f"rewrote {summary_path} from {len(reps)} repetitions")
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
    else:
        # Local smoke runs only: never behind a published number.
        from niadra_bench.services.fakes import FakeLlm, FakeModels

        app = FakeLlm() if args.service == "fake-llm" else FakeModels()
    serve_forever(app, args.host, args.port)
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="bench", description="Niadra's public benchmark against Mem0.")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="generate the dataset")
    prepare.add_argument(
        "--check", action="store_true", help="fail if the committed dataset is stale or invalid"
    )
    prepare.add_argument(
        "--dataset", choices=[*bench_config.DATASET_VERSIONS, "all"], default="all", help="default: all"
    )
    prepare.set_defaults(func=_prepare)

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
        "--dataset",
        choices=bench_config.DATASET_VERSIONS,
        default=bench_config.DEFAULT_DATASET,
        help="dataset version (default: v1, the dataset of the published runs)",
    )
    run.add_argument(
        "--niadra-memory-v2",
        choices=["on", "off"],
        default=None,
        help="set Niadra's memory_v2 space flag for the run through the control API, and put it back "
        "at the end (default: leave the space as it is)",
    )
    run.set_defaults(func=_run)

    ab = sub.add_parser("ab", help="a baseline and a candidate on the same cases, and the delta")
    ab.add_argument(
        "--candidate-env",
        action="append",
        metavar="KEY=VALUE",
        help="a Niadra server setting the candidate runs with (repeatable), e.g. NIADRA_MEMORY_V2=on",
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
    ab.add_argument("--now", default=None, help="a local cell's clock, ISO 8601 (default: this hour)")
    ab.add_argument("--max-minutes", type=float, default=None, help="default: [ab] max_minutes")
    ab.add_argument("--label", default=None, help="a name for the A/B, in the report")
    ab.set_defaults(func=_ab)

    report = sub.add_parser("report", help="rebuild summary.json from a run's repetitions")
    report.add_argument("directory")
    report.set_defaults(func=_report)

    serve = sub.add_parser("serve", help="run a helper service")
    serve.add_argument("service", choices=["embed-proxy", "llm-meter", "fake-llm", "fake-models"])
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - a pod's service port
    serve.add_argument("--port", type=int, default=8080)
    serve.set_defaults(func=_serve)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
