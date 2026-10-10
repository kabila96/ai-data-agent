"""Choose a chart that answers the question, or decide that a table is better.

The selector looks at column roles (time, category, measure) and the stated
intent. It returns `None` for the chart type when no chart would add meaning,
for example a wide table of identifiers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pandas as pd

from analysis.profiling import semantic_type

_INTENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("correlation", re.compile(r"\b(correlat|relationship between|associated with)", re.I)),
    ("distribution", re.compile(r"\b(distribution|spread|histogram|skew|range of)", re.I)),
    ("compare_distribution", re.compile(r"\b(box ?plot|variation by|spread by|compare distributions?)", re.I)),
    ("trend", re.compile(r"\b(trend|over time|monthly|weekly|daily|quarterly|yearly|by month|by week|time series)", re.I)),
    ("ranking", re.compile(r"\b(top|bottom|rank|highest|lowest|best|worst|most|least)\b", re.I)),
    ("kpi", re.compile(r"\b(total|how many|overall|kpi)\b", re.I)),
    ("map", re.compile(r"\b(map|choropleth|spatial|where)\b", re.I)),
    ("scatter", re.compile(r"\b(scatter|versus|vs\.?|against)\b", re.I)),
    ("heatmap", re.compile(r"\b(heat ?map|matrix|by .* and )", re.I)),
]


@dataclass
class ChartRecommendation:
    chart_type: str | None
    x: str | None = None
    y: str | None = None
    color: str | None = None
    value: str | None = None
    columns: list[str] | None = None
    label: str | None = None
    reason: str = ""


def detect_intent(question: str) -> list[str]:
    return [name for name, pattern in _INTENT_PATTERNS if pattern.search(question or "")]


def column_roles(df: pd.DataFrame) -> dict[str, list[str]]:
    roles: dict[str, list[str]] = {"time": [], "category": [], "measure": [], "identifier": [], "geometry": [], "text": []}
    for col in df.columns:
        sem = semantic_type(df[col])
        name = str(col).lower()
        if sem == "geometry":
            roles["geometry"].append(col)
        elif sem == "datetime" or (sem != "numeric" and re.search(r"(date|month|period|week|year)$", name)):
            roles["time"].append(col)
        elif sem == "numeric":
            if re.search(r"(^id$|_id$|code$|year$)", name) and not re.search(r"(count|total|sum|n_)", name):
                roles["identifier"].append(col) if not name.endswith("year") else roles["time"].append(col)
            else:
                roles["measure"].append(col)
        elif sem in ("category", "boolean"):
            roles["category"].append(col)
        else:
            roles["text"].append(col) if df[col].nunique() > 50 else roles["category"].append(col)
    return roles


def recommend_chart(df: pd.DataFrame, question: str = "") -> ChartRecommendation:
    if df is None or df.empty:
        return ChartRecommendation(None, reason="No data to chart.")
    intents = detect_intent(question)
    roles = column_roles(df)
    time, cats, measures = roles["time"], roles["category"], roles["measure"]
    n = len(df)

    if {"period", "observed", "forecast"} <= set(df.columns):
        return ChartRecommendation("forecast", reason="Forecast output: observed history with predictions and interval.")
    if roles["geometry"] and measures:
        return ChartRecommendation("choropleth", value=measures[0], label=cats[0] if cats else None,
                                   reason="Areas with a measure: a choropleth shows the spatial pattern.")
    if n == 1 and measures:
        return ChartRecommendation("kpi", value=measures[0], reason="A single value is best shown as a KPI card.")
    if "correlation" in intents and len(measures) >= 3:
        return ChartRecommendation("correlation", columns=measures[:12],
                                   reason="Several measures and a question about relationships.")
    if ("scatter" in intents or "correlation" in intents) and len(measures) >= 2 and n >= 10:
        return ChartRecommendation("scatter", x=measures[0], y=measures[1],
                                   color=cats[0] if cats and df[cats[0]].nunique() <= 8 else None,
                                   reason="Two measures: a scatterplot shows their relationship.")
    if time and measures:
        color = cats[0] if cats and 1 < df[cats[0]].nunique() <= 8 else None
        return ChartRecommendation("line", x=time[0], y=measures[0], color=color,
                                   reason="A time column and a measure: a line chart shows change over time.")
    if "distribution" in intents and measures and not cats:
        return ChartRecommendation("histogram", x=measures[0], reason="Distribution of a single measure.")
    if ("compare_distribution" in intents or "distribution" in intents) and measures and cats and n > 30:
        return ChartRecommendation("box", x=cats[0], y=measures[0], reason="Compare distributions across groups.")
    if len(cats) >= 2 and measures and df[cats[0]].nunique() <= 40 and df[cats[1]].nunique() <= 40:
        return ChartRecommendation("heatmap", x=cats[0], y=cats[1], value=measures[0],
                                   reason="Two categories and a measure: a heat map shows the cross-tab.")
    if cats and measures:
        unique = df[cats[0]].nunique()
        if unique <= 40:
            return ChartRecommendation("bar", x=cats[0], y=measures[0],
                                       reason="Categories with a measure: a sorted bar chart supports ranking.")
        return ChartRecommendation("ranked_table", columns=[cats[0], measures[0]],
                                   reason="Too many categories for a readable bar chart; showing a ranked table.")
    if len(measures) >= 2 and n >= 10:
        return ChartRecommendation("scatter", x=measures[0], y=measures[1],
                                   reason="Two measures: a scatterplot shows their relationship.")
    if len(measures) == 1 and n >= 20:
        return ChartRecommendation("histogram", x=measures[0], reason="Distribution of a single measure.")
    return ChartRecommendation(None, reason="No chart adds meaning here; the table is clearer.")
