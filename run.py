#!/usr/bin/env python3
"""Market Pulse Engine — single entry point.

    python run.py

Starts the scheduler and the dashboard **in one process**, deliberately: DuckDB
is an embedded database and a file may be held read-write by only one process,
so the writer (the pipeline) and the reader (the dashboard) share a single
database instance, with the scheduler on a background thread.

Options::

    python run.py                 # pipeline + dashboard (the normal case)
    python run.py --once          # one pipeline cycle, then exit
    python run.py --no-dashboard  # headless pipeline only
    python run.py --dashboard-only
    python run.py --backfill      # pull real intraday history first, then run
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from market_pulse_engine.config import CONFIG
from market_pulse_engine.db import init as db_init
from market_pulse_engine.logging_setup import get_logger, setup_logging

log = get_logger("run")

BANNER = r"""
   __  __         _        _     ___      _
  |  \/  |__ _ _ | |_____ | |_  | _ \_  _| |___ ___
  | |\/| / _` | '_| / / -_)|  _| |  _/ || | (_-</ -_)
  |_|  |_\__,_|_| |_\_\___| \__| |_|  \_,_|_/__/\___|   E N G I N E
"""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Real-time financial data pipeline with AI narrative analysis.",
    )
    parser.add_argument("--once", action="store_true", help="run a single pipeline cycle and exit")
    parser.add_argument("--no-dashboard", action="store_true", help="run the pipeline headless")
    parser.add_argument("--dashboard-only", action="store_true", help="serve the dashboard without the scheduler")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="pull real intraday history before starting, so Gold has depth immediately",
    )
    parser.add_argument("--interval", type=int, default=None, help="seconds between cycles")
    return parser.parse_args()


def _shutdown(orchestrator) -> None:
    """Stop the scheduler and close DuckDB cleanly.

    Closing matters: DuckDB keeps a ``PRIMARY KEY`` index that, if the process
    dies mid-write, can be left inconsistent with the table's rows — after which
    the next overwrite aborts the database with a fatal index error. Shutting
    down cleanly checkpoints the file and avoids that entirely.
    """
    from market_pulse_engine.db import connection

    if orchestrator is not None:
        orchestrator.shutdown()
    try:
        connection.close()
    except Exception as exc:  # noqa: BLE001 - never mask the original exit path
        log.warning("database did not close cleanly", extra={"error": str(exc)})


def _backfill() -> None:
    from market_pulse_engine.ingestion import backfill_history
    from market_pulse_engine.transforms import run_transforms
    from market_pulse_engine.utils import new_run_id

    print("  backfilling intraday history…")
    result = backfill_history(new_run_id())
    print(f"  {result.written:,} historical bars landed in Bronze")
    run_transforms.run_all()


def main() -> int:
    args = _parse_args()
    setup_logging()
    print(BANNER)

    CONFIG.ensure_directories()
    db_init.apply_schema()
    print(f"  database   {CONFIG.db_path}")
    print(f"  watchlist  {', '.join(CONFIG.watchlist)}")
    print(f"  cadence    every {CONFIG.poll_seconds}s"
          f"  ·  LLM stage every {CONFIG.llm.every_n_runs} cycles")
    if CONFIG.demo_mode:
        print("  demo mode  ON — synthetic ticks are labelled 'demo_synthetic'")
    print()

    if args.backfill:
        _backfill()

    # -- single cycle ------------------------------------------------------
    if args.once:
        from market_pulse_engine.pipeline import Orchestrator

        report = Orchestrator().run_once()
        print(f"\n  {report.status} in {report.duration_ms:,} ms — "
              f"{report.records_ingested:,} records, {report.anomalies_detected:,} anomalies\n")
        return 0 if report.status != "failed" else 1

    orchestrator = None
    if not args.dashboard_only:
        from market_pulse_engine.pipeline import Orchestrator

        orchestrator = Orchestrator()
        orchestrator.start(args.interval)
        print(f"  pipeline   running every {args.interval or CONFIG.poll_seconds}s")

    # -- headless ----------------------------------------------------------
    if args.no_dashboard:
        stop = threading.Event()

        def _handle(_signum, _frame):
            stop.set()

        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)
        print("  dashboard  disabled — Ctrl-C to stop\n")
        try:
            stop.wait()
        finally:
            _shutdown(orchestrator)
        return 0

    # -- pipeline + dashboard ---------------------------------------------
    from market_pulse_engine.dashboard.app import create_app

    settings = CONFIG.dashboard
    app = create_app()
    print(f"  dashboard  http://{settings.host}:{settings.port}\n")

    try:
        app.run(host=settings.host, port=settings.port, debug=False)
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown(orchestrator)
    return 0


if __name__ == "__main__":
    sys.exit(main())
