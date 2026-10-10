"""Build Plotly figures from a validated `ChartSpec`.

Every figure carries a title, labelled axes with units, and a footer with the
data source, applied filters and date range. Interpretation text and
data-quality warnings are rendered by the UI next to the figure and included
in exports.
"""

from __future__ import annotations

import textwrap
import warnings

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from models.schemas import ChartSpec
from visualisations.theme import CATEGORICAL, DIVERGING, SEQUENTIAL_BLUE, TEMPLATE_NAME, TEXT_SECONDARY

MAX_BAR_CATEGORIES = 40
MAX_LINE_SERIES = 8
_CURRENCY_PREFIXES = {"£", "$", "€", "GBP", "USD", "EUR"}


class ChartError(ValueError):
    """Raised when a chart cannot be drawn meaningfully from the data."""


def _label(spec: ChartSpec, axis: str) -> str:
    column = getattr(spec, axis)
    explicit = getattr(spec, f"{axis}_label", None)
    label = explicit or (str(column).replace("_", " ").capitalize() if column else "")
    if axis in ("y",) and spec.units and spec.units not in label:
        label = f"{label} ({spec.units})"
    return label


def _require_columns(df: pd.DataFrame, *columns: str | None) -> None:
    missing = [c for c in columns if c and c not in df.columns]
    if missing:
        raise ChartError(
            f"Column(s) {', '.join(missing)} not in the data. Available: {', '.join(map(str, df.columns))}"
        )


def _require_numeric(df: pd.DataFrame, column: str | None, role: str) -> None:
    if column and not pd.api.types.is_numeric_dtype(pd.to_numeric(df[column], errors="coerce")):
        raise ChartError(f"The {role} '{column}' must be numeric.")
    if column and pd.to_numeric(df[column], errors="coerce").notna().sum() == 0:
        raise ChartError(f"The {role} '{column}' has no numeric values.")


def _aggregate(df: pd.DataFrame, keys: list[str], value: str, how: str) -> pd.DataFrame:
    func = {"sum": "sum", "mean": "mean", "count": "count", "median": "median", "min": "min", "max": "max"}[how]
    data = df.assign(**{value: pd.to_numeric(df[value], errors="coerce")})
    return data.groupby(keys, dropna=False, observed=True)[value].agg(func).reset_index()


def footer_text(spec: ChartSpec, notes: list[str] | None = None) -> str:
    parts = [f"Source: {spec.source or 'not specified'}", f"Filters: {spec.filters_text or 'None'}"]
    if spec.date_range:
        parts.append(f"Date range: {spec.date_range}")
    if spec.units:
        parts.append(f"Units: {spec.units}")
    text = "  |  ".join(parts)
    if notes:
        text += "<br>" + "  ".join(notes)
    return "<br>".join(textwrap.wrap(text, 160, break_long_words=False, replace_whitespace=False)) if "<br>" not in text else text


def _finish(fig: go.Figure, spec: ChartSpec, notes: list[str]) -> go.Figure:
    fig.update_layout(
        template=TEMPLATE_NAME,
        title=dict(text=spec.title),
        legend=dict(title_text="", orientation="v", x=1.01, xanchor="left", y=1, yanchor="top"),
    )
    fig.update_xaxes(automargin=True)
    fig.update_yaxes(automargin=True)
    fig.add_annotation(
        text=footer_text(spec, notes), xref="paper", yref="paper", x=0, y=-0.22, xanchor="left",
        yanchor="top", showarrow=False, align="left", font=dict(size=11, color=TEXT_SECONDARY),
    )
    return fig


def build_figure(spec: ChartSpec, df: pd.DataFrame) -> go.Figure:
    if df is None or df.empty:
        raise ChartError("There is no data to chart.")
    if spec.chart_type in ("choropleth", "point_map", "proportional_map"):
        from geospatial.maps import MapError, build_map

        try:
            return build_map(spec, df.copy())
        except MapError as exc:
            raise ChartError(str(exc)) from exc
    builder = _BUILDERS.get(spec.chart_type)
    if builder is None:
        raise ChartError(f"Unknown chart type '{spec.chart_type}'.")
    notes: list[str] = []
    fig = builder(spec, df.copy(), notes)
    return _finish(fig, spec, notes)


