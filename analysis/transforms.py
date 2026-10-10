"""Re-playable analysis transforms.

A transform turns a base data frame into a derived one (e.g. a monthly trend).
Results store their transforms so dashboards can refresh from the original
source and recompute the same analysis.
"""

from __future__ import annotations

from typing import Any, Callable

import pandas as pd

from analysis import anomalies, forecasting, stats

TransformFn = Callable[[pd.DataFrame, dict[str, Any]], stats.AnalysisResult]

TRANSFORMS: dict[str, TransformFn] = {
    "descriptive": lambda df, p: stats.descriptive(df, p.get("columns")),
    "group_summary": lambda df, p: stats.group_summary(df, p["group"], p["value"], p.get("agg", "sum")),
    "compare_groups": lambda df, p: stats.compare_groups(df, p["group"], p["value"]),
    "correlation": lambda df, p: stats.correlation(df, p.get("columns"), p.get("method", "spearman")),
    "time_trend": lambda df, p: stats.time_trend(
        df, p["date_col"], p["value"], p.get("freq", "MS"), p.get("agg", "sum"), p.get("rolling", 3), p.get("group")
    ),
    "contribution": lambda df, p: stats.contribution(
        df, p["group"], p["value"], p["period_col"], p["current"], p["previous"]
    ),
    "regression": lambda df, p: stats.regression(df, p["target"], p["predictors"]),
    "forecast": lambda df, p: forecasting.forecast(
        df, p["date_col"], p["value"], p.get("horizon", 30), p.get("freq", "D"), p.get("agg", "sum"), p.get("holdout")
    ),
    "anomalies": lambda df, p: anomalies.detect(
        df, p.get("columns", []), p.get("method", "auto"), p.get("date_col"), p.get("threshold"),
        p.get("contamination", 0.01), p.get("id_columns"),
    ),
}


def run_transform(df: pd.DataFrame, method: str, params: dict[str, Any]) -> stats.AnalysisResult:
    if method not in TRANSFORMS:
        raise ValueError(f"Unknown analysis method '{method}'. Options: {', '.join(TRANSFORMS)}")
    return TRANSFORMS[method](df, params)


def replay(df: pd.DataFrame, transforms: list[dict[str, Any]]) -> pd.DataFrame:
    for step in transforms:
        df = run_transform(df, step["method"], step.get("params", {})).table
    return df
