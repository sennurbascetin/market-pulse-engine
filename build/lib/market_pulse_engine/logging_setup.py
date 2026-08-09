"""Structured JSON logging for the pipeline.

Every record is emitted as a single JSON object so ``logs/pipeline.log`` can be
tailed, grepped, or loaded straight into DuckDB:

    SELECT * FROM read_json_auto('logs/pipeline.log');

Extra fields are attached per call site, e.g.::

    log.info("pipeline complete", extra={"run_id": rid, "duration_ms": 1234})
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

from .config import CONFIG

#: Attributes present on every ``LogRecord``; anything else was passed by the
#: caller via ``extra=`` and belongs in the structured payload.
_STANDARD_ATTRS = set(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    """Render log records as one-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    """Compact, human-readable console output that mirrors the dashboard palette."""

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_ATTRS and key != "run_id"
        }
        suffix = "  " + " ".join(f"{k}={v}" for k, v in extras.items()) if extras else ""
        return f"{stamp} │ {record.levelname:<7} │ {record.name:<28} │ {record.getMessage()}{suffix}"


def setup_logging(level: int = logging.INFO, *, console: bool = True) -> None:
    """Install the JSON file handler (and optionally a console handler).

    Idempotent: repeated calls are no-ops, so importing modules may call it
    freely without stacking duplicate handlers.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    CONFIG.ensure_directories()
    root = logging.getLogger("market_pulse_engine")
    root.setLevel(level)
    root.propagate = False

    file_handler = logging.FileHandler(CONFIG.log_path, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(ConsoleFormatter())
        root.addHandler(stream)

    # Third-party chatter would otherwise drown the pipeline's own narrative.
    for noisy in ("yfinance", "peewee", "urllib3", "apscheduler.executors", "werkzeug"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger, configuring logging on first use."""
    setup_logging()
    return logging.getLogger(f"market_pulse_engine.{name}")
