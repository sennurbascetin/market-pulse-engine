"""Plotly figures for the dashboard.

Design rules applied throughout:

* **No dual axes.** Price and volume share an x-axis as two stacked subplots
  with independent scales — never two y-scales on one plot, which would invent a
  correlation the data does not contain.
* **Thin marks, recessive chrome.** 2px lines, solid hairline gridlines one
  shade off the surface, no plot borders.
* **Identity never by colour alone.** Every multi-series figure carries a
  legend; direction always carries its sign; anomaly markers carry a shape and a
  hover description as well as a colour.
* **Selective direct labels.** The last MA value is labelled; individual points
  are left to the hover layer.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from . import theme
from .theme import (
    ACCENT, AMBER, BORDER, GREEN, GRID, RED, SEVERITY_COLORS, SURFACE_RAISED,
    TEMPLATE, TEXT, TEXT_MUTED, TEXT_SECONDARY, VOLUME_SCALE,
)

EMPTY_MESSAGE = "awaiting data — the first pipeline cycle is still running"


def _empty_figure(message: str = EMPTY_MESSAGE, height: int = 320) -> go.Figure:
    """A placeholder that keeps the layout stable before data lands."""
    figure = go.Figure()
    figure.update_layout(
        template=TEMPLATE,
        height=height,
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        annotations=[
            dict(
                text=message, x=0.5, y=0.5, xref="paper", yref="paper",
                showarrow=False, font=dict(size=12, color=TEXT_MUTED),
            )
        ],
    )
    return figure


# ---------------------------------------------------------------------------
# Panel 4 — price chart
# ---------------------------------------------------------------------------
def price_chart(
    ticker: str, series: pd.DataFrame, anomalies: pd.DataFrame, height: int = 430
) -> go.Figure:
    """Candlesticks with MA overlays, VWAP, anomaly markers and a volume band."""
    if series is None or series.empty:
        return _empty_figure(f"no Gold data for {ticker} yet", height)

    figure = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.74, 0.26], vertical_spacing=0.06,
        subplot_titles=("", ""),
    )

    figure.add_trace(
        go.Candlestick(
            x=series["observed_at"],
            open=series["open"], high=series["day_high"],
            low=series["day_low"], close=series["price"],
            name=ticker,
            increasing=dict(line=dict(color=GREEN, width=1), fillcolor=GREEN),
            decreasing=dict(line=dict(color=RED, width=1), fillcolor=RED),
            showlegend=False,
            hovertext=[f"{ticker}" for _ in range(len(series))],
        ),
        row=1, col=1,
    )

    # Moving averages: fixed categorical slots, never reassigned by rank.
    overlays = (
        ("ma_short", theme.SERIES[0], "MA fast"),
        ("ma_mid", theme.SERIES[1], "MA mid"),
        ("ma_long", theme.SERIES[2], "MA slow"),
    )
    for column, color, label in overlays:
        if column not in series or series[column].isna().all():
            continue
        figure.add_trace(
            go.Scatter(
                x=series["observed_at"], y=series[column],
                mode="lines", name=label,
                line=dict(color=color, width=2),
                hovertemplate=f"{label} %{{y:,.2f}}<extra></extra>",
            ),
            row=1, col=1,
        )

    if "vwap" in series and not series["vwap"].isna().all():
        figure.add_trace(
            go.Scatter(
                x=series["observed_at"], y=series["vwap"],
                mode="lines", name="VWAP",
                line=dict(color=TEXT_MUTED, width=1),
                hovertemplate="VWAP %{y:,.2f}<extra></extra>",
            ),
            row=1, col=1,
        )

    # Anomaly markers: amber, ringed in the surface colour so overlapping marks
    # stay separable, and never colour-alone — shape plus hover text carry it.
    if anomalies is not None and not anomalies.empty:
        figure.add_trace(
            go.Scatter(
                x=anomalies["observed_at"], y=anomalies["price"],
                mode="markers", name="anomaly",
                marker=dict(
                    size=11, symbol="circle", color=AMBER,
                    line=dict(color=theme.BG, width=2),
                ),
                customdata=anomalies[["description", "severity"]].values,
                hovertemplate="<b>%{customdata[1]}</b><br>%{customdata[0]}<extra></extra>",
            ),
            row=1, col=1,
        )

    volume_colors = [
        GREEN if close >= open_ else RED
        for close, open_ in zip(series["price"], series["open"])
    ]
    figure.add_trace(
        go.Bar(
            x=series["observed_at"], y=series["volume_delta"],
            name="volume", marker=dict(color=volume_colors, line=dict(width=0)),
            opacity=0.55, showlegend=False,
            hovertemplate="volume %{y:,.0f}<extra></extra>",
        ),
        row=2, col=1,
    )

    figure.update_layout(
        template=TEMPLATE,
        height=height,
        # The y-axes sit on the right, so the right margin must be wide enough
        # to hold their tick labels — otherwise the card clips them.
        margin=dict(l=14, r=64, t=42, b=34),
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
        bargap=0.15,
        legend=dict(orientation="h", y=1.03, x=0, font=dict(size=11)),
    )
    figure.update_yaxes(title_text="price", row=1, col=1, gridcolor=GRID, side="right")
    figure.update_yaxes(title_text="volume", row=2, col=1, gridcolor=GRID, side="right")
    figure.update_xaxes(showgrid=True, gridcolor=GRID, row=2, col=1)
    return figure


# ---------------------------------------------------------------------------
# Panel 5 — volume heatmap
# ---------------------------------------------------------------------------
def volume_heatmap(frame: pd.DataFrame, height: int = 300) -> go.Figure:
    """Rows = tickers, columns = hour of day, colour = share of that row's peak."""
    if frame is None or frame.empty:
        return _empty_figure("no volume history yet", height)

    grid = frame.pivot(index="ticker", columns="hour_of_day", values="pct_of_peak")
    raw = frame.pivot(index="ticker", columns="hour_of_day", values="volume")
    grid = grid.sort_index(ascending=False)
    raw = raw.reindex(grid.index)

    figure = go.Figure(
        go.Heatmap(
            z=grid.values,
            x=[f"{hour:02d}" for hour in grid.columns],
            y=grid.index.tolist(),
            customdata=raw.values,
            colorscale=VOLUME_SCALE,
            zmin=0, zmax=100,
            xgap=2, ygap=2,  # surface gap between cells, never a border
            colorbar=dict(
                title=dict(text="% of peak", font=dict(size=10, color=TEXT_SECONDARY)),
                thickness=10, len=0.85, outlinewidth=0,
                tickfont=dict(size=10, color=TEXT_SECONDARY),
            ),
            hovertemplate="<b>%{y}</b> at %{x}:00 UTC<br>%{z:.0f}% of its peak hour<br>volume %{customdata:,.0f}<extra></extra>",
        )
    )
    figure.update_layout(
        template=TEMPLATE, height=height, margin=dict(l=76, r=16, t=16, b=44)
    )
    # Without an explicit category type Plotly reads "00".."23" as numbers and
    # thins the axis to 0/5/10/15/20, losing the hour-by-hour reading.
    figure.update_xaxes(
        type="category", showgrid=False, tickfont=dict(size=9),
        title=dict(text="hour of day (UTC)", font=dict(size=10, color=TEXT_SECONDARY)),
    )
    figure.update_yaxes(showgrid=False, tickfont=dict(size=11, color=TEXT))
    return figure


