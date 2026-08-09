"""The Market Pulse dashboard — eight live panels over the Gold and Platinum layers.

Run standalone (reads whatever is already in the database)::

    python -m market_pulse_engine.dashboard.app

Or, normally, via ``python run.py``, which starts the scheduler in the same
process so that one DuckDB file backs both the writer and the reader.

Every panel refreshes on a single ``dcc.Interval`` so the whole board updates in
one callback pass rather than N independent ones.
"""

from __future__ import annotations

from datetime import datetime, timezone

import dash_bootstrap_components as dbc
from dash import Dash, Input, Output, dcc, html

from ..config import CONFIG
from ..logging_setup import get_logger
from . import components as ui
from . import figures, queries, theme

log = get_logger("dashboard")

GOOGLE_FONTS = (
    "https://fonts.googleapis.com/css2?"
    "family=JetBrains+Mono:wght@400;600;700&display=swap"
)


def _masthead() -> html.Div:
    return html.Div(
        [
            html.Span("Market Pulse Engine", className="brand"),
            html.Span("real-time financial intelligence", className="brand-sub"),
            html.Div(
                [
                    html.Span(id="session-state", className="clock"),
                    html.Span(id="wall-clock", className="clock"),
                ],
                className="masthead-right",
            ),
        ],
        className="masthead",
    )


def _ticker_picker() -> html.Div:
    """Chip row selecting which ticker the price chart shows."""
    return html.Div(
        dcc.RadioItems(
            id="ticker-select",
            options=[{"label": ticker, "value": ticker} for ticker in CONFIG.watchlist],
            value=CONFIG.watchlist[0],
            className="ticker-picker",
            inputClassName="ticker-radio-input",
            labelClassName="ticker-chip",
            inline=True,
        )
    )


def build_layout() -> html.Div:
    """The full page. Panels are declared here; data arrives via callbacks."""
    return html.Div(
        [
            dcc.Interval(
                id="refresh",
                interval=CONFIG.dashboard.refresh_seconds * 1000,
                n_intervals=0,
            ),
            _masthead(),
            html.Div(id="ticker-tape"),
            html.Div(
                [
                    # --- row 1: briefing + fear & greed ------------------------
                    html.Div(
                        [
                            html.Div(
                                ui.panel(
                                    "Market Pulse",
                                    html.Div(id="pulse-card"),
                                    subtitle="AI analyst briefing",
                                ),
                                className="col-wide",
                            ),
                            html.Div(
                                ui.panel(
                                    "Fear & Greed",
                                    [
                                        ui.graph("fear-greed-gauge", height=240),
                                        html.Div(id="fear-greed-label", className="gauge-label"),
                                    ],
                                    subtitle="CNN composite",
                                    className="panel-flush",
                                ),
                                className="col",
                            ),
                        ],
                        className="grid-row",
                    ),
                    # --- row 2: price chart + anomaly log ----------------------
                    html.Div(
                        [
                            html.Div(
                                ui.panel(
                                    "Price & Volume",
                                    [_ticker_picker(), ui.graph("price-chart", height=430)],
                                    subtitle="candles · MA overlays · anomalies",
                                    className="panel-flush",
                                ),
                                className="col-wide",
                            ),
                            html.Div(
                                ui.panel(
                                    "Anomaly Alerts",
                                    [
                                        ui.graph("severity-bars", height=96),
                                        html.Div(id="anomaly-log"),
                                    ],
                                    subtitle="Z-score ∩ IQR consensus",
                                ),
                                className="col",
                            ),
                        ],
                        className="grid-row",
                    ),
                    # --- row 3: heatmap + sentiment feed -----------------------
                    html.Div(
                        [
                            html.Div(
                                ui.panel(
                                    "Activity Heatmap",
                                    ui.graph("volume-heatmap", height=300),
                                    subtitle="hour of day · % of each ticker's peak",
                                    className="panel-flush",
                                ),
                                className="col",
                            ),
                            html.Div(
                                ui.panel(
                                    "Sentiment Feed",
                                    html.Div(id="sentiment-feed"),
                                    subtitle="scored news",
                                ),
                                className="col",
                            ),
                        ],
                        className="grid-row",
                    ),
                ],
                className="shell",
            ),
            html.Div(id="health-bar"),
        ]
    )


