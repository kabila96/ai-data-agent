"""Statistical analysis with explicit assumptions and sample-size guards."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np
import pandas as pd
from scipy import stats as sps

MIN_GROUP_N = 5
MIN_CORR_N = 10


@dataclass
class AnalysisResult:
    method: str
    table: pd.DataFrame
    summary: dict[str, Any]
    assumptions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    observations: int = 0


def _require(df: pd.DataFrame, *cols: str | None) -> None:
    missing = [c for c in cols if c and c not in df.columns]
    if missing:
        raise KeyError(f"Column(s) not found: {', '.join(missing)}. Available: {', '.join(map(str, df.columns))}")


def _numeric(df: pd.DataFrame, col: str) -> pd.Series:
    series = pd.to_numeric(df[col], errors="coerce")
    return series.astype(float)


def descriptive(df: pd.DataFrame, columns: list[str] | None = None) -> AnalysisResult:
    cols = columns or [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    _require(df, *cols)
    if not cols:
        raise ValueError("No numeric columns to describe.")
    rows = []
    for c in cols:
        s = _numeric(df, c)
        v = s.dropna()
        rows.append({
            "column": c, "n": int(v.size), "missing": int(s.isna().sum()),
            "mean": v.mean(), "median": v.median(), "std": v.std(), "min": v.min(),
            "p25": v.quantile(0.25), "p75": v.quantile(0.75), "max": v.max(), "sum": v.sum(),
        })
    table = pd.DataFrame(rows)
    return AnalysisResult("descriptive statistics", table, {"columns": cols},
                          ["Missing values excluded per column."], [], len(df))


def group_summary(df: pd.DataFrame, group: str, value: str, agg: str = "sum") -> AnalysisResult:
    _require(df, group, value)
    data = df[[group]].assign(_v=_numeric(df, value))
    grouped = data.groupby(group, dropna=False)["_v"]
    table = pd.DataFrame({
        "n": grouped.size(), "total": grouped.sum(), "mean": grouped.mean(), "median": grouped.median(),
    }).reset_index()
    sort_col = {"sum": "total", "mean": "mean", "median": "median", "count": "n"}.get(agg, "total")
    table = table.sort_values(sort_col, ascending=False)
    grand = table["total"].sum()
    table["share_of_total_%"] = (table["total"] / grand * 100) if grand else np.nan
    warnings = []
    if (table["n"] < MIN_GROUP_N).any():
        warnings.append(f"Some groups have fewer than {MIN_GROUP_N} observations; their averages are unstable.")
    return AnalysisResult(f"group summary of {value} by {group}", table,
                          {"groups": int(len(table)), "grand_total": grand}, [], warnings, len(df))


def compare_groups(df: pd.DataFrame, group: str, value: str) -> AnalysisResult:
    """Welch t-test (2 groups) or Kruskal-Wallis (3+ groups), with size guards."""
    _require(df, group, value)
    data = df[[group]].assign(_v=_numeric(df, value)).dropna()
    samples = {str(k): g["_v"].to_numpy() for k, g in data.groupby(group)}
    table = pd.DataFrame(
        [{"group": k, "n": v.size, "mean": v.mean(), "median": np.median(v), "std": v.std(ddof=1) if v.size > 1 else np.nan}
         for k, v in samples.items()]
    ).sort_values("mean", ascending=False)
    warnings: list[str] = []
    assumptions = ["Observations are assumed independent.", "This tests association, not causation."]
    usable = {k: v for k, v in samples.items() if v.size >= MIN_GROUP_N}
    summary: dict[str, Any] = {"groups": len(samples), "groups_tested": len(usable)}
    if len(usable) < 2:
        warnings.append(f"Fewer than two groups with ≥{MIN_GROUP_N} observations; no test performed.")
        return AnalysisResult("group comparison (descriptive only)", table, summary, assumptions, warnings, len(data))
    if len(usable) < len(samples):
        warnings.append(f"{len(samples) - len(usable)} small group(s) excluded from the test.")
    if len(usable) == 2:
        (a_name, a), (b_name, b) = usable.items()
        stat, p = sps.ttest_ind(a, b, equal_var=False)
        method = f"Welch two-sample t-test ({a_name} vs {b_name})"
        assumptions.append("Welch's t-test does not assume equal variances; it is robust for moderate samples.")
        summary.update({"t_statistic": float(stat), "p_value": float(p), "mean_difference": float(a.mean() - b.mean())})
    else:
        stat, p = sps.kruskal(*usable.values())
        method = "Kruskal-Wallis H-test (difference in distributions across groups)"
        assumptions.append("Kruskal-Wallis is rank-based and does not assume normality.")
        summary.update({"h_statistic": float(stat), "p_value": float(p)})
    summary["significant_at_0.05"] = bool(p < 0.05)
    return AnalysisResult(method, table, summary, assumptions, warnings, len(data))


def correlation(df: pd.DataFrame, columns: list[str] | None = None,
                method: Literal["pearson", "spearman"] = "spearman") -> AnalysisResult:
    cols = columns or [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    _require(df, *cols)
    if len(cols) < 2:
        raise ValueError("Correlation needs at least two numeric columns.")
    data = pd.DataFrame({c: _numeric(df, c) for c in cols}).dropna()
    n = len(data)
    warnings = []
    if n < MIN_CORR_N:
        warnings.append(f"Only {n} complete observations; correlation not computed (need ≥{MIN_CORR_N}).")
        return AnalysisResult("correlation", pd.DataFrame(), {"n": n}, [], warnings, n)
    matrix = data.corr(method=method)
    pairs = []
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            fn = sps.spearmanr if method == "spearman" else sps.pearsonr
            r, p = fn(data[a], data[b])
            pairs.append({"var_1": a, "var_2": b, "r": float(r), "p_value": float(p), "n": n})
    pair_table = pd.DataFrame(pairs).sort_values("r", key=np.abs, ascending=False)
    assumptions = [
        "Correlation measures association only; it does not show that one variable causes another.",
        "Spearman is rank-based and robust to outliers" if method == "spearman"
        else "Pearson assumes a linear relationship and is sensitive to outliers",
    ]
    return AnalysisResult(f"{method} correlation", pair_table,
                          {"n": n, "matrix": matrix.round(3).to_dict()}, assumptions, warnings, n)


def time_trend(
    df: pd.DataFrame, date_col: str, value_col: str, freq: Literal["D", "W", "MS", "QS", "YS"] = "MS",
    agg: Literal["sum", "mean", "count"] = "sum", rolling: int | None = 3, group: str | None = None,
) -> AnalysisResult:
    _require(df, date_col, value_col, group)
    data = df.assign(_d=pd.to_datetime(df[date_col], errors="coerce"), _v=_numeric(df, value_col))
    invalid = int(data["_d"].isna().sum())
    data = data.dropna(subset=["_d"])
    keys = [pd.Grouper(key="_d", freq=freq)] + ([group] if group else [])
    series = data.groupby(keys)["_v"].agg(agg).reset_index().rename(columns={"_d": "period", "_v": value_col})
    sort_keys = ([group] if group else []) + ["period"]
    series = series.sort_values(sort_keys)
    by = series.groupby(group)[value_col] if group else series[value_col]
    series["pct_change_%"] = by.pct_change() * 100
    if rolling and rolling > 1:
        series[f"rolling_{rolling}"] = (
            by.transform(lambda s: s.rolling(rolling, min_periods=rolling).mean()) if group
            else series[value_col].rolling(rolling, min_periods=rolling).mean()
        )
    warnings = []
    if invalid:
        warnings.append(f"{invalid} row(s) with invalid or missing dates were excluded.")
    periods = series["period"].nunique()
    if periods < 3:
        warnings.append("Fewer than three periods; a trend cannot be assessed.")
    # Partial-period check: the last period may be incomplete.
    if periods and freq in ("MS", "QS", "YS", "W"):
        last = series["period"].max()
        latest_date = data["_d"].max()
        expected_end = last + pd.tseries.frequencies.to_offset(freq) - pd.Timedelta(days=1)
        if latest_date < expected_end:
            warnings.append(f"The latest period starting {last:%Y-%m-%d} may be incomplete (data end {latest_date:%Y-%m-%d}).")
    summary: dict[str, Any] = {
        "periods": int(periods),
        "start": data["_d"].min().isoformat() if len(data) else None,
        "end": data["_d"].max().isoformat() if len(data) else None,
    }
    if not group and periods >= 3:
        y = series[value_col].to_numpy(dtype=float)
        x = np.arange(len(y))
        mask = ~np.isnan(y)
        if mask.sum() >= 3:
            slope, intercept, r, p, se = sps.linregress(x[mask], y[mask])
            summary.update({"linear_slope_per_period": float(slope), "trend_p_value": float(p), "r_squared": float(r**2)})
    return AnalysisResult(f"{agg} of {value_col} per {freq} period", series, summary,
                          ["Periods are calendar-based; gaps with no records appear as missing periods."],
                          warnings, len(data))


def contribution(df: pd.DataFrame, group: str, value: str, period_col: str,
                 current: str, previous: str) -> AnalysisResult:
    """Which groups explain the change in `value` between two periods?"""
    _require(df, group, value, period_col)
    data = df.assign(_v=_numeric(df, value), _p=df[period_col].astype(str))
    pivot = data[data["_p"].isin([current, previous])].pivot_table(
        index=group, columns="_p", values="_v", aggfunc="sum", fill_value=0)
    for col in (current, previous):
        if col not in pivot.columns:
            pivot[col] = 0.0
    pivot["change"] = pivot[current] - pivot[previous]
    total_change = pivot["change"].sum()
    pivot["share_of_change_%"] = pivot["change"] / total_change * 100 if total_change else np.nan
    table = pivot.reset_index().sort_values("change")
    return AnalysisResult(f"contribution to change in {value} ({previous} → {current})", table,
                          {"total_change": float(total_change)}, [], [], len(data))


def regression(df: pd.DataFrame, target: str, predictors: list[str]) -> AnalysisResult:
    """Ordinary least squares with coefficient standard errors, p-values and R²."""
    _require(df, target, *predictors)
    if not predictors:
        raise ValueError("Regression needs at least one predictor.")
    cols = [target] + predictors
    data = pd.DataFrame({c: _numeric(df, c) for c in cols}).dropna()
    n, k = len(data), len(predictors)
    warnings: list[str] = []
    if n < max(20, 10 * k):
        warnings.append(f"Only {n} complete rows for {k} predictor(s); estimates are unstable (aim for ≥ 10 per predictor).")
    if n <= k + 1:
        raise ValueError("Too few observations to fit the model.")
    X = np.column_stack([np.ones(n), data[predictors].to_numpy()])
    y = data[target].to_numpy()
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    dof = n - k - 1
    sigma2 = float(resid @ resid) / dof
    cov = sigma2 * np.linalg.pinv(X.T @ X)
    se = np.sqrt(np.diag(cov))
    t = np.divide(beta, se, out=np.zeros_like(beta), where=se > 0)
    p = 2 * sps.t.sf(np.abs(t), dof)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - float(resid @ resid) / ss_tot if ss_tot else float("nan")
    adj = 1 - (1 - r2) * (n - 1) / dof if dof > 0 else float("nan")
    table = pd.DataFrame({"term": ["intercept"] + predictors, "coefficient": beta, "std_error": se,
                          "t": t, "p_value": p})
    if k > 1:
        corr = data[predictors].corr().abs().to_numpy()
        np.fill_diagonal(corr, 0)
        if (corr > 0.85).any():
            warnings.append("Some predictors are highly correlated (|r| > 0.85); individual coefficients are unreliable.")
    assumptions = [
        "Linear relationship, independent observations and roughly constant residual variance are assumed.",
        "Coefficients describe association, not causation.",
    ]
    return AnalysisResult(f"OLS regression of {target}", table,
                          {"n": n, "r_squared": r2, "adj_r_squared": adj, "residual_std": sigma2 ** 0.5},
                          assumptions, warnings, n)
