"""Data-quality profiling for any DataFrame (CSV upload or query result).

The profile reports problems; it never modifies the data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

MIN_OBSERVATIONS = 30
_NON_NEGATIVE_HINT = re.compile(
    r"(qty|quantity|count|price|revenue|sales|amount|cost|units|stock|population|area|length|age)", re.I
)
_CURRENCY_HINT = re.compile(r"(currency|ccy|curr)$", re.I)
_DATE_HINT = re.compile(r"(date|time|_at$|_on$|timestamp)", re.I)


@dataclass
class ColumnProfile:
    name: str
    dtype: str
    semantic: str
    missing: int
    missing_pct: float
    unique: int
    examples: list[str]
    stats: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)


@dataclass
class DataProfile:
    rows: int
    columns: int
    duplicate_rows: int
    column_profiles: list[ColumnProfile]
    warnings: list[str]
    memory_mb: float

    def summary_table(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "column": c.name, "type": c.semantic, "missing": c.missing,
                    "missing_%": round(c.missing_pct, 1), "unique": c.unique,
                    "issues": "; ".join(c.issues), "examples": ", ".join(c.examples[:3]),
                }
                for c in self.column_profiles
            ]
        )

    def to_llm_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "columns": self.columns,
            "duplicate_rows": self.duplicate_rows,
            "warnings": self.warnings[:25],
            "columns_detail": [
                {
                    "name": c.name, "type": c.semantic, "missing": c.missing,
                    "unique": c.unique, "issues": c.issues[:5],
                    "stats": {k: _round(v) for k, v in c.stats.items()},
                }
                for c in self.column_profiles[:80]
            ],
        }


def _round(value: Any) -> Any:
    if isinstance(value, (float, np.floating)):
        return None if np.isnan(value) else round(float(value), 4)
    if isinstance(value, (np.integer,)):
        return int(value)
    return value if isinstance(value, (int, str, bool, type(None))) else str(value)


def semantic_type(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    if pd.api.types.is_numeric_dtype(series):
        return "numeric"
    if isinstance(series.dtype, pd.CategoricalDtype):
        return "category"
    sample = series.dropna().head(200)
    if len(sample) and sample.map(lambda v: isinstance(v, (bytes, bytearray, memoryview))).any():
        return "binary"
    if len(sample) and sample.astype(str).str.match(r"^(0[0-9]{3}|01)0000[0-9A-Fa-f]{2}").mean() > 0.8:
        return "geometry"
    if len(sample) and sample.astype(str).str.match(r'^\s*\{\s*"type"\s*:').mean() > 0.8:
        return "geometry"
    if len(sample) and sample.map(lambda v: hasattr(v, "isoformat")).all():
        return "datetime"
    unique = series.nunique(dropna=True)
    if unique <= max(30, int(len(series) * 0.05)):
        return "category"
    return "text"


def _numeric_profile(series: pd.Series, name: str, issues: list[str]) -> dict[str, Any]:
    values = pd.to_numeric(series, errors="coerce").dropna().astype(float)
    if values.empty:
        return {}
    q1, q3 = values.quantile([0.25, 0.75])
    iqr = q3 - q1
    lower, upper = q1 - 3 * iqr, q3 + 3 * iqr
    outliers = int(((values < lower) | (values > upper)).sum()) if iqr > 0 else 0
    if outliers:
        issues.append(f"{outliers} extreme value(s) beyond 3×IQR")
    negatives = int((values < 0).sum())
    if negatives and _NON_NEGATIVE_HINT.search(name):
        issues.append(f"{negatives} negative value(s) in a column that is normally non-negative")
    return {
        "mean": values.mean(), "median": values.median(), "std": values.std(),
        "min": values.min(), "max": values.max(), "q1": q1, "q3": q3,
        "outliers_3iqr": outliers, "negatives": negatives, "zeros": int((values == 0).sum()),
    }


def _category_profile(series: pd.Series, issues: list[str]) -> dict[str, Any]:
    values = series.dropna().astype(str)
    if values.empty:
        return {}
    normalised = values.str.strip().str.lower().str.replace(r"\s+", " ", regex=True)
    raw_unique, norm_unique = values.nunique(), normalised.nunique()
    if norm_unique < raw_unique:
        groups = (
            pd.DataFrame({"raw": values, "norm": normalised})
            .groupby("norm")["raw"].unique()
        )
        variants = [list(v) for v in groups if len(v) > 1][:3]
        issues.append(
            f"Inconsistent labels: {raw_unique - norm_unique} variant(s) differ only by case/spacing, e.g. "
            + "; ".join(" / ".join(map(str, v[:3])) for v in variants)
        )
    top = values.value_counts().head(5)
    return {"top_values": {str(k): int(v) for k, v in top.items()}}


def _datetime_profile(series: pd.Series, issues: list[str]) -> dict[str, Any]:
    values = pd.to_datetime(series, errors="coerce").dropna()
    if values.empty:
        return {}
    now = pd.Timestamp.now(tz=values.dt.tz) if values.dt.tz is not None else pd.Timestamp.now()
    future = int((values > now + pd.Timedelta(days=1)).sum())
    ancient = int((values < pd.Timestamp("1900-01-01", tz=values.dt.tz)).sum())
    if future:
        issues.append(f"{future} date(s) in the future")
    if ancient:
        issues.append(f"{ancient} date(s) before 1900")
    staleness = (now - values.max()).days
    return {
        "min": values.min().isoformat(), "max": values.max().isoformat(),
        "span_days": (values.max() - values.min()).days, "days_since_latest": staleness,
    }


def profile_dataframe(
    df: pd.DataFrame,
    key_columns: list[str] | None = None,
    invalid_date_counts: dict[str, int] | None = None,
) -> DataProfile:
    """Profile `df` and return structured findings and plain-language warnings."""
    warnings: list[str] = []
    rows = len(df)
    hashable = df.copy()
    for col in hashable.columns:
        if hashable[col].map(lambda v: isinstance(v, (list, dict, bytes, memoryview))).any():
            hashable[col] = hashable[col].astype(str)
    duplicate_rows = int(hashable.duplicated().sum()) if rows else 0

    profiles: list[ColumnProfile] = []
    currency_cols: list[str] = []
    for col in df.columns:
        series = df[col]
        semantic = semantic_type(series)
        missing = int(series.isna().sum())
        issues: list[str] = []
        stats: dict[str, Any] = {}
        if semantic == "numeric":
            stats = _numeric_profile(series, str(col), issues)
        elif semantic == "datetime":
            stats = _datetime_profile(series, issues)
        elif semantic in ("category", "text"):
            stats = _category_profile(series, issues)
            if _CURRENCY_HINT.search(str(col)) and series.nunique(dropna=True) > 1:
                currency_cols.append(str(col))
            if semantic == "text" and _DATE_HINT.search(str(col)):
                issues.append("Looks like a date column but is stored as text")
        if invalid_date_counts and invalid_date_counts.get(str(col)):
            issues.append(f"{invalid_date_counts[str(col)]} invalid date value(s)")
        if rows and missing / rows > 0.2:
            issues.append(f"{missing / rows:.0%} missing")
        if series.nunique(dropna=True) == 1 and rows > 1:
            issues.append("Constant column (single value)")
        examples = (
            series.dropna().astype(str).head(5).tolist() if semantic not in ("geometry", "binary") else ["<geometry>"]
        )
        profiles.append(
            ColumnProfile(
                name=str(col), dtype=str(series.dtype), semantic=semantic, missing=missing,
                missing_pct=(missing / rows * 100) if rows else 0.0,
                unique=int(series.nunique(dropna=True)) if semantic not in ("geometry", "binary") else 0,
                examples=[e[:60] for e in examples], stats=stats, issues=issues,
            )
        )

    if rows == 0:
        warnings.append("No rows to analyse.")
    elif rows < MIN_OBSERVATIONS:
        warnings.append(
            f"Only {rows} rows: statistical tests and trends may be unreliable (fewer than {MIN_OBSERVATIONS})."
        )
    if duplicate_rows:
        warnings.append(f"{duplicate_rows} fully duplicated row(s) found ({duplicate_rows / rows:.1%}).")
    total_missing = sum(p.missing for p in profiles)
    if total_missing:
        worst = sorted(profiles, key=lambda p: p.missing, reverse=True)[:3]
        warnings.append(
            f"{total_missing} missing value(s); most affected: "
            + ", ".join(f"{p.name} ({p.missing})" for p in worst if p.missing)
        )
    for col in currency_cols:
        values = ", ".join(sorted(df[col].dropna().astype(str).unique()[:5]))
        warnings.append(f"Mixed currencies/units in '{col}' ({values}); do not sum across them without conversion.")
    for key in key_columns or []:
        if key in df.columns:
            dups = int(df[key].duplicated(keep=False).sum())
            if dups:
                warnings.append(f"Key column '{key}' has {dups} rows sharing duplicated values.")
    for p in profiles:
        for issue in p.issues:
            if "missing" in issue and issue.endswith("missing"):
                continue
            warnings.append(f"{p.name}: {issue}")

    memory_mb = float(df.memory_usage(deep=True).sum()) / 1_048_576 if rows else 0.0
    return DataProfile(rows, len(df.columns), duplicate_rows, profiles, warnings, round(memory_mb, 2))


def duplicate_examples(df: pd.DataFrame, limit: int = 20) -> pd.DataFrame:
    mask = df.astype(str).duplicated(keep=False)
    return df[mask].head(limit)