# ------------------------------------------------------------------ builders
def _bar(spec: ChartSpec, df: pd.DataFrame, notes: list[str]) -> go.Figure:
    _require_columns(df, spec.x, spec.y, spec.color)
    if not spec.x or not spec.y:
        raise ChartError("A bar chart needs a category (x) and a measure (y).")
    _require_numeric(df, spec.y, "measure")
    keys = [spec.x] + ([spec.color] if spec.color else [])
    how = spec.aggregate
    if how == "none" and df.duplicated(keys).any():
        how = "sum"
        notes.append(f"Note: values summed per {spec.x}.")
    data = _aggregate(df, keys, spec.y, how) if how != "none" else df
    order = data.groupby(spec.x, observed=True)[spec.y].sum().sort_values(ascending=not spec.sort_desc)
    if spec.top_n and len(order) > spec.top_n:
        notes.append(f"Showing top {spec.top_n} of {len(order)} categories.")
        order = order.head(spec.top_n) if spec.sort_desc else order.tail(spec.top_n)
    elif len(order) > MAX_BAR_CATEGORIES:
        raise ChartError(
            f"{len(order)} categories are too many for a readable bar chart. Use top_n or a ranked table."
        )
    data = data[data[spec.x].isin(order.index)]
    horizontal = data[spec.x].astype(str).str.len().max() > 14 or len(order) > 12
    cat_order = list(order.index.astype(str))
    data = data.assign(**{spec.x: data[spec.x].astype(str)})
    if horizontal:
        fig = px.bar(data, x=spec.y, y=spec.x, color=spec.color, orientation="h",
                     category_orders={spec.x: cat_order},
                     color_discrete_sequence=CATEGORICAL)
        fig.update_yaxes(title_text=_label(spec, "x"))
        fig.update_xaxes(title_text=_label(spec, "y"))
    else:
        fig = px.bar(data, x=spec.x, y=spec.y, color=spec.color, category_orders={spec.x: cat_order},
                     color_discrete_sequence=CATEGORICAL)
        fig.update_xaxes(title_text=_label(spec, "x"))
        fig.update_yaxes(title_text=_label(spec, "y"))
    fig.update_traces(marker_line_width=0)
    return fig


def _line_like(spec: ChartSpec, df: pd.DataFrame, notes: list[str], area: bool) -> go.Figure:
    _require_columns(df, spec.x, spec.y, spec.color)
    if not spec.x or not spec.y:
        raise ChartError("A line chart needs an ordered x (usually time) and a measure (y).")
    _require_numeric(df, spec.y, "measure")
    if pd.api.types.is_numeric_dtype(df[spec.x]) or pd.api.types.is_datetime64_any_dtype(df[spec.x]):
        converted = df[spec.x]
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            converted = pd.to_datetime(df[spec.x], errors="coerce", format="mixed")
    if converted.notna().mean() < 0.9:
        raise ChartError(f"'{spec.x}' is not a date or ordered numeric column, so a line would be misleading.")
    df[spec.x] = converted
    keys = [spec.x] + ([spec.color] if spec.color else [])
    how = spec.aggregate
    if how == "none" and df.duplicated(keys).any():
        how = "sum"
        notes.append(f"Note: values summed per {spec.x}.")
    data = _aggregate(df, keys, spec.y, how) if how != "none" else df
    if spec.color and data[spec.color].nunique() > MAX_LINE_SERIES:
        top = data.groupby(spec.color)[spec.y].sum().nlargest(MAX_LINE_SERIES).index
        notes.append(f"Showing the {MAX_LINE_SERIES} largest series of {data[spec.color].nunique()}.")
        data = data[data[spec.color].isin(top)]
    data = data.sort_values(spec.x)
    maker = px.area if area else px.line
    fig = maker(data, x=spec.x, y=spec.y, color=spec.color, color_discrete_sequence=CATEGORICAL,
                markers=(not area and len(data) <= 40))
    fig.update_traces(line_width=2)
    fig.update_xaxes(title_text=_label(spec, "x"))
    fig.update_yaxes(title_text=_label(spec, "y"), rangemode="tozero" if area else "normal")
    fig.update_layout(hovermode="x unified")
    return fig


def _line(spec: ChartSpec, df: pd.DataFrame, notes: list[str]) -> go.Figure:
    return _line_like(spec, df, notes, area=False)


def _area(spec: ChartSpec, df: pd.DataFrame, notes: list[str]) -> go.Figure:
    return _line_like(spec, df, notes, area=True)


def _scatter(spec: ChartSpec, df: pd.DataFrame, notes: list[str]) -> go.Figure:
    _require_columns(df, spec.x, spec.y, spec.color)
    if not spec.x or not spec.y:
        raise ChartError("A scatterplot needs two measures (x and y).")
    _require_numeric(df, spec.x, "x measure")
    _require_numeric(df, spec.y, "y measure")
    data = df.assign(**{spec.x: pd.to_numeric(df[spec.x], errors="coerce"),
                        spec.y: pd.to_numeric(df[spec.y], errors="coerce")}).dropna(subset=[spec.x, spec.y])
    if len(data) < 5:
        raise ChartError("Fewer than five complete points; a scatterplot would not be informative.")
    if len(data) > 20_000:
        data = data.sample(20_000, random_state=0)
        notes.append("Random sample of 20,000 points shown.")
    fig = px.scatter(data, x=spec.x, y=spec.y, color=spec.color, color_discrete_sequence=CATEGORICAL, opacity=0.75)
    fig.update_traces(marker=dict(size=8, line=dict(width=1, color="white")))
    fig.update_xaxes(title_text=_label(spec, "x"))
    fig.update_yaxes(title_text=_label(spec, "y"))
    notes.append(f"n = {len(data):,} points. Association does not imply causation.")
    return fig


