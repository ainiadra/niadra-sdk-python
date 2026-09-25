"""`bench`: generate and check the dataset, run the benchmark, serve the helper services.

bench prepare [--check]          write dataset/ from the generator (or check the committed copy)
bench run [options]              seed, measure and write results/<date>-<id>/
bench report <results dir>       rebuild summary.json from the repetitions of a run
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
    cases = generate.generate(config.dataset)
    if args.check:
        committed = (bench_config.DATASET_DIR / "cases.jsonl").read_text()
        fresh = "\n".join(case.model_dump_json() for case in cases) + "\n"
        if committed != fresh:
            print("dataset/cases.jsonl differs from what the generator makes; run `bench prepare`")
            return 1
        problems = {c.id: p for c in generate.load(bench_config.DATASET_DIR) if (p := structural_problems(c))}
        if problems:
            print(json.dumps(problems, indent=2))
            return 1
        print(f"dataset ok: {len(cases)} cases, every one passes the structural validity rule")
        return 0
    manifest = generate.write(cases, bench_config.DATASET_DIR, config.dataset)
    print(json.dumps(manifest, indent=2))
    return 0


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
    )
    if args.mock:
        import httpx
        from niadra_mock import MOCK_KEY, MockApp

        mock = MockApp()
        options.niadra_transport = lambda: httpx.ASGITransport(app=mock.asgi)
        options.niadra_base_url = "http://niadra-mock"
        import os

        os.environ.setdefault("NIADRA_API_KEY", MOCK_KEY)
    run = Run(config, load_cases(), options)
    out = asyncio.run(run.execute())
    print(f"results in {out}")
    return 0


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
    run.set_defaults(func=_run)

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
