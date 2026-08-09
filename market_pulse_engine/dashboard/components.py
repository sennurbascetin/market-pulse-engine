"""Reusable UI pieces for the dashboard.

Every status colour ships with its text label, so nothing in the interface is
identifiable by colour alone.
"""

from __future__ import annotations

from typing import Any

from dash import dcc, html

from ..utils import humanise_age
from . import theme
from .theme import GRAPH_CONFIG


def panel(title: str, children: Any, *, subtitle: str | None = None, className: str = "") -> html.Div:
    """A titled card — the single container primitive the layout is built from."""
    header = [html.Span(title, className="panel-title")]
    if subtitle:
        header.append(html.Span(subtitle, className="panel-subtitle"))
    return html.Div(
        [html.Div(header, className="panel-header"), html.Div(children, className="panel-body")],
        className=f"panel {className}".strip(),
    )


def graph(component_id: str, *, height: int | None = None) -> dcc.Graph:
    """A chart slot sized to include its axis band, so no card scrolls."""
    style = {"height": f"{height}px"} if height else None
    return dcc.Graph(id=component_id, config=GRAPH_CONFIG, style=style)


def badge(text: str, color: str, *, subtle: bool = False) -> html.Span:
    """A pill carrying both a colour and its label."""
    return html.Span(
        text.upper(),
        className="badge",
        style={
            "color": theme.BG if not subtle else color,
            "backgroundColor": color if not subtle else "transparent",
            "border": f"1px solid {color}",
        },
    )


def signed(value: float | None, *, suffix: str = "%", digits: int = 2) -> html.Span:
    """A signed number in up/down ink.

    The sign is always rendered: direction is carried by the glyph as well as by
    the colour, which keeps the readout legible under colour-vision deficiency.
    """
    if value is None:
        return html.Span("—", style={"color": theme.TEXT_MUTED})
    return html.Span(
        f"{value:+.{digits}f}{suffix}",
        style={"color": theme.color_for_change(value), "fontWeight": 600},
    )


# ---------------------------------------------------------------------------
# Panel 1 — ticker tape
# ---------------------------------------------------------------------------
def ticker_tape(rows: list[dict[str, Any]]) -> html.Div:
    """Scrolling top bar. The item list is duplicated so the loop is seamless."""
    if not rows:
        return html.Div(
            html.Div("awaiting first quotes…", className="tape-item tape-empty"),
            className="tape-track",
        )

    def item(row: dict[str, Any]) -> html.Div:
        live_dot = html.Span(
            "●",
            className="tape-live" if row["is_live"] else "tape-stale",
            title="live" if row["is_live"] else "market closed — last known price",
        )
        price = row["price"]
        return html.Div(
            [
                live_dot,
                html.Span(row["ticker"], className="tape-symbol"),
                html.Span(f"{price:,.2f}" if price is not None else "—", className="tape-price"),
                signed(row["change_pct"]),
            ],
            className="tape-item",
        )

    items = [item(row) for row in rows]
    return html.Div(
        [html.Div(items + items, className="tape-track")], className="tape-viewport"
    )


# ---------------------------------------------------------------------------
# Panel 2 — Market Pulse briefing
# ---------------------------------------------------------------------------
def pulse_card(narrative: dict[str, Any] | None) -> html.Div:
    if not narrative:
        return html.Div(
            "The analyst has not written a briefing yet — it is produced at the end "
            "of the first full pipeline cycle.",
            className="pulse-empty",
        )

    regime = narrative.get("regime", "mixed")
    color = theme.REGIME_COLORS.get(regime, theme.AMBER)
    provider = narrative.get("provider", "heuristic")
    provider_label = {
        "heuristic": "offline analyst",
        "openai": "OpenAI",
        "anthropic": "Anthropic",
    }.get(provider, provider)

    return html.Div(
        [
            html.Div(
                [
                    badge(regime.replace("_", " "), color),
                    html.Span(narrative.get("headline", "Market Pulse"), className="pulse-headline"),
                ],
                className="pulse-heading",
            ),
            html.P(narrative["narrative"], className="pulse-text"),
            html.Div(
                [
                    html.Span(f"generated {humanise_age(narrative.get('generated_at'))}"),
                    html.Span("·", className="dot-sep"),
                    html.Span(f"{provider_label} · {narrative.get('model', '')}"),
                ],
                className="pulse-meta",
            ),
        ]
    )


