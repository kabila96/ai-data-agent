"""Time-series forecasting with honest validation.

Method
------
1. Aggregate to a regular period (day/week/month/quarter).
2. Hold out the last `holdout` periods.
3. Fit every candidate on the training part and score it on the holdout
   (MAE, RMSE, MAPE where defined).
4. Baselines always compete: naive (last value) and seasonal naive.
5. The best model is refitted on all data to produce the forecast. If no
   model beats the seasonal-naive baseline, the baseline is used and the
   answer says so.
6. Prediction intervals come from the holdout errors of the chosen model
   (empirical ~80% band), widened with horizon.

Candidates: naive, seasonal naive, drift, linear trend + seasonal dummies
(scikit-learn), and Holt-Winters exponential smoothing (statsmodels, when
installed).
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from analysis.stats import AnalysisResult

SEASON = {"D": 7, "W": 52, "MS": 12, "QS": 4, "YS": 1}
FREQ_NAME = {"D": "day", "W": "week", "MS": "month", "QS": "quarter", "YS": "year"}
Z80 = 1.2816


@dataclass
class ModelScore:
    name: str
    mae: float
    rmse: float
    mape: float | None
    holdout_pred: np.ndarray = field(repr=False, default_factory=lambda: np.array([]))


def _naive(train: np.ndarray, h: int, m: int) -> np.ndarray:
    return np.repeat(train[-1], h)


def _seasonal_naive(train: np.ndarray, h: int, m: int) -> np.ndarray:
    if m <= 1 or len(train) < m:
        return _naive(train, h, m)
    last = train[-m:]
    return np.array([last[i % m] for i in range(h)])


def _drift(train: np.ndarray, h: int, m: int) -> np.ndarray:
    if len(train) < 2:
        return _naive(train, h, m)
    slope = (train[-1] - train[0]) / (len(train) - 1)
    return train[-1] + slope * np.arange(1, h + 1)


def _trend_seasonal(train: np.ndarray, h: int, m: int) -> np.ndarray:
    from sklearn.linear_model import Ridge

    n = len(train)
    t = np.arange(n + h)
    X = t.reshape(-1, 1).astype(float)
    if m > 1 and n >= 2 * m:
        dummies = np.eye(m)[t % m][:, 1:]
        X = np.hstack([X, dummies])
    model = Ridge(alpha=1e-3).fit(X[:n], train)
    return model.predict(X[n:])


def _holt_winters(train: np.ndarray, h: int, m: int) -> np.ndarray:
    from statsmodels.tsa.holtwinters import ExponentialSmoothing

    seasonal = "add" if m > 1 and len(train) >= 2 * m else None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = ExponentialSmoothing(train, trend="add", damped_trend=True, seasonal=seasonal,
                                   seasonal_periods=m if seasonal else None,
                                   initialization_method="estimated").fit()
    return np.asarray(fit.forecast(h))


def candidate_models() -> dict[str, Callable[[np.ndarray, int, int], np.ndarray]]:
    models: dict[str, Callable[[np.ndarray, int, int], np.ndarray]] = {
        "naive (last value)": _naive,
        "seasonal naive": _seasonal_naive,
        "drift": _drift,
        "linear trend + seasonality": _trend_seasonal,
    }
    try:
        import statsmodels  # noqa: F401

        models["Holt-Winters (damped)"] = _holt_winters
    except ImportError:
        pass
    return models


def _metrics(actual: np.ndarray, pred: np.ndarray) -> tuple[float, float, float | None]:
    err = actual - pred
    mae = float(np.mean(np.abs(err)))
    rmse = float(np.sqrt(np.mean(err ** 2)))
    nonzero = np.abs(actual) > 1e-9
    mape = float(np.mean(np.abs(err[nonzero] / actual[nonzero])) * 100) if nonzero.all() else None
    return mae, rmse, mape


def forecast(
    df: pd.DataFrame, date_col: str, value_col: str, horizon: int = 30, freq: str = "D",
    agg: str = "sum", holdout: int | None = None,
) -> AnalysisResult:
    for col in (date_col, value_col):
        if col not in df.columns:
            raise KeyError(f"Column '{col}' not found. Available: {', '.join(map(str, df.columns))}")
    if freq not in SEASON:
        raise ValueError(f"freq must be one of {', '.join(SEASON)}")
    horizon = int(max(1, min(horizon, 365)))
    data = df.assign(_d=pd.to_datetime(df[date_col], errors="coerce"),
                     _v=pd.to_numeric(df[value_col], errors="coerce"))
    invalid_dates = int(data["_d"].isna().sum())
    data = data.dropna(subset=["_d"])
    series = data.set_index("_d")["_v"].resample(freq).agg(agg)
    warnings_out: list[str] = []
    if invalid_dates:
        warnings_out.append(f"{invalid_dates} row(s) with invalid dates were excluded.")
    # Drop a trailing partial period: it would look like a sudden fall.
    last_date = data["_d"].max()
    if freq != "D" and len(series):
        period_end = series.index[-1] + pd.tseries.frequencies.to_offset(freq) - pd.Timedelta(days=1)
        if last_date < period_end:
            series = series.iloc[:-1]
            warnings_out.append(f"The incomplete final {FREQ_NAME[freq]} was excluded from fitting.")
    gaps = int(series.isna().sum())
    if gaps:
        warnings_out.append(f"{gaps} period(s) had no data and were filled by interpolation.")
        series = series.interpolate(limit_direction="both")
    y = series.to_numpy(dtype=float)
    n = len(y)
    m = SEASON[freq]
    min_needed = max(12, 2 * min(m, 26) if m > 1 else 12)
    if n < 8:
        raise ValueError(f"Only {n} {FREQ_NAME[freq]}s of history; at least 8 are needed to forecast at all.")
    if n < min_needed:
        warnings_out.append(f"Only {n} {FREQ_NAME[freq]}s of history; seasonality cannot be estimated reliably "
                            f"(ideally ≥ {min_needed}). Treat the forecast as indicative.")
    if horizon > n / 2:
        warnings_out.append(f"The horizon ({horizon}) is long relative to the history ({n}); uncertainty grows quickly.")
    holdout = int(holdout or max(2, min(horizon, n // 4)))
    holdout = min(holdout, n - 4)
    train, test = y[:-holdout], y[-holdout:]

    scores: list[ModelScore] = []
    for name, fn in candidate_models().items():
        try:
            pred = np.asarray(fn(train, holdout, m), dtype=float)
            if pred.shape != test.shape or not np.isfinite(pred).all():
                continue
            mae, rmse, mape = _metrics(test, pred)
            scores.append(ModelScore(name, mae, rmse, mape, pred))
        except Exception as exc:  # a failing candidate must not break forecasting
            warnings_out.append(f"{name} could not be fitted ({type(exc).__name__}).")
    scores.sort(key=lambda s: s.mae)
    baseline = next(s for s in scores if s.name == "seasonal naive")
    best = scores[0]
    beats_baseline = best.name == baseline.name or best.mae < baseline.mae * 0.98
    if not beats_baseline:
        best = baseline
    chosen_fn = candidate_models()[best.name]
    future = np.asarray(chosen_fn(y, horizon, m), dtype=float)
    resid = test - best.holdout_pred
    sigma = float(np.std(resid, ddof=1)) if len(resid) > 1 else float(np.abs(resid).mean() or 0)
    widen = np.sqrt(np.arange(1, horizon + 1) / max(1, holdout)).clip(min=1)
    lower, upper = future - Z80 * sigma * widen, future + Z80 * sigma * widen
    if (y >= 0).all():
        lower = np.maximum(lower, 0)

    future_index = pd.date_range(series.index[-1], periods=horizon + 1, freq=freq)[1:]
    observed = pd.DataFrame({"period": series.index, "observed": y, "fitted": np.nan,
                             "forecast": np.nan, "lower": np.nan, "upper": np.nan, "type": "observed"})
    observed.loc[observed.index[-holdout:], "fitted"] = best.holdout_pred
    fut = pd.DataFrame({"period": future_index, "observed": np.nan, "fitted": np.nan, "forecast": future,
                        "lower": lower, "upper": upper, "type": "forecast"})
    table = pd.concat([observed, fut], ignore_index=True)

    leaderboard = [{"model": s.name, "holdout_MAE": round(s.mae, 4), "holdout_RMSE": round(s.rmse, 4),
                    "holdout_MAPE_%": None if s.mape is None else round(s.mape, 2)} for s in scores]
    summary = {
        "chosen_model": best.name,
        "beats_seasonal_naive_baseline": bool(beats_baseline and best.name != baseline.name),
        "holdout_periods": holdout, "history_periods": n, "horizon": horizon, "frequency": FREQ_NAME[freq],
        "holdout_MAE": round(best.mae, 4), "holdout_MAPE_%": None if best.mape is None else round(best.mape, 2),
        "forecast_total": round(float(future.sum()), 4),
        "forecast_mean_per_period": round(float(future.mean()), 4),
        "interval": "approximate 80% prediction interval from holdout errors",
        "leaderboard": leaderboard,
        "history_start": series.index[0].isoformat(), "history_end": series.index[-1].isoformat(),
    }
    if not summary["beats_seasonal_naive_baseline"]:
        warnings_out.append("No model clearly beat the seasonal-naive baseline on the holdout, so the "
                            "forecast simply repeats the recent seasonal pattern.")
    assumptions = [
        "Past patterns (trend and seasonality) continue; external events are not modelled.",
        f"Accuracy was measured on the last {holdout} {FREQ_NAME[freq]}s, which the model did not see.",
        "Forecast values are predictions, not observed data.",
    ]
    return AnalysisResult(f"forecast of {value_col} ({best.name})", table, summary, assumptions, warnings_out, n)