def create_app() -> Dash:
    """Build the Dash application and register its callbacks."""
    app = Dash(
        __name__,
        title="Market Pulse Engine",
        update_title=None,
        external_stylesheets=[dbc.themes.BOOTSTRAP, GOOGLE_FONTS],
        suppress_callback_exceptions=True,
    )
    app.layout = build_layout()
    _register_callbacks(app)
    return app


def _register_callbacks(app: Dash) -> None:
    @app.callback(
        Output("ticker-tape", "children"),
        Output("pulse-card", "children"),
        Output("fear-greed-gauge", "figure"),
        Output("fear-greed-label", "children"),
        Output("volume-heatmap", "figure"),
        Output("sentiment-feed", "children"),
        Output("anomaly-log", "children"),
        Output("severity-bars", "figure"),
        Output("health-bar", "children"),
        Output("session-state", "children"),
        Output("wall-clock", "children"),
        Input("refresh", "n_intervals"),
    )
    def refresh_board(_n: int):
        """One pass repaints every panel that does not depend on the picker."""
        from ..ingestion.market_hours import session_state

        reading = queries.fear_greed()
        alerts = queries.anomaly_log()

        gauge_label = html.Div("—", className="gauge-rating")
        if reading and reading.get("score") is not None:
            band_color = theme.TEXT_SECONDARY
            for low, high, color, _label in theme.FEAR_GREED_BANDS:
                if low <= reading["score"] < high:
                    band_color = color
                    break
            context = [
                html.Span(f"prev {reading['previous_close']:.0f}") if reading.get("previous_close") else None,
                html.Span(f"1w {reading['week_ago']:.0f}") if reading.get("week_ago") else None,
                html.Span(f"1m {reading['month_ago']:.0f}") if reading.get("month_ago") else None,
            ]
            gauge_label = html.Div(
                [
                    html.Div(reading["label"], className="gauge-rating", style={"color": band_color}),
                    html.Div([c for c in context if c], className="gauge-context"),
                ]
            )

        now = datetime.now(timezone.utc)
        return (
            ui.ticker_tape(queries.ticker_tape()),
            ui.pulse_card(queries.latest_narrative()),
            figures.fear_greed_gauge(reading),
            gauge_label,
            figures.volume_heatmap(queries.volume_heatmap()),
            ui.sentiment_feed(queries.sentiment_feed()),
            ui.anomaly_log(alerts),
            figures.severity_bars(alerts),
            ui.health_bar(queries.pipeline_health(), queries.layer_counts()),
            f"session: {session_state().replace('_', ' ')}",
            now.strftime("%Y-%m-%d %H:%M:%S UTC"),
        )

    @app.callback(
        Output("price-chart", "figure"),
        Input("ticker-select", "value"),
        Input("refresh", "n_intervals"),
    )
    def refresh_price_chart(ticker: str, _n: int):
        """Separate callback so changing ticker repaints instantly."""
        symbol = ticker or CONFIG.watchlist[0]
        series = queries.price_series(symbol)
        since = series["observed_at"].min() if not series.empty else None
        return figures.price_chart(symbol, series, queries.anomalies_for(symbol, since))


def main() -> None:
    settings = CONFIG.dashboard
    app = create_app()
    log.info(
        "dashboard starting",
        extra={"host": settings.host, "port": settings.port, "refresh_s": settings.refresh_seconds},
    )
    print(f"\n  Market Pulse Engine → http://{settings.host}:{settings.port}\n")
    app.run(host=settings.host, port=settings.port, debug=False)


if __name__ == "__main__":
    main()
