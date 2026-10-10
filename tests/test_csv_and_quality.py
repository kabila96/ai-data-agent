"""CSV loading, type inference/overrides and data-quality profiling."""

from __future__ import annotations

import pandas as pd
import pytest

from analysis.csv_loader import apply_types, load_csv
from analysis.profiling import profile_dataframe
from security.errors import DataSourceError


def test_sample_csv_types_inferred(sample_csv_bytes):
    loaded = load_csv("branch_sales_upload.csv", sample_csv_bytes)
    t = loaded.inferred_types
    assert t["transaction_date"] == "datetime"
    assert t["branch_id"] == "identifier"
    assert t["units"] == "integer"
    assert t["revenue_gbp"] == "numeric"
    assert t["currency"] in ("category", "text")
    # UK day-first parsing: 01/03/2026 is 1 March, not 3 January.
    assert loaded.data["transaction_date"].iloc[0] == pd.Timestamp("2026-03-01")


def test_invalid_dates_reported_not_dropped(sample_csv_bytes):
    loaded = load_csv("branch_sales_upload.csv", sample_csv_bytes)
    assert len(loaded.data) == len(loaded.raw)  # nothing removed
    assert any("transaction_date" in w and "could not be read" in w for w in loaded.warnings)
    assert loaded.data["transaction_date"].isna().sum() == 2


def test_profile_finds_seeded_issues(sample_csv_bytes):
    loaded = load_csv("branch_sales_upload.csv", sample_csv_bytes)
    profile = profile_dataframe(loaded.data, invalid_date_counts={"transaction_date": 2})
    text = " | ".join(profile.warnings)
    assert profile.duplicate_rows == 8
    assert "missing value" in text
    assert "Inconsistent labels" in text and "branch" in text
    assert "negative value" in text and "units" in text
    assert "Mixed currencies" in text
    assert "extreme value" in text
    assert "invalid date" in text


def test_type_override_reports_failures():
    raw = pd.DataFrame({"a": ["1", "2", "x", None]}, dtype="string")
    converted, reports = apply_types(raw, {"a": "integer"})
    assert reports[0].failed == 1 and reports[0].examples == ["x"]
    assert converted["a"].isna().sum() == 2


def test_semicolon_and_cp1252():
    raw = "name;price\nCafé;3,50\nThé;4\n".encode("cp1252")
    loaded = load_csv("euro.csv", raw)
    assert list(loaded.data.columns) == ["name", "price"]
    assert loaded.data["name"].iloc[0] == "Café"


def test_duplicate_column_names_renamed():
    loaded = load_csv("dup.csv", b"a,a,b\n1,2,3\n")
    assert list(loaded.data.columns) == ["a", "a_1", "b"]
    assert any("renamed" in w for w in loaded.warnings)


def test_empty_and_oversize_rejected():
    with pytest.raises(DataSourceError):
        load_csv("empty.csv", b"   ")
    with pytest.raises(DataSourceError):
        load_csv("big.csv", b"a\n" + b"1\n" * 10, max_mb=0)


def test_small_sample_warning():
    profile = profile_dataframe(pd.DataFrame({"x": [1, 2, 3]}))
    assert any("Only 3 rows" in w for w in profile.warnings)


def test_key_duplicates_reported():
    df = pd.DataFrame({"id": [1, 1, 2], "v": [1, 2, 3]})
    profile = profile_dataframe(df, key_columns=["id"])
    assert any("Key column 'id'" in w for w in profile.warnings)
