"""``market-pulse`` console entry point — thin wrappers over the module mains.

    market-pulse init        create/verify the DuckDB schema
    market-pulse ingest      one ingestion pass into Bronze
    market-pulse backfill    pull real intraday history
    market-pulse transform   Bronze -> Silver -> Gold
    market-pulse analyse     anomaly scan + LLM analyst
    market-pulse run         one full pipeline cycle
    market-pulse dashboard   serve the dashboard
    market-pulse status      row counts and last run
"""

from __future__ import annotations

import argparse
import sys


def _init(_args) -> int:
    from .db import init

    init.main()
    return 0


def _ingest(_args) -> int:
    from .ingestion import fetch_news, fetch_quotes, fetch_sentiment_index
    from .utils import new_run_id

    run_id = new_run_id()
    for result in (fetch_quotes(run_id), fetch_news(run_id), fetch_sentiment_index(run_id)):
        print(f"  {result}")
    return 0


def _backfill(args) -> int:
    from .ingestion import backfill_history
    from .utils import new_run_id

    print(f"  {backfill_history(new_run_id(), period=args.period, interval=args.interval)}")
    return 0


def _transform(_args) -> int:
    from .transforms import run_transforms

    run_transforms.main()
    return 0


def _analyse(_args) -> int:
    from .intelligence import anomaly_detector, llm_analyst
    from .utils import new_run_id

    run_id = new_run_id()
    anomalies = anomaly_detector.detect(run_id)
    result = llm_analyst.run(run_id)
    print(f"\n  {len(anomalies):,} anomalies · {result.articles_scored} articles scored "
          f"via {result.provider}\n")
    if result.narrative:
        print(f"  {result.narrative['headline']}\n  {result.narrative['narrative']}\n")
    return 0


def _run(_args) -> int:
    from .pipeline.orchestrator import main as run_cycle

    run_cycle()
    return 0


def _dashboard(_args) -> int:
    from .dashboard.app import main as serve

    serve()
    return 0


def _status(_args) -> int:
    from .db.init import row_counts
    from .dashboard.queries import pipeline_health
    from .utils import humanise_age

    print("\n  Layer row counts\n")
    for table, count in row_counts().items():
        print(f"    {table:<34} {count:>10,}")
    health = pipeline_health()
    print(f"\n  Last run: {health['status']} ({humanise_age(health['last_run_at'])}) "
          f"· {health['runs']:,} total runs · {health['failures']:,} failed\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="market-pulse", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, handler, help_text in (
        ("init", _init, "create or verify the DuckDB schema"),
        ("ingest", _ingest, "one ingestion pass into Bronze"),
        ("transform", _transform, "Bronze -> Silver -> Gold"),
        ("analyse", _analyse, "anomaly scan and LLM analyst"),
        ("run", _run, "one full pipeline cycle"),
        ("dashboard", _dashboard, "serve the dashboard"),
        ("status", _status, "row counts and last run"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.set_defaults(handler=handler)

    backfill = subparsers.add_parser("backfill", help="pull real intraday history")
    backfill.add_argument("--period", default="5d")
    backfill.add_argument("--interval", default="5m")
    backfill.set_defaults(handler=_backfill)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
