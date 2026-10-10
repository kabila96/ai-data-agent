"""Shared Plotly styling: a validated colour-blind-aware categorical order,
a single-hue sequential ramp, recessive grid lines and consistent fonts."""

from __future__ import annotations

import plotly.graph_objects as go
import plotly.io as pio

CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SEQUENTIAL_BLUE = [
    [0.0, "#cde2fb"], [0.17, "#9ec5f4"], [0.33, "#6da7ec"], [0.5, "#3987e5"],
    [0.67, "#256abf"], [0.83, "#184f95"], [1.0, "#0d366b"],
]
DIVERGING = [[0.0, "#1c5cab"], [0.5, "#f0efec"], [1.0, "#c2410c"]]
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e6e5e1"
SURFACE = "#fcfcfb"

TEMPLATE_NAME = "analytics"


def register_template() -> None:
    if TEMPLATE_NAME in pio.templates:
        return
    template = go.layout.Template(
        layout=go.Layout(
            font=dict(family="Inter, Segoe UI, Arial, sans-serif", size=13, color=TEXT_PRIMARY),
            title=dict(font=dict(size=17), x=0, xanchor="left"),
            colorway=CATEGORICAL,
            paper_bgcolor=SURFACE,
            plot_bgcolor=SURFACE,
            xaxis=dict(gridcolor=GRID, linecolor=GRID, zeroline=False, ticks="", title_font=dict(color=TEXT_SECONDARY)),
            yaxis=dict(gridcolor=GRID, linecolor=GRID, zeroline=False, ticks="", title_font=dict(color=TEXT_SECONDARY)),
            legend=dict(orientation="v", x=1.01, xanchor="left", y=1, yanchor="top", title_text=""),
            hoverlabel=dict(bgcolor="white", font_size=12),
            colorscale=dict(sequential=SEQUENTIAL_BLUE, diverging=DIVERGING),
            bargap=0.25,
            margin=dict(l=70, r=30, t=70, b=120),
        )
    )
    pio.templates[TEMPLATE_NAME] = template


register_template()