def _histogram(spec: ChartSpec, df: pd.DataFrame, notes: list[str]) -> go.Figure:
    column = spec.x or spec.value
    _require_columns(df, column, spec.color)
    _require_numeric(df, column, "measure")
    values = pd.to_numeric(df[column], errors="coerce")
    data = df.assign(**{column: values}).dropna(subset=[column])
    fig = px.histogram(data, x=column, color=spec.color, nbins=min(60, max(10, int(np.sqrt(len(data))))),
                       color_discrete_sequence=CATEGORICAL, barmode="overlay" if spec.color else "relative")
    fig.update_xaxes(title_text=(spec.x_label or str(column).replace("_", " ").capitalize())
                     + (f" ({spec.units})" if spec.units else ""))
    fig.update_yaxes(title_text="Number of records")
    notes.append(f"n = {len(data):,}; median = {values.median():,.4g}.")
    return fig


def _box(spec: ChartSpec, df: pd.DataFrame, notes: list[str]) -> go.Figure:
    _require_columns(df, spec.x, spec.y)
    if not spec.y:
        raise ChartError("A boxplot needs a measure (y).")
    _require_numeric(df, spec.y, "measure")
    data = df.assign(**{spec.y: pd.to_numeric(df[spec.y], errors="coerce")}).dropna(subset=[spec.y])
    if spec.x and data[spec.x].nunique() > MAX_BAR_CATEGORIES:
        raise ChartError("Too many groups for a readable boxplot.")
    fig = px.box(data, x=spec.x, y=spec.y, color_discrete_sequence=CATEGORICAL, points="outliers")
    fig.update_xaxes(title_text=_label(spec, "x"))
    fig.update_yaxes(title_text=_label(spec, "y"))
    if spec.x:
        small = data.groupby(spec.x).size()
        if (small < 5).any():
            notes.append("Some groups have fewer than 5 values; their boxes are unreliable.")
    return fig


def _heatmap(spec: ChartSpec, df: pd.DataFrame, notes: list[str]) -> go.Figure:
    _require_columns(df, spec.x, spec.y, spec.value)
    if not (spec.x and spec.y and spec.value):
        raise ChartError("A heat map needs two categories (x, y) and a value.")
    _require_numeric(df, spec.value, "value")
    how = spec.aggregate if spec.aggregate != "none" else "sum"
    pivot = _aggregate(df, [spec.y, spec.x], spec.value, how).pivot(index=spec.y, columns=spec.x, values=spec.value)
    if pivot.shape[0] > 60 or pivot.shape[1] > 60:
        raise ChartError("Too many categories for a readable heat map (max 60 per axis).")
    fig = go.Figure(go.Heatmap(
        z=pivot.values, x=[str(c) for c in pivot.columns], y=[str(i) for i in pivot.index],
        colorscale=SEQUENTIAL_BLUE, colorbar=dict(title=spec.units or spec.value),
        hovertemplate=f"{spec.x}: %{{x}}<br>{spec.y}: %{{y}}<br>{spec.value}: %{{z:,.4g}}<extra></extra>",
        xgap=2, ygap=2,
    ))
    fig.update_xaxes(title_text=_label(spec, "x"))
    fig.update_yaxes(title_text=spec.y_label or str(spec.y).replace("_", " ").capitalize())
    notes.append(f"Cell values: {how} of {spec.value}. Blank cells have no records.")
    return fig


