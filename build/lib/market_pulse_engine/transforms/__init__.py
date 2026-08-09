"""Bronze -> Silver -> Gold transforms, expressed in pure DuckDB SQL.

Import the submodules directly (``from .transforms import run_transforms``);
this package deliberately imports nothing at module scope so that
``python -m market_pulse_engine.transforms.run_transforms`` executes cleanly.
"""

__all__ = ["gold", "loader", "run_transforms", "silver"]
