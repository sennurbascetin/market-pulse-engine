"""Process the full Bronze -> Gold pipeline in one pass.

Run directly::

    python -m market_pulse_engine.transforms.run_transforms
"""

from __future__ import annotations

import time

from ..logging_setup import get_logger
from . import gold, silver
from .silver import TransformResult

log = get_logger("transforms.run")


def run_all() -> list[TransformResult]:
    """Execute Bronze -> Silver -> Gold and return every step's row counts."""
    started = time.perf_counter()
    results = silver.run_all() + gold.run_all()
    duration_ms = int((time.perf_counter() - started) * 1000)

    log.info(
        "transform pass complete",
        extra={
            "duration_ms": duration_ms,
            "tables": len(results),
            "rows_added": sum(result.rows_added for result in results),
        },
    )
    return results


def main() -> None:
    results = run_all()
    print("\n  Bronze → Silver → Gold\n")
    for result in results:
        print(f"    {result.table:<26} {result.rows_after:>8,} rows  (+{result.rows_added:,})")
    print()


if __name__ == "__main__":
    main()
