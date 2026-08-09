"""Phase 6 — the self-managing pipeline.

One cycle is:

1. **Ingest** — the three connectors run concurrently in a ``ThreadPoolExecutor``;
   the whole watchlist plus five RSS feeds plus the sentiment index costs roughly
   one round-trip rather than N.
2. **Transform** — Bronze -> Silver -> Gold, in DuckDB SQL.
3. **Analyse** — anomaly consensus scan, then (throttled) the LLM analyst.
4. **Record** — a row in ``pipeline.run_log`` and a structured JSON log line.

Market-hours awareness: outside the regular session the price leg is skipped for
equities, because polling a closed exchange returns the same close over and over.
Crypto is always ingested, and news and sentiment never stop.

Failure policy: a failing *stage* degrades the run to ``partial`` and is
recorded; it never kills the scheduler. The engine is meant to run unattended.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import CONFIG
from ..db.connection import transaction
from ..ingestion import fetch_news, fetch_quotes, fetch_sentiment_index
from ..ingestion.market_hours import is_market_open, session_state
from ..intelligence import anomaly_detector, llm_analyst
from ..intelligence.llm_provider import BaseProvider, get_provider
from ..logging_setup import get_logger
from ..transforms import gold, silver
from ..utils import new_run_id, utcnow

log = get_logger("pipeline.orchestrator")

#: How far back each scheduled anomaly scan looks. Long enough to cover a
#: weekend gap, short enough that the scan stays a fraction of the cycle.
ANOMALY_LOOKBACK_HOURS = 72


@dataclass
class RunReport:
    """Everything one cycle produced — mirrors ``pipeline.run_log``."""

    run_id: str
    mode: str
    started_at: Any
    finished_at: Any = None
    duration_ms: int = 0
    status: str = "running"
    quotes_ingested: int = 0
    news_ingested: int = 0
    sentiment_ingested: int = 0
    silver_rows: int = 0
    gold_rows: int = 0
    anomalies_detected: int = 0
    articles_scored: int = 0
    llm_calls: int = 0
    llm_tokens_used: int = 0
    llm_provider: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def records_ingested(self) -> int:
        return self.quotes_ingested + self.news_ingested + self.sentiment_ingested

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "records_ingested": self.records_ingested,
            "anomalies_detected": self.anomalies_detected,
            "articles_scored": self.articles_scored,
            "llm_calls": self.llm_calls,
            "llm_tokens_used": self.llm_tokens_used,
            "llm_provider": self.llm_provider,
            "errors": len(self.errors),
        }


class Orchestrator:
    """Runs pipeline cycles, on demand or on a schedule."""

    def __init__(self, provider: BaseProvider | None = None) -> None:
        self._provider = provider
        self._cycle = 0
        self._scheduler = None

    # -- provider is resolved lazily so start-up never blocks on a network SDK
    @property
    def provider(self) -> BaseProvider:
        if self._provider is None:
            self._provider = get_provider()
        return self._provider

    # ------------------------------------------------------------------
    def _guard(self, report: RunReport, stage: str, action: Callable[[], Any]) -> Any:
        """Run one stage, converting an exception into a recorded degradation."""
        try:
            return action()
        except Exception as exc:  # noqa: BLE001 - the scheduler must survive anything
            message = f"{stage}: {exc}"
            report.errors.append(message)
            log.error("stage failed", extra={"run_id": report.run_id, "stage": stage, "error": str(exc)})
            return None

    def run_once(self) -> RunReport:
        """Execute one full pipeline cycle and return its report."""
        self._cycle += 1
        started = time.perf_counter()
        run_id = new_run_id()
        market_open = is_market_open()
        mode = "demo" if CONFIG.demo_mode else ("live" if market_open else "after_hours")

        report = RunReport(run_id=run_id, mode=mode, started_at=utcnow())
        self._open_run_log(report)
        log.info("cycle start", extra={"run_id": run_id, "mode": mode, "session": session_state()})

        # --- 1. ingest (concurrent) -----------------------------------------
        # Outside the session, equity prices are frozen at the last close, so the
        # price leg is limited to instruments that actually trade round the clock.
        symbols = (
            CONFIG.watchlist
            if market_open
            else [ticker for ticker in CONFIG.watchlist if CONFIG.is_crypto(ticker)]
        )

        jobs: dict[str, Callable[[], Any]] = {
            "news": lambda: fetch_news(run_id),
            "sentiment": lambda: fetch_sentiment_index(run_id),
        }
        if symbols:
            jobs["quotes"] = lambda: fetch_quotes(run_id, symbols)
        if CONFIG.demo_mode:
            # Clearly-labelled synthetic ticks so the board stays alive with the
            # markets shut. See DEMO_MODE.md.
            from . import demo_seed

            jobs["demo"] = lambda: demo_seed.synthesise(run_id)

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {name: pool.submit(action) for name, action in jobs.items()}
            results = {
                name: self._guard(report, f"ingest.{name}", future.result)
                for name, future in futures.items()
            }

        for name, result in results.items():
            if result is None:
                continue
            if name in ("quotes", "demo"):
                report.quotes_ingested += result.written
            elif name == "news":
                report.news_ingested = result.written
            elif name == "sentiment":
                report.sentiment_ingested = result.written
            report.errors.extend(result.errors)

        # --- 2. transform ----------------------------------------------------
        silver_results = self._guard(report, "transform.silver", silver.run_all) or []
        gold_results = self._guard(report, "transform.gold", gold.run_all) or []
        report.silver_rows = sum(r.rows_after for r in silver_results)
        report.gold_rows = sum(r.rows_after for r in gold_results)

        # --- 3. analyse ------------------------------------------------------
        # A bounded lookback keeps the steady-state scan cheap; the rolling
        # windows still see plenty of history inside it.
        detection = self._guard(
            report,
            "intelligence.anomalies",
            lambda: anomaly_detector.detect(run_id, lookback_hours=ANOMALY_LOOKBACK_HOURS),
        )
        report.anomalies_detected = detection.newly_recorded if detection else 0

        if self._should_run_llm():
            analysis = self._guard(
                report, "intelligence.analyst", lambda: llm_analyst.run(run_id, self.provider)
            )
            if analysis:
                report.articles_scored = analysis.articles_scored
                report.llm_calls = analysis.llm_calls
                report.llm_tokens_used = analysis.tokens_used
                report.llm_provider = analysis.provider
        else:
            log.info(
                "llm stage skipped",
                extra={"run_id": run_id, "cycle": self._cycle, "every_n": CONFIG.llm.every_n_runs},
            )

        # --- 4. record -------------------------------------------------------
        report.duration_ms = int((time.perf_counter() - started) * 1000)
        report.finished_at = utcnow()
        report.status = "failed" if len(report.errors) >= 3 else ("partial" if report.errors else "success")
        self._close_run_log(report)

        log.info("cycle complete", extra=report.as_log_fields())
        return report

    def _should_run_llm(self) -> bool:
        """Throttle the paid stage: the first cycle, then every Nth."""
        every = max(1, CONFIG.llm.every_n_runs)
        return self._cycle == 1 or self._cycle % every == 0

    # ------------------------------------------------------------------
    def _open_run_log(self, report: RunReport) -> None:
        with transaction() as conn:
            conn.execute(
                """
                INSERT INTO pipeline.run_log (run_id, started_at, status, mode)
                VALUES (?, ?, 'running', ?) ON CONFLICT DO NOTHING
                """,
                [report.run_id, report.started_at, report.mode],
            )

    def _close_run_log(self, report: RunReport) -> None:
        with transaction() as conn:
            conn.execute(
                """
                UPDATE pipeline.run_log SET
                    finished_at = ?, duration_ms = ?, status = ?,
                    quotes_ingested = ?, news_ingested = ?, sentiment_ingested = ?,
                    records_ingested = ?, silver_rows = ?, gold_rows = ?,
                    anomalies_detected = ?, articles_scored = ?,
                    llm_calls = ?, llm_tokens_used = ?, llm_provider = ?, error = ?
                WHERE run_id = ?
                """,
                [
                    report.finished_at, report.duration_ms, report.status,
                    report.quotes_ingested, report.news_ingested, report.sentiment_ingested,
                    report.records_ingested, report.silver_rows, report.gold_rows,
                    report.anomalies_detected, report.articles_scored,
                    report.llm_calls, report.llm_tokens_used, report.llm_provider,
                    " | ".join(report.errors[:5]) if report.errors else None,
                    report.run_id,
                ],
            )

    # ------------------------------------------------------------------
    def start(self, interval_seconds: int | None = None) -> Any:
        """Start the APScheduler background job and return the scheduler."""
        from apscheduler.schedulers.background import BackgroundScheduler

        interval = interval_seconds or CONFIG.poll_seconds
        scheduler = BackgroundScheduler(
            timezone="UTC",
            job_defaults={
                # A slow cycle must never stack up behind the next tick.
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": max(30, interval // 2),
            },
        )
        scheduler.add_job(
            self.run_once,
            trigger="interval",
            seconds=interval,
            id="market_pulse_cycle",
            next_run_time=utcnow(),  # run immediately, then every `interval`
        )
        scheduler.start()
        self._scheduler = scheduler
        log.info("scheduler started", extra={"interval_seconds": interval})
        return scheduler

    def shutdown(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            log.info("scheduler stopped")


def main() -> None:
    """Run a single cycle from the command line (useful for cron or CI)."""
    from ..db import init as db_init

    db_init.apply_schema()
    report = Orchestrator().run_once()

    print(f"\n  run {report.run_id} — {report.status} in {report.duration_ms:,} ms\n")
    print(f"    ingested    {report.records_ingested:,} "
          f"(quotes {report.quotes_ingested}, news {report.news_ingested}, sentiment {report.sentiment_ingested})")
    print(f"    gold rows   {report.gold_rows:,}")
    print(f"    anomalies   {report.anomalies_detected:,}")
    print(f"    articles    {report.articles_scored:,} scored via {report.llm_provider or 'n/a'}")
    if report.errors:
        print(f"\n    {len(report.errors)} degradation(s):")
        for message in report.errors[:5]:
            print(f"      - {message}")
    print()


if __name__ == "__main__":
    main()
