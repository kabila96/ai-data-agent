"""Visualisation, dashboard and export tools, plus Phase 3 placeholders."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from dashboards.service import add_component_from_chart
from models.schemas import ChartSpec
from tools.context import AgentContext
from tools.registry import ToolOutput, registry
from visualisations.chart_selector import recommend_chart
from visualisations.charts import ChartError, build_figure


class VisualisationArgs(BaseModel):
    source: str = Field(description="result_id (res_...) or csv:<name> holding the data to plot.")
    title: str = Field(description="Clear title stating what is shown.")
    chart_type: Literal["auto", "bar", "line", "area", "scatter", "histogram", "box", "heatmap",
                        "correlation", "kpi", "ranked_table", "forecast"] = "auto"
    question: str = Field(default="", description="The user's question; used when chart_type is auto.")
    x: str | None = None
    y: str | None = None
    color: str | None = None
    value: str | None = None
    columns: list[str] = Field(default_factory=list)
    aggregate: Literal["none", "sum", "mean", "count", "median", "min", "max"] = "none"
    x_label: str | None = None
    y_label: str | None = None
    units: str | None = Field(default=None, description="e.g. '£', 'facilities', 'km²'.")
    filters_text: str = Field(default="None", description="Plain-language filters applied.")
    date_range: str | None = None
    interpretation: str = Field(default="", description="One or two sentences grounded in the data.")
    top_n: int | None = Field(default=None, ge=1, le=500)


class SaveDashboardArgs(BaseModel):
    dashboard_name: str = Field(min_length=1, max_length=120)
    chart_id: str
    component_title: str | None = None
    width: Literal["full", "half"] = "half"


class ExportArgs(BaseModel):
    result_id: str
    reason: str = ""


@registry.register(
    "generate_visualisation",
    "Create a chart from a result or CSV. Use chart_type='auto' to let the system pick a chart suited to the "
    "question and column types, or say why a table is better. Always give a title, units and an interpretation "
    "that only states numbers present in the data.",
    VisualisationArgs,
)
def generate_visualisation(ctx: AgentContext, args: VisualisationArgs) -> ToolOutput:
    data, label, _, _ = ctx.resolve_frame(args.source)
    fields = args.model_dump()
    reason = ""
    if args.chart_type == "auto":
        rec = recommend_chart(data, args.question or args.title)
        if rec.chart_type is None:
            return ToolOutput(True, {"chart_created": False, "reason": rec.reason})
        reason = rec.reason
        fields.update({"chart_type": rec.chart_type})
        for key in ("x", "y", "color", "value"):
            if not fields.get(key) and getattr(rec, key):
                fields[key] = getattr(rec, key)
        if not fields.get("columns") and rec.columns:
            fields["columns"] = rec.columns
    record = ctx.results.get(args.source)
    spec = ChartSpec(
        chart_type=fields["chart_type"], title=args.title, x=fields.get("x"), y=fields.get("y"),
        color=fields.get("color"), value=fields.get("value"), columns=fields.get("columns") or [],
        aggregate=args.aggregate, x_label=args.x_label, y_label=args.y_label, units=args.units,
        source=record.source_label if record else label, filters_text=args.filters_text,
        date_range=args.date_range, interpretation=args.interpretation,
        warnings=(record.warnings[:5] if record else []), top_n=args.top_n,
    )
    try:
        figure = build_figure(spec, data)
    except ChartError as exc:
        return ToolOutput(False, {"chart_created": False, "error": str(exc)})
    result_id = args.source if args.source in ctx.results else ctx.add_result(
        data, label, ctx.resolve_frame(args.source)[2], label).id
    chart = ctx.add_chart(spec, figure, result_id)
    return ToolOutput(True, {"chart_created": True, "chart_type": spec.chart_type, "x": spec.x, "y": spec.y,
                             "color": spec.color, "value": spec.value, "selection_reason": reason},
                      chart_id=chart.id, method=f"{spec.chart_type} chart")


@registry.register(
    "save_dashboard",
    "Save a chart (with its data recipe) as a component of a named dashboard, creating the dashboard if needed. "
    "Only do this when the user asks to save or build a dashboard.",
    SaveDashboardArgs,
)
def save_dashboard(ctx: AgentContext, args: SaveDashboardArgs) -> ToolOutput:
    chart = ctx.charts.get(args.chart_id)
    if chart is None:
        return ToolOutput(False, {"error": f"Unknown chart_id '{args.chart_id}'."})
    spec, component = add_component_from_chart(ctx, chart, args.dashboard_name, args.component_title, args.width)
    return ToolOutput(True, {"dashboard": spec.name, "dashboard_id": spec.id, "component_id": component.id,
                             "components": len(spec.components)}, method="dashboard save")


@registry.register(
    "export_reviewed_results",
    "Offer a result table for download in the interface after the user has reviewed it. The user downloads it; "
    "nothing is written elsewhere.",
    ExportArgs,
)
def export_reviewed_results(ctx: AgentContext, args: ExportArgs) -> ToolOutput:
    if args.result_id not in ctx.results:
        return ToolOutput(False, {"error": f"Unknown result_id '{args.result_id}'."})
    if args.result_id not in ctx.export_offers:
        ctx.export_offers.append(args.result_id)
    record = ctx.results[args.result_id]
    return ToolOutput(True, {"offered": True, "rows": len(record.data), "label": record.label},
                      result_id=args.result_id, method="export offer")