# ---------------------------------------------------------------------------
# Panel 3 — Fear & Greed gauge
# ---------------------------------------------------------------------------
def fear_greed_gauge(reading: dict | None, height: int = 240) -> go.Figure:
    """Diverging arc gauge: fear <-> greed with a neutral midpoint."""
    if not reading or reading.get("score") is None:
        return _empty_figure("Fear & Greed unavailable", height)

    score = float(reading["score"])
    previous = reading.get("previous_close")

    figure = go.Figure(
        go.Indicator(
            mode="gauge+number+delta" if previous else "gauge+number",
            value=score,
            number=dict(font=dict(size=42, color=TEXT, family=theme.MONO_STACK), valueformat=".0f"),
            delta=(
                dict(
                    reference=previous,
                    # Stacked under the number rather than beside it, which
                    # would push the pair off the gauge's centre line.
                    position="bottom",
                    increasing=dict(color=GREEN),
                    decreasing=dict(color=RED),
                    font=dict(size=13),
                    valueformat=".1f",
                )
                if previous
                else None
            ),
            gauge=dict(
                axis=dict(
                    range=[0, 100], tickwidth=1, tickcolor=BORDER,
                    tickfont=dict(size=10, color=TEXT_SECONDARY),
                    tickvals=[0, 25, 50, 75, 100],
                ),
                bar=dict(color=TEXT, thickness=0.18),
                bgcolor="rgba(0,0,0,0)",
                borderwidth=0,
                steps=[
                    dict(range=[low, high], color=color)
                    for low, high, color, _ in theme.FEAR_GREED_BANDS
                ],
                threshold=dict(line=dict(color=TEXT, width=3), thickness=0.8, value=score),
            ),
            # Lifted off the floor of the plot so the arc's 0/100 end labels and
            # the stacked delta both have room inside the card.
            domain=dict(x=[0, 1], y=[0.12, 1]),
        )
    )
    figure.update_layout(
        template=TEMPLATE, height=height, margin=dict(l=26, r=26, t=10, b=4)
    )
    return figure


# ---------------------------------------------------------------------------
# Anomaly severity distribution — a small bar summary above the alert log
# ---------------------------------------------------------------------------
def severity_bars(log: list[dict], height: int = 96) -> go.Figure:
    """Counts by severity. One mark per level, status colours, direct labels."""
    if not log:
        return _empty_figure("no anomalies detected yet", height)

    order = ["high", "medium", "low"]
    counts = {level: sum(1 for item in log if item["severity"] == level) for level in order}

    figure = go.Figure(
        go.Bar(
            x=[counts[level] for level in order],
            y=[level.upper() for level in order],
            orientation="h",
            marker=dict(
                color=[SEVERITY_COLORS[level] for level in order],
                cornerradius=4,  # rounded data-end, anchored to the baseline
                line=dict(width=0),
            ),
            text=[str(counts[level]) for level in order],
            textposition="outside",
            textfont=dict(size=11, color=TEXT_SECONDARY, family=theme.MONO_STACK),
            hovertemplate="%{y}: %{x} anomalies<extra></extra>",
        )
    )
    figure.update_layout(
        template=TEMPLATE, height=height,
        margin=dict(l=62, r=34, t=8, b=8), bargap=0.52,
    )
    figure.update_xaxes(visible=False)
    figure.update_yaxes(showgrid=False, tickfont=dict(size=10, color=TEXT_SECONDARY))
    return figure