# ---------------------------------------------------------------------------
# Panel 6 — sentiment feed
# ---------------------------------------------------------------------------
def sentiment_feed(articles: list[dict[str, Any]]) -> html.Div:
    if not articles:
        return html.Div("no scored articles yet", className="feed-empty")

    def row(article: dict[str, Any]) -> html.Div:
        color = theme.SENTIMENT_COLORS.get(article["sentiment"], theme.TEXT_SECONDARY)
        confidence = article.get("confidence") or 0.0
        meta = [article["source"], humanise_age(article.get("published_at"))]
        if article.get("tickers"):
            meta.append(" ".join(article["tickers"]))
        return html.Div(
            [
                html.Div(
                    [badge(article["sentiment"], color, subtle=True),
                     html.Span(f"{confidence:.0%}", className="feed-confidence")],
                    className="feed-badges",
                ),
                html.Div(
                    [
                        html.A(
                            article["title"],
                            href=article["url"],
                            target="_blank",
                            rel="noopener noreferrer",
                            className="feed-title",
                        ),
                        html.Div(" · ".join(meta), className="feed-meta"),
                    ],
                    className="feed-content",
                ),
            ],
            className="feed-row",
        )

    return html.Div([row(article) for article in articles], className="feed")


# ---------------------------------------------------------------------------
# Panel 7 — anomaly alert log
# ---------------------------------------------------------------------------
def anomaly_log(events: list[dict[str, Any]]) -> html.Div:
    if not events:
        return html.Div("no anomalies confirmed yet", className="feed-empty")

    def row(event: dict[str, Any]) -> html.Div:
        color = theme.SEVERITY_COLORS.get(event["severity"], theme.ACCENT)
        arrow = "▲" if event["direction"] == "up" else "▼"
        return html.Div(
            [
                html.Div(
                    [badge(event["severity"], color, subtle=True),
                     html.Span(f"{arrow} {abs(event['z_score']):.1f}σ",
                               className="alert-sigma", style={"color": color})],
                    className="feed-badges",
                ),
                html.Div(
                    [
                        html.Div(event["description"], className="alert-text"),
                        html.Div(
                            f"{event['anomaly_type'].replace('_', ' ')} · "
                            f"{humanise_age(event['observed_at'])}",
                            className="feed-meta",
                        ),
                    ],
                    className="feed-content",
                ),
            ],
            className="feed-row",
        )

    return html.Div([row(event) for event in events], className="feed")


# ---------------------------------------------------------------------------
# Panel 8 — pipeline health bar
# ---------------------------------------------------------------------------
def health_bar(health: dict[str, Any], layers: dict[str, int]) -> html.Div:
    status = health.get("status", "idle")
    status_color = {
        "success": theme.GREEN, "running": theme.ACCENT,
        "partial": theme.AMBER, "failed": theme.RED,
    }.get(status, theme.TEXT_MUTED)

    duration = health.get("duration_ms")
    uptime = (
        humanise_age(health["first_run"]).replace(" ago", "")
        if health.get("first_run")
        else "—"
    )

    def stat(label: str, value: str, color: str | None = None) -> html.Div:
        return html.Div(
            [
                html.Span(label, className="stat-label"),
                html.Span(value, className="stat-value", style={"color": color} if color else None),
            ],
            className="stat",
        )

    bronze = sum(count for name, count in layers.items() if name.startswith("bronze"))
    platinum = sum(count for name, count in layers.items() if name.startswith("platinum"))

    return html.Div(
        [
            stat("status", status.upper(), status_color),
            stat("mode", str(health.get("mode", "-")).replace("_", " ")),
            stat("last run", humanise_age(health.get("last_run_at"))),
            stat("duration", f"{duration:,.0f} ms" if duration else "—"),
            stat("runs", f"{health.get('runs', 0):,}"),
            stat("uptime", uptime),
            stat("bronze", f"{bronze:,}"),
            stat("gold", f"{layers.get('gold.quotes_enriched', 0):,}"),
            stat("platinum", f"{platinum:,}"),
            stat("anomalies", f"{health.get('total_anomalies', 0):,}", theme.AMBER),
            stat("llm tokens", f"{health.get('total_tokens', 0):,}"),
            stat(
                "errors",
                f"{health.get('failures', 0):,}",
                theme.RED if health.get("failures") else theme.TEXT_SECONDARY,
            ),
        ],
        className="health-bar",
    )
