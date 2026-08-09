"""Design tokens and the shared Plotly template.

The chrome palette is the one specified in the project brief: deep navy surface,
electric cyan accent, amber alerts, red danger. Every colour that carries data
meaning was run through the OKLab/CVD checks against the ``#0a0e1a`` surface
before being adopted:

* **MA overlays** — cyan / violet / magenta. Worst all-pairs CVD ΔE 14.2
  (target ≥ 8), normal-vision ΔE 19.7 (floor ≥ 15), all ≥ 3:1 contrast.
* **Up/down** — mint ``#2ee6a8`` against red ``#ff4757``: CVD ΔE 14.9. The
  conventional trading green ``#3fb950`` was **rejected** — against this red it
  scores ΔE 1.2 under protanopia and deuteranopia, i.e. the two are effectively
  the same colour for roughly one in twelve men. Direction is additionally
  carried by the sign on every number and by candle geometry, so colour is never
  the sole encoding.
* **Severity** — cyan / amber / red, worst pair CVD ΔE 15.5. Badges always
  carry their text label.

Cyan and amber sit above the reference dark lightness band because that band is
calibrated for a ``#1a1a19`` surface; against this much darker navy they measure
10.8:1 contrast, which is the property the band exists to protect.
"""

from __future__ import annotations

import plotly.graph_objects as go

# ---------------------------------------------------------------------------
# Surfaces & ink
# ---------------------------------------------------------------------------
BG = "#0a0e1a"          # page / chart surface
SURFACE = "#0f1524"     # card
SURFACE_RAISED = "#141c2f"
BORDER = "#1e2a44"
GRID = "#161f33"        # hairline, one shade off the surface

TEXT = "#e6edf7"
TEXT_SECONDARY = "#93a5c4"
TEXT_MUTED = "#5f708b"

# ---------------------------------------------------------------------------
# Accents (from the brief)
# ---------------------------------------------------------------------------
ACCENT = "#00d4ff"      # electric cyan
AMBER = "#ffb347"       # alerts / anomaly markers
RED = "#ff4757"         # danger / down
GREEN = "#2ee6a8"       # up — CVD-validated against RED
VIOLET = "#9085e9"
MAGENTA = "#d55181"

#: Categorical slots for the moving-average overlays, in fixed order.
#: Never cycled, never reassigned by rank.
SERIES = (ACCENT, VIOLET, MAGENTA)

#: Status tokens. Reserved — never reused as a series colour.
SEVERITY_COLORS = {"low": ACCENT, "medium": AMBER, "high": RED}
SENTIMENT_COLORS = {"bullish": GREEN, "bearish": RED, "neutral": TEXT_SECONDARY}
REGIME_COLORS = {"risk_on": GREEN, "risk_off": RED, "mixed": AMBER, "calm": ACCENT}

#: Single-hue sequential ramp for the volume heatmap (surface -> accent).
VOLUME_SCALE = [
    [0.00, "#0b1220"],
    [0.20, "#0e2437"],
    [0.40, "#0f3b52"],
    [0.60, "#0d5a76"],
    [0.80, "#068fae"],
    [1.00, ACCENT],
]

#: Fear & Greed gauge: a genuine diverging scale (fear <-> greed) with a
#: neutral midpoint, so 50 reads as "nothing to see here".
FEAR_GREED_BANDS = (
    (0, 25, RED, "Extreme Fear"),
    (25, 45, "#e8825f", "Fear"),
    (45, 55, "#6b7a94", "Neutral"),
    (55, 75, "#4fc79a", "Greed"),
    (75, 100, GREEN, "Extreme Greed"),
)

MONO_STACK = (
    "'JetBrains Mono', 'Fira Code', ui-monospace, SFMono-Regular, "
    "'SF Mono', Menlo, Consolas, monospace"
)


def color_for_change(value: float | None) -> str:
    """Up/down/flat ink for a signed number."""
    if value is None:
        return TEXT_MUTED
    if value > 0:
        return GREEN
    if value < 0:
        return RED
    return TEXT_SECONDARY


def build_template() -> go.layout.Template:
    """The Plotly template every figure inherits.

    Recessive chrome by design: solid hairline gridlines one shade off the
    surface, no plot border, no dashed rules.
    """
    return go.layout.Template(
        layout=go.Layout(
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(family=MONO_STACK, size=12, color=TEXT_SECONDARY),
            margin=dict(l=52, r=18, t=34, b=38),
            xaxis=dict(
                gridcolor=GRID, gridwidth=1, griddash="solid",
                zeroline=False, linecolor=BORDER, ticks="outside",
                tickcolor=BORDER, ticklen=4, tickfont=dict(size=11),
            ),
            yaxis=dict(
                gridcolor=GRID, gridwidth=1, griddash="solid",
                zeroline=False, linecolor=BORDER, ticks="outside",
                tickcolor=BORDER, ticklen=4, tickfont=dict(size=11),
            ),
            legend=dict(
                orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0,
                bgcolor="rgba(0,0,0,0)", font=dict(size=11, color=TEXT_SECONDARY),
            ),
            hoverlabel=dict(
                bgcolor=SURFACE_RAISED,
                bordercolor=BORDER,
                font=dict(family=MONO_STACK, size=12, color=TEXT),
            ),
            colorway=list(SERIES),
        )
    )


TEMPLATE = build_template()

#: Passed to every ``dcc.Graph`` — a static-looking terminal, not a toolbar demo.
GRAPH_CONFIG = {"displayModeBar": False, "responsive": True, "scrollZoom": False}
