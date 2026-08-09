"""Market Pulse Engine — a real-time financial intelligence pipeline.

Layers
------
``ingestion``     multi-source connectors writing raw payloads to Bronze
``transforms``    Bronze -> Silver -> Gold, expressed in pure DuckDB SQL
``intelligence``  Gold -> Platinum: anomaly detection and LLM narrative analysis
``dashboard``     Plotly Dash serving layer
``pipeline``      APScheduler orchestration and run bookkeeping
"""

__version__ = "1.0.0"
