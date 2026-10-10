"""Join diagnostics, key suggestions and CSV-vs-table comparison.

Joins are never executed on a guessed key: `suggest_join_keys` only proposes
candidates, and `join_frames` requires an explicit key pair that the user
approved in the UI (enforced by the tool layer).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Literal

import pandas as pd

JoinHow = Literal["inner", "left", "right", "outer"]


@dataclass
class JoinReport:
    left_name: str
    right_name: str
    left_key: str
    right_key: str
    how: JoinHow
    left_rows: int
    right_rows: int
    left_duplicate_keys: int
    right_duplicate_keys: int
    left_null_keys: int
    right_null_keys: int
    matched_left_rows: int
    unmatched_left_rows: int
    unmatched_right_rows: int
    result_rows: int
    multiplication_factor: float
    relationship: str
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class KeyCandidate:
    left_key: str
    right_key: str
    name_similarity: float
    overlap_ratio: float
    left_unique_ratio: float
    right_unique_ratio: float
    confidence: Literal["high", "medium", "low"]


def _normalise_key(series: pd.Series) -> pd.Series:
    """Compare keys as trimmed strings, removing a trailing '.0' from float-like integers."""
    s = series.astype("string").str.strip()
    return s.str.replace(r"^(-?\d+)\.0+$", r"\1", regex=True)


def join_report(
    left: pd.DataFrame, right: pd.DataFrame, left_key: str, right_key: str,
    how: JoinHow = "left", left_name: str = "left", right_name: str = "right",
) -> JoinReport:
    if left_key not in left.columns:
        raise KeyError(f"'{left_key}' is not a column of {left_name}")
    if right_key not in right.columns:
        raise KeyError(f"'{right_key}' is not a column of {right_name}")
    lk, rk = _normalise_key(left[left_key]), _normalise_key(right[right_key])
    l_valid, r_valid = lk.dropna(), rk.dropna()
    right_set, left_set = set(r_valid), set(l_valid)
    matched_left = int(lk.isin(right_set).sum())
    unmatched_left = int(len(lk) - matched_left)
    unmatched_right = int((~rk.isin(left_set)).sum())

    l_counts = l_valid.value_counts()
    r_counts = r_valid.value_counts()
    left_dup = int((l_counts > 1).sum())
    right_dup = int((r_counts > 1).sum())
    common = l_counts.index.intersection(r_counts.index)
    inner_rows = int((l_counts[common] * r_counts[common]).sum())
    if how == "inner":
        result_rows = inner_rows
    elif how == "left":
        result_rows = inner_rows + unmatched_left
    elif how == "right":
        result_rows = inner_rows + unmatched_right
    else:
        result_rows = inner_rows + unmatched_left + unmatched_right
    base = {"inner": min(len(left), len(right)) or 1, "left": len(left) or 1,
            "right": len(right) or 1, "outer": max(len(left), len(right)) or 1}[how]
    factor = round(result_rows / base, 3)

    relationship = (
        "one-to-one" if not left_dup and not right_dup
        else "many-to-one" if left_dup and not right_dup
        else "one-to-many" if right_dup and not left_dup
        else "many-to-many"
    )
    warnings: list[str] = []
    if relationship == "many-to-many":
        warnings.append(
            "Many-to-many join: duplicated keys on both sides will multiply rows and inflate totals."
        )
    elif relationship == "one-to-many" and how in ("left", "inner"):
        warnings.append(
            f"{right_name} has {right_dup} duplicated key value(s); rows from {left_name} will be repeated."
        )
    if factor > 1.0 and how in ("left", "inner"):
        warnings.append(f"The join produces {factor}× the rows of the base table; sums may be inflated.")
    if len(left) and matched_left == 0:
        warnings.append("No keys matched. Check that the key columns hold the same codes and formats.")
    elif len(left) and matched_left / len(left) < 0.8:
        warnings.append(f"Only {matched_left / len(left):.0%} of {left_name} rows found a match.")
    l_null, r_null = int(lk.isna().sum()), int(rk.isna().sum())
    if l_null or r_null:
        warnings.append(f"Null keys: {l_null} in {left_name}, {r_null} in {right_name} (these never match).")
    if str(left[left_key].dtype) != str(right[right_key].dtype):
        warnings.append(
            f"Key types differ ({left[left_key].dtype} vs {right[right_key].dtype}); keys were compared as text."
        )
    return JoinReport(
        left_name, right_name, left_key, right_key, how, len(left), len(right),
        left_dup, right_dup, l_null, r_null, matched_left, unmatched_left, unmatched_right,
        result_rows, factor, relationship, warnings,
    )


def join_frames(
    left: pd.DataFrame, right: pd.DataFrame, left_key: str, right_key: str, how: JoinHow = "left",
    left_name: str = "left", right_name: str = "right",
) -> tuple[pd.DataFrame, JoinReport]:
    report = join_report(left, right, left_key, right_key, how, left_name, right_name)
    l2, r2 = left.copy(), right.copy()
    l2["__key"] = _normalise_key(l2[left_key])
    r2["__key"] = _normalise_key(r2[right_key])
    merged = l2.merge(r2, on="__key", how=how, suffixes=("", f"_{right_name}"), indicator="_match")
    merged = merged.drop(columns="__key")
    merged["_match"] = merged["_match"].map(
        {"both": "matched", "left_only": f"{left_name} only", "right_only": f"{right_name} only"}
    ).astype("string")
    return merged, report


def suggest_join_keys(left: pd.DataFrame, right: pd.DataFrame, max_candidates: int = 5) -> list[KeyCandidate]:
    candidates: list[KeyCandidate] = []
    for lcol in left.columns:
        lvals = set(_normalise_key(left[lcol]).dropna().head(20_000))
        if not lvals:
            continue
        for rcol in right.columns:
            sim = difflib.SequenceMatcher(None, str(lcol).lower(), str(rcol).lower()).ratio()
            rvals = set(_normalise_key(right[rcol]).dropna().head(20_000))
            if not rvals:
                continue
            overlap = len(lvals & rvals) / max(1, min(len(lvals), len(rvals)))
            if overlap < 0.2 and sim < 0.8:
                continue
            l_unique = left[lcol].nunique() / max(1, left[lcol].notna().sum())
            r_unique = right[rcol].nunique() / max(1, right[rcol].notna().sum())
            score_high = overlap >= 0.9 and max(l_unique, r_unique) >= 0.99
            score_med = overlap >= 0.6
            confidence = "high" if score_high else "medium" if score_med else "low"
            candidates.append(KeyCandidate(str(lcol), str(rcol), round(sim, 2), round(overlap, 3),
                                           round(l_unique, 3), round(r_unique, 3), confidence))
    candidates.sort(key=lambda c: (c.overlap_ratio, c.name_similarity), reverse=True)
    return candidates[:max_candidates]


def compare_frames(
    csv_df: pd.DataFrame, db_df: pd.DataFrame, csv_key: str, db_key: str,
    csv_name: str = "csv", db_name: str = "table", max_diffs: int = 200,
) -> dict:
    """Compare a CSV with a database extract on an approved key."""
    report = join_report(csv_df, db_df, csv_key, db_key, "outer", csv_name, db_name)
    shared = sorted((set(csv_df.columns) & set(db_df.columns)) - {csv_key, db_key})
    only_csv = sorted(set(csv_df.columns) - set(db_df.columns))
    only_db = sorted(set(db_df.columns) - set(csv_df.columns))
    a = csv_df.assign(__key=_normalise_key(csv_df[csv_key])).drop_duplicates("__key").set_index("__key")
    b = db_df.assign(__key=_normalise_key(db_df[db_key])).drop_duplicates("__key").set_index("__key")
    common = a.index.intersection(b.index)
    diffs: list[dict] = []
    diff_counts: dict[str, int] = {}
    for col in shared:
        left = a.loc[common, col].astype("string").str.strip()
        right = b.loc[common, col].astype("string").str.strip()
        ln = pd.to_numeric(left, errors="coerce")
        rn = pd.to_numeric(right, errors="coerce")
        both_numeric = ln.notna() & rn.notna()
        differs = (left.fillna("<NA>") != right.fillna("<NA>"))
        differs &= ~(both_numeric & ((ln - rn).abs() <= 1e-9 + 1e-6 * rn.abs()))
        diff_counts[col] = int(differs.sum())
        for key in differs[differs].index[: max(0, max_diffs - len(diffs))]:
            diffs.append({"key": key, "column": col, csv_name: left[key], db_name: right[key]})
    return {
        "join": report.as_dict(),
        "shared_columns": shared,
        "columns_only_in_csv": only_csv,
        "columns_only_in_table": only_db,
        "matched_keys": int(len(common)),
        "value_differences_by_column": diff_counts,
        "difference_examples": pd.DataFrame(diffs),
    }
