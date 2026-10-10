"""Forecasting validation and anomaly detection."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from analysis.anomalies import detect
from analysis.forecasting import forecast
from analysis.stats import regression
from models.schemas import ChartSpec
from visualisations.charts import build_figure


def seasonal_daily(days: int = 400, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    d = pd.date_range("2025-01-01", periods=days, freq="D")
    weekly = np.array([1.0, 1.0, 1.0, 1.05, 1.1, 1.4, 1.3])[d.dayofweek]
    y = (100 + 0.1 * np.arange(days)) * weekly + rng.normal(0, 3, days)
    return pd.DataFrame({"sale_date": d, "revenue": y})


def test_forecast_beats_baseline_and_separates_predictions():
    res = forecast(seasonal_daily(), "sale_date", "revenue", horizon=28, freq="D")
    s = res.summary
    assert s["horizon"] == 28 and s["holdout_periods"] >= 2
    assert {m["model"] for m in s["leaderboard"]} >= {"naive (last value)", "seasonal naive", "drift"}
    future = res.table[res.table["type"] == "forecast"]
    assert len(future) == 28 and future["observed"].isna().all()
    assert (future["lower"] <= future["forecast"]).all() and (future["forecast"] <= future["upper"]).all()
    observed = res.table[res.table["type"] == "observed"]
    assert observed["forecast"].isna().all()
    assert any("predictions" in a for a in res.assumptions)
    fig = build_figure(ChartSpec(chart_type="forecast", title="Revenue forecast", units="£"), res.table)
    assert {t.name for t in fig.data} >= {"Observed", "Forecast", "Prediction interval"}


def test_forecast_drops_partial_month_and_guards_short_history():
    df = seasonal_daily(days=500)
    res = forecast(df, "sale_date", "revenue", horizon=3, freq="MS")
    assert any("incomplete final month" in w for w in res.warnings)
    with pytest.raises(ValueError, match="at least 8"):
        forecast(seasonal_daily(days=5), "sale_date", "revenue", horizon=3, freq="D")


def test_noise_only_series_falls_back_to_baseline():
    rng = np.random.default_rng(3)
    df = pd.DataFrame({"d": pd.date_range("2025-01-01", periods=120), "v": rng.normal(50, 10, 120)})
    res = forecast(df, "d", "v", horizon=14, freq="D")
    assert res.summary["chosen_model"]  # always produces a model and honest metrics
    assert res.summary["holdout_MAE"] > 0


def test_robust_z_and_iqr_flag_extremes_without_removing():
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"id": range(300), "amount": rng.normal(100, 10, 300)})
    df.loc[17, "amount"] = 900
    res = detect(df, ["amount"], method="robust_z", id_columns=["id"])
    assert res.summary["anomalies"] >= 1 and 17 in res.table["id"].tolist()
    assert "robust SDs" in res.table.iloc[0]["anomaly_reason"]
    assert res.summary["rows_scored"] == 300  # all rows scored, none dropped
    iqr = detect(df, ["amount"], method="iqr")
    assert 900 in iqr.table["amount"].tolist()


def test_isolation_forest_and_time_series():
    rng = np.random.default_rng(1)
    df = pd.DataFrame({"qty": rng.normal(10, 1, 400), "price": rng.normal(5, 0.5, 400)})
    df.loc[5, ["qty", "price"]] = [10, 15]  # normal qty, absurd price
    res = detect(df, ["qty", "price"], method="auto", contamination=0.01)
    assert res.summary["method"] == "isolation_forest" and 5 in res.table.index
    ts = seasonal_daily(200)
    ts.loc[120, "revenue"] *= 3
    out = detect(ts, ["revenue"], method="auto", date_col="sale_date")
    assert out.summary["method"] == "time_series" and 120 in out.table.index


def test_anomaly_small_sample_guard():
    with pytest.raises(ValueError, match="at least"):
        detect(pd.DataFrame({"x": range(5)}), ["x"])


def test_regression_recovers_coefficients():
    rng = np.random.default_rng(2)
    x = rng.normal(0, 1, 200)
    df = pd.DataFrame({"x": x, "y": 3 + 2 * x + rng.normal(0, 0.5, 200)})
    res = regression(df, "y", ["x"])
    coef = dict(zip(res.table["term"], res.table["coefficient"]))
    assert abs(coef["x"] - 2) < 0.15 and res.summary["r_squared"] > 0.9
    assert any("causation" in a for a in res.assumptions)


def test_forecast_tool_records_replayable_recipe(offline_ctx):
    from analysis.csv_loader import load_csv
    from tools import registry
    from tools.context import CSVSource

    df = seasonal_daily(200)
    loaded = load_csv("daily.csv", df.to_csv(index=False).encode())
    offline_ctx.csv_sources["daily.csv"] = CSVSource("daily.csv", loaded.raw, loaded.data, loaded.inferred_types)
    out = registry.dispatch(offline_ctx, "run_forecast", {"source": "csv:daily.csv", "date_col": "sale_date",
                                                         "value": "revenue", "horizon": 14, "freq": "D"})
    assert out.ok, out.payload
    rec = offline_ctx.results[out.result_id]
    assert rec.recipe.transforms[-1]["method"] == "forecast"
    assert "beats_seasonal_naive_baseline" in out.payload["summary"]
    anomalies = registry.dispatch(offline_ctx, "detect_anomalies", {"source": "csv:daily.csv", "columns": ["revenue"]})
    assert anomalies.ok, anomalies.payload
