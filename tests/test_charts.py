"""Chart generation and chart selection."""

from __future__ import annotations

import pandas as pd
import pytest

from models.schemas import ChartSpec
from tests.conftest import make_sales_frame
from visualisations.chart_selector import recommend_chart
from visualisations.charts import ChartError, build_figure

SALES = make_sales_frame()
BY_BRANCH = pd.DataFrame({"branch": ["Camden", "Digbeth", "Edgbaston"], "revenue": [300.0, 250.5, 90.0],
                          "region": ["London", "West Midlands", "West Midlands"]})

CASES = [
    ChartSpec(chart_type="bar", title="Revenue by branch", x="branch", y="revenue", units="£"),
    ChartSpec(chart_type="line", title="Daily revenue", x="sale_date", y="revenue", color="branch", units="£"),
    ChartSpec(chart_type="area", title="Daily revenue (area)", x="sale_date", y="revenue", units="£"),
    ChartSpec(chart_type="scatter", title="Units vs revenue", x="units", y="revenue"),
    ChartSpec(chart_type="histogram", title="Revenue distribution", x="revenue", units="£"),
    ChartSpec(chart_type="box", title="Revenue by branch", x="branch", y="revenue"),
    ChartSpec(chart_type="heatmap", title="Revenue by branch and region", x="branch", y="region", value="revenue"),
    ChartSpec(chart_type="correlation", title="Correlation", columns=["revenue", "units"]),
    ChartSpec(chart_type="kpi", title="Total revenue", value="revenue", units="£"),
    ChartSpec(chart_type="ranked_table", title="Branches ranked", columns=["branch", "revenue"]),
]


@pytest.mark.parametrize("spec", CASES, ids=[c.chart_type for c in CASES])
def test_chart_types_render_with_required_labels(spec):
    data = BY_BRANCH if spec.chart_type in ("bar", "heatmap", "ranked_table") else SALES
    spec = spec.model_copy(update={"source": "powell_analytics: training.demo_sales", "filters_text": "branch = all",
                                   "date_range": "2025-01-01 to 2025-04-30"})
    fig = build_figure(spec, data)
    assert fig.layout.title.text == spec.title
    footer = fig.layout.annotations[-1].text
    assert "Source: powell_analytics" in footer and "Filters: branch = all" in footer and "Date range" in footer
    if spec.chart_type in ("bar", "line", "area", "scatter", "box"):
        assert fig.layout.xaxis.title.text and fig.layout.yaxis.title.text
    if spec.units and spec.chart_type in ("bar", "line"):
        axis_titles = (fig.layout.xaxis.title.text or "") + (fig.layout.yaxis.title.text or "")
        assert "£" in axis_titles
    fig.to_json()  # serialisable


def test_line_rejects_non_temporal_x():
    with pytest.raises(ChartError, match="not a date"):
        build_figure(ChartSpec(chart_type="line", title="t", x="branch", y="revenue"), BY_BRANCH)


def test_bar_rejects_too_many_categories():
    df = pd.DataFrame({"c": [f"c{i}" for i in range(100)], "v": range(100)})
    with pytest.raises(ChartError, match="too many"):
        build_figure(ChartSpec(chart_type="bar", title="t", x="c", y="v"), df)
    fig = build_figure(ChartSpec(chart_type="bar", title="t", x="c", y="v", top_n=10), df)
    assert len(fig.data[0].x) == 10 or len(fig.data[0].y) == 10


def test_bar_requires_numeric_measure():
    with pytest.raises(ChartError, match="numeric"):
        build_figure(ChartSpec(chart_type="bar", title="t", x="branch", y="region"), BY_BRANCH)


def test_missing_column_error_lists_available():
    with pytest.raises(ChartError, match="Available"):
        build_figure(ChartSpec(chart_type="bar", title="t", x="nope", y="revenue"), BY_BRANCH)


def test_selector_choices():
    assert recommend_chart(SALES[["sale_date", "revenue"]], "trend").chart_type == "line"
    assert recommend_chart(BY_BRANCH[["branch", "revenue"]], "revenue by branch").chart_type == "bar"
    assert recommend_chart(pd.DataFrame({"total": [42.0]}), "total sales").chart_type == "kpi"
    ids_only = pd.DataFrame({"name": [f"n{i}" for i in range(200)], "comment": [f"text {i}" for i in range(200)]})
    assert recommend_chart(ids_only, "list").chart_type is None
    assert recommend_chart(SALES[["revenue", "units"]], "relationship between revenue and units").chart_type == "scatter"


def test_duplicate_categories_are_summed_with_note():
    df = pd.DataFrame({"b": ["x", "x", "y"], "v": [1, 2, 3]})
    fig = build_figure(ChartSpec(chart_type="bar", title="t", x="b", y="v"), df)
    assert "summed" in fig.layout.annotations[-1].text
