"""Anomaly detection. Flags records; never removes or edits them.

Methods
-------
robust_z        |x - median| / (1.4826 * MAD) > threshold (default 3.5). Robust to the outliers themselves.
iqr             outside Q1 - k*IQR .. Q3 + k*IQR (default k = 3).
isolation_forest  multivariate (scikit-learn), for unusual combinations of several columns.
time_series     residual from a rolling median per period exceeds a robust threshold; catches spikes
                that are normal in absolute size but unusual for their time.
auto            time_series if a date column is given, isolation_forest for ≥2 columns, else robust_z.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

from analysis.stats import AnalysisResult

Method = Literal["auto", "robust_z", "iqr", "isolation_forest", "time_series"]
MIN_ROWS = 20


def _robust_z(values: pd.Series) -> pd.Series:
    med = values.median()
    mad = (values - med).abs().median()
    if not mad or np.isnan(mad):
        std = values.std()
        return (values - values.mean()) / std if std else pd.Series(0.0, index=values.index)
    return (values - med) / (1.4826 * mad)


def detect(
    df: pd.DataFrame, columns: list[str], method: Method = "auto", date_col: str | None = None,
    threshold: float | None = None, contamination: float = 0.01, id_columns: list[str] | None = None,
) -> AnalysisResult:
    if not columns:
        columns = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])][:6]
    missing = [c for c in columns + ([date_col] if date_col else []) if c not in df.columns]
    if missing:
        raise KeyError(f"Column(s) not found: {', '.join(missing)}")
    if not columns:
        raise ValueError("No numeric columns to check for anomalies.")
    if method == "auto":
        method = "time_series" if date_col else ("isolation_forest" if len(columns) >= 2 else "robust_z")

    data = df.copy()
    numeric = pd.DataFrame({c: pd.to_numeric(data[c], errors="coerce") for c in columns})
    usable = numeric.notna().all(axis=1)
    warnings: list[str] = []
    if (~usable).any():
        warnings.append(f"{int((~usable).sum())} row(s) with missing values in {', '.join(columns)} were not scored.")
    n = int(usable.sum())
    if n < MIN_ROWS:
        raise ValueError(f"Only {n} complete rows; at least {MIN_ROWS} are needed for anomaly detection.")

    score = pd.Series(np.nan, index=data.index)
    reason = pd.Series("", index=data.index, dtype="string")
    col = columns[0]
    if method == "robust_z":
        t = threshold or 3.5
        z = _robust_z(numeric.loc[usable, col])
        score.loc[usable] = z.abs()
        flags = score > t
        reason.loc[flags] = [f"{col} = {numeric.at[i, col]:,.4g} is {z.at[i]:+.1f} robust SDs from the median"
                             for i in score[flags].index]
        rule = f"|robust z| > {t} on {col}"
    elif method == "iqr":
        k = threshold or 3.0
        v = numeric.loc[usable, col]
        q1, q3 = v.quantile([0.25, 0.75])
        iqr = q3 - q1
        lo, hi = q1 - k * iqr, q3 + k * iqr
        dist = np.maximum(lo - v, v - hi).clip(lower=0) / (iqr or 1)
        score.loc[usable] = dist
        flags = score > 0
        reason.loc[flags] = [f"{col} = {numeric.at[i, col]:,.4g} outside {lo:,.4g} to {hi:,.4g}"
                             for i in score[flags].index]
        rule = f"outside Q1 − {k}·IQR … Q3 + {k}·IQR on {col}"
    elif method == "isolation_forest":
        from sklearn.ensemble import IsolationForest
        from sklearn.preprocessing import RobustScaler

        X = RobustScaler().fit_transform(numeric.loc[usable].to_numpy())
        model = IsolationForest(n_estimators=300, contamination=contamination, random_state=42).fit(X)
        raw = -model.score_samples(X)
        score.loc[usable] = raw
        labels = pd.Series(model.predict(X) == -1, index=numeric.loc[usable].index)
        flags = pd.Series(False, index=data.index)
        flags.loc[labels.index] = labels
        med = numeric.loc[usable].median()
        for i in flags[flags].index:
            parts = [f"{c} {numeric.at[i, c]:,.4g} (median {med[c]:,.4g})" for c in columns[:4]]
            reason.at[i] = "unusual combination: " + ", ".join(parts)
        rule = f"IsolationForest, contamination {contamination:.1%}, columns {', '.join(columns)}"
    elif method == "time_series":
        if not date_col:
            raise ValueError("time_series detection needs a date column.")
        t = threshold or 3.5
        order = pd.to_datetime(data[date_col], errors="coerce")
        ts = pd.DataFrame({"d": order, "v": numeric[col]}).loc[usable].sort_values("d")
        window = max(5, min(29, len(ts) // 10 * 2 + 1))
        baseline = ts["v"].rolling(window, center=True, min_periods=3).median()
        resid = ts["v"] - baseline
        z = _robust_z(resid.dropna())
        score.loc[z.index] = z.abs()
        flags = score > t
        reason.loc[flags] = [f"{col} = {numeric.at[i, col]:,.4g} vs local typical {baseline.at[i]:,.4g}"
                             for i in score[flags].index]
        rule = f"|robust z| of residual from a {window}-point rolling median > {t}"
    else:
        raise ValueError(f"Unknown method {method}")

    flags = flags.fillna(False).astype(bool)
    data["anomaly_score"] = score.round(4)
    data["is_anomaly"] = flags
    data["anomaly_reason"] = reason
    flagged = data[flags].sort_values("anomaly_score", ascending=False)
    share = len(flagged) / n
    if share > 0.05:
        warnings.append(f"{share:.1%} of rows were flagged; the rule may be too sensitive or the data heavy-tailed.")
    if len(flagged) == 0:
        warnings.append("No anomalies found with this rule.")
    keep = (id_columns or []) + [c for c in data.columns if c not in (id_columns or [])]
    table = flagged[keep] if len(flagged) else data.head(0)[keep]
    summary = {"method": method, "rule": rule, "rows_scored": n, "anomalies": int(len(flagged)),
               "share_flagged_%": round(share * 100, 2), "columns": columns}
    assumptions = ["Anomalies are statistically unusual, not necessarily errors or fraud; review each one.",
                   "No records were removed or changed."]
    return AnalysisResult(f"anomaly detection ({method})", table, summary, assumptions, warnings, n)