def _correlation(spec: ChartSpec, df: pd.DataFrame, notes: list[str]) -> go.Figure:
    columns = spec.columns or [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    _require_columns(df, *columns)
    if len(columns) < 2:
        raise ChartError("A correlation matrix needs at least two numeric columns.")
    data = pd.DataFrame({c: pd.to_numeric(df[c], errors="coerce") for c in columns}).dropna()
    if len(data) < 10:
        raise ChartError("Fewer than 10 complete rows; correlations would be unreliable.")
    matrix = data.corr(method="spearman")
    fig = go.Figure(go.Heatmap(
        z=matrix.values, x=columns, y=columns, zmin=-1, zmax=1, colorscale=DIVERGING,
        text=np.round(matrix.values, 2), texttemplate="%{text}", colorbar=dict(title="Spearman ρ"),
        hovertemplate="%{y} vs %{x}: ρ = %{z:.2f}<extra></extra>", xgap=2, ygap=2,
    ))
    fig.update_yaxes(autorange="reversed")
    notes.append(f"Spearman rank correlation, n = {len(data):,}. Correlation is not causation.")
    return fig


def _kpi(spec: ChartSpec, df: pd.DataFrame, notes: list[str]) -> go.Figure:
    column = spec.value or spec.y
    _require_columns(df, column)
    if not column:
        raise ChartError("A KPI needs a value column.")
    _require_numeric(df, column, "value")
    values = pd.to_numeric(df[column], errors="coerce")
    if len(df) == 1:
        value = float(values.iloc[0])
    else:
        how = spec.aggregate if spec.aggregate != "none" else "sum"
        value = float(getattr(values, how)())
        notes.append(f"{how.capitalize()} of {len(df):,} rows.")
    prefix = spec.units if spec.units in _CURRENCY_PREFIXES and len(spec.units) == 1 else ""
    suffix = "" if prefix else (f" {spec.units}" if spec.units else "")
    fig = go.Figure(go.Indicator(
        mode="number", value=value,
        number=dict(prefix=prefix, suffix=suffix, valueformat=",.4~g" if abs(value) < 1e6 else ",.3s"),
        title=dict(text=spec.y_label or str(column).replace("_", " ").capitalize()),
    ))
    fig.update_layout(height=260, margin=dict(l=20, r=20, t=70, b=80))
    return fig


def _ranked_table(spec: ChartSpec, df: pd.DataFrame, notes: list[str]) -> go.Figure:
    columns = spec.columns or list(df.columns[:6])
    _require_columns(df, *columns)
    sort_col = spec.value or next(
        (c for c in columns if pd.api.types.is_numeric_dtype(df[c])), columns[-1]
    )
    data = df[columns].sort_values(sort_col, ascending=not spec.sort_desc)
    if spec.top_n:
        data = data.head(spec.top_n)
    data.insert(0, "Rank", range(1, len(data) + 1))
    formatted = [
        data[c].map(lambda v: f"{v:,.2f}" if isinstance(v, (float, np.floating)) else v) for c in data.columns
    ]
    fig = go.Figure(go.Table(
        header=dict(values=[str(c).replace("_", " ").capitalize() for c in data.columns],
                    fill_color="#f0efec", align="left", font=dict(color="#0b0b0b")),
        cells=dict(values=formatted, align="left", fill_color="white", height=26),
    ))
    fig.update_layout(height=min(900, 120 + 28 * len(data)))
    return fig


def _forecast(spec: ChartSpec, df: pd.DataFrame, notes: list[str]) -> go.Figure:
    needed = {"period", "observed", "forecast"}
    if not needed <= set(df.columns):
        raise ChartError("A forecast chart needs the output of run_forecast (period, observed, forecast columns).")
    data = df.sort_values("period")
    data["period"] = pd.to_datetime(data["period"], errors="coerce")
    fig = go.Figure()
    future = data[data["forecast"].notna()]
    if {"lower", "upper"} <= set(data.columns) and future["lower"].notna().any():
        fig.add_trace(go.Scatter(x=future["period"], y=future["upper"], mode="lines", line=dict(width=0),
                                 showlegend=False, hoverinfo="skip"))
        fig.add_trace(go.Scatter(x=future["period"], y=future["lower"], mode="lines", line=dict(width=0),
                                 fill="tonexty", fillcolor="rgba(235,104,52,0.15)", name="Prediction interval"))
    fig.add_trace(go.Scatter(x=data["period"], y=data["observed"], mode="lines", name="Observed",
                             line=dict(color=CATEGORICAL[0], width=2)))
    if "fitted" in data.columns and data["fitted"].notna().any():
        fig.add_trace(go.Scatter(x=data["period"], y=data["fitted"], mode="lines", name="Holdout prediction",
                                 line=dict(color=CATEGORICAL[1], width=1.5, dash="dot")))
    fig.add_trace(go.Scatter(x=future["period"], y=future["forecast"], mode="lines", name="Forecast",
                             line=dict(color=CATEGORICAL[1], width=2, dash="dash")))
    fig.update_xaxes(title_text=spec.x_label or "Period")
    fig.update_yaxes(title_text=(spec.y_label or "Value") + (f" ({spec.units})" if spec.units else ""))
    fig.update_layout(hovermode="x unified")
    notes.append("Dashed line: model forecast, not observed data. Shaded band: prediction interval.")
    return fig


_BUILDERS = {
    "forecast": _forecast,
    "bar": _bar,
    "line": _line,
    "area": _area,
    "scatter": _scatter,
    "histogram": _histogram,
    "box": _box,
    "heatmap": _heatmap,
    "correlation": _correlation,
    "kpi": _kpi,
    "ranked_table": _ranked_table,
}

SUPPORTED_CHARTS = tuple(_BUILDERS)
