"""Join diagnostics, approval enforcement and statistics guards."""

from __future__ import annotations

import pandas as pd

from analysis import stats
from analysis.joins import compare_frames, join_frames, join_report, suggest_join_keys
from tests.conftest import make_sales_frame
from tools import registry
from tools.context import CSVSource


def test_many_to_many_inflation_reported():
    left = pd.DataFrame({"k": [1, 1, 2], "a": [1, 2, 3]})
    right = pd.DataFrame({"k": [1, 1, 3], "b": [4, 5, 6]})
    report = join_report(left, right, "k", "k", "left")
    assert report.relationship == "many-to-many"
    assert report.result_rows == 5  # 2x2 for key 1 + one unmatched
    assert report.multiplication_factor > 1
    merged, _ = join_frames(left, right, "k", "k", "left")
    assert len(merged) == report.result_rows


def test_key_types_normalised_and_unmatched_counted():
    left = pd.DataFrame({"id": ["1", "2", "3"]})
    right = pd.DataFrame({"id": [1.0, 2.0, 9.0]})
    report = join_report(left, right, "id", "id", "inner")
    assert report.matched_left_rows == 2 and report.unmatched_right_rows == 1
    assert any("types differ" in w for w in report.warnings)


def test_suggest_keys_prefers_overlap():
    left = pd.DataFrame({"branch_id": [1, 2, 3], "x": ["a", "b", "c"]})
    right = pd.DataFrame({"id_branch": [1, 2, 3], "name": ["p", "q", "r"]})
    cands = suggest_join_keys(left, right)
    assert (cands[0].left_key, cands[0].right_key) == ("branch_id", "id_branch")


def test_compare_frames_finds_value_differences():
    a = pd.DataFrame({"id": [1, 2], "price": ["10.0", "20"]})
    b = pd.DataFrame({"id": [1, 2], "price": [10, 21]})
    result = compare_frames(a, b, "id", "id")
    assert result["value_differences_by_column"]["price"] == 1


def _ctx_with_csvs(ctx, sample_csv_bytes):
    from analysis.csv_loader import load_csv
    from pathlib import Path

    for name in ("branch_sales_upload.csv", "branch_targets.csv"):
        raw = (Path(__file__).resolve().parents[1] / "sample_data" / name).read_bytes()
        loaded = load_csv(name, raw)
        ctx.csv_sources[name] = CSVSource(name, loaded.raw, loaded.data, loaded.inferred_types, loaded.warnings)
    return ctx


def test_join_tool_requires_user_approval(offline_ctx, sample_csv_bytes):
    ctx = _ctx_with_csvs(offline_ctx, sample_csv_bytes)
    args = {"left_source": "csv:branch_sales_upload.csv", "right_source": "csv:branch_targets.csv"}
    out = registry.dispatch(ctx, "join_data_sources", args)
    assert out.payload["status"] == "keys_required" and out.payload["candidates"]

    args.update(left_key="branch_id", right_key="branch_id")
    out = registry.dispatch(ctx, "join_data_sources", args)
    assert out.requires_approval and out.result_id is None
    assert out.payload["join_report"]["relationship"] == "many-to-one" or out.payload["join_report"]["right_duplicate_keys"] == 1
    assert ctx.pending_approvals

    # The model cannot approve on the user's behalf: extra arguments are ignored.
    out = registry.dispatch(ctx, "join_data_sources", {**args, "approved": True})
    assert out.requires_approval

    ctx.approved_joins.add(("csv:branch_sales_upload.csv", "csv:branch_targets.csv", "branch_id", "branch_id"))
    out = registry.dispatch(ctx, "join_data_sources", args)
    assert out.ok and out.result_id and out.payload["status"] == "joined"
    assert any("repeated" in w or "inflated" in w for w in out.warnings)


def test_compare_groups_small_sample_guard():
    df = pd.DataFrame({"g": ["a", "a", "b", "b"], "v": [1, 2, 3, 4]})
    res = stats.compare_groups(df, "g", "v")
    assert "p_value" not in res.summary
    assert res.warnings


def test_compare_groups_welch():
    res = stats.compare_groups(make_sales_frame(), "branch", "revenue")
    assert "Welch" in res.method and res.summary["p_value"] < 0.05
    assert any("causation" in a for a in res.assumptions)


def test_correlation_reports_n_and_caveat():
    res = stats.correlation(make_sales_frame(), ["revenue", "units"])
    assert res.observations == 240
    assert any("does not show" in a for a in res.assumptions)


def test_time_trend_with_partial_period_warning():
    df = make_sales_frame()
    df = df[df["sale_date"] < "2025-04-20"]  # April is incomplete
    res = stats.time_trend(df, "sale_date", "revenue", "MS", "sum", 2)
    assert res.summary["periods"] == 4
    assert any("incomplete" in w for w in res.warnings)
    assert "pct_change_%" in res.table.columns and "rolling_2" in res.table.columns


def test_stats_tool_records_transform(offline_ctx, sample_csv_bytes):
    ctx = _ctx_with_csvs(offline_ctx, sample_csv_bytes)
    out = registry.dispatch(ctx, "run_statistical_analysis", {
        "source": "csv:branch_sales_upload.csv", "method": "time_trend",
        "params": {"date_col": "transaction_date", "value": "revenue_gbp", "freq": "MS"},
    })
    assert out.ok, out.payload
    record = ctx.results[out.result_id]
    assert record.recipe.kind == "csv" and record.recipe.transforms[0]["method"] == "time_trend"
    assert out.payload["observations"] > 5000
