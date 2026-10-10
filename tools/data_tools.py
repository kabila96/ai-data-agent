"""CSV, data-quality, join and statistics tools."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from analysis.joins import join_frames, join_report, suggest_join_keys
from analysis.profiling import profile_dataframe
from analysis.transforms import run_transform
from models.schemas import new_id
from tools.context import AgentContext, PendingApproval, frame_summary
from tools.registry import ToolOutput, registry


class CSVArg(BaseModel):
    name: str = Field(description="Uploaded CSV file name (as listed by list_available_sources).")


class ProfileArgs(BaseModel):
    source: str = Field(description="A result_id (res_...) or csv:<file name>.")
    key_columns: list[str] = Field(default_factory=list, description="Columns expected to be unique keys.")


class JoinArgs(BaseModel):
    left_source: str = Field(description="result_id or csv:<name> for the left side.")
    right_source: str = Field(description="result_id or csv:<name> for the right side.")
    left_key: str | None = Field(default=None, description="Join column on the left. Omit to get suggestions.")
    right_key: str | None = Field(default=None, description="Join column on the right. Omit to get suggestions.")
    how: Literal["inner", "left", "right", "outer"] = "left"


class StatsArgs(BaseModel):
    source: str = Field(description="result_id or csv:<name>.")
    method: Literal["descriptive", "group_summary", "compare_groups", "correlation", "time_trend", "contribution",
                    "regression"]
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "descriptive: {columns?}; group_summary: {group, value, agg?}; compare_groups: {group, value}; "
            "correlation: {columns?, method?: spearman|pearson}; time_trend: {date_col, value, "
            "freq?: D|W|MS|QS|YS, agg?: sum|mean|count, rolling?: int, group?}; "
            "contribution: {group, value, period_col, current, previous}; regression: {target, predictors: [..]}"
        ),
    )


@registry.register(
    "inspect_csv",
    "Show an uploaded CSV's columns, detected types, row count, a small sample and loading warnings.",
    CSVArg,
)
def inspect_csv(ctx: AgentContext, args: CSVArg) -> ToolOutput:
    data, label, _, _ = ctx.resolve_frame(f"csv:{args.name}")
    src = ctx.csv_sources[args.name]
    return ToolOutput(True, {"name": args.name, "detected_types": src.types,
                             **frame_summary(data, ctx.settings.llm_max_sample_rows)},
                      warnings=src.warnings, method="CSV inspection")


@registry.register(
    "profile_data_quality",
    "Profile a result or CSV for missing values, duplicates, invalid or future dates, negative or extreme "
    "values, inconsistent labels, mixed currencies and low sample size. Reports issues; never changes data.",
    ProfileArgs,
)
def profile_data_quality(ctx: AgentContext, args: ProfileArgs) -> ToolOutput:
    data, label, _, _ = ctx.resolve_frame(args.source)
    invalid_dates = None
    if args.source.removeprefix("csv:") in ctx.csv_sources:
        src = ctx.csv_sources[args.source.removeprefix("csv:")]
        invalid_dates = {}
        for col, t in src.types.items():
            if t == "datetime":
                raw = src.raw[col]
                invalid_dates[col] = int((raw.notna() & (raw.str.len() > 0) & src.data[col].isna()).sum())
    profile = profile_dataframe(data, args.key_columns, invalid_dates)
    return ToolOutput(True, {"source": label, **profile.to_llm_dict()}, warnings=profile.warnings[:15],
                      method="data-quality profile")


@registry.register(
    "join_data_sources",
    "Join two sources on key columns. Without keys it returns candidate keys. A join only runs after the USER "
    "approves the exact key pair in the interface; otherwise it returns a join-risk report and an approval request. "
    "Never assume a key.",
    JoinArgs,
)
def join_data_sources(ctx: AgentContext, args: JoinArgs) -> ToolOutput:
    left, left_label, _, _ = ctx.resolve_frame(args.left_source)
    right, right_label, _, _ = ctx.resolve_frame(args.right_source)
    if not args.left_key or not args.right_key:
        candidates = suggest_join_keys(left, right)
        return ToolOutput(True, {
            "status": "keys_required",
            "candidates": [c.__dict__ for c in candidates],
            "instruction": "Present these candidates to the user and ask which key pair to use. Do not choose one yourself.",
        }, method="join key suggestion")

    report = join_report(left, right, args.left_key, args.right_key, args.how, left_label, right_label)
    approval_key = (args.left_source, args.right_source, args.left_key, args.right_key)
    if approval_key not in ctx.approved_joins:
        approval = PendingApproval(
            id=new_id("apr"), kind="join",
            description=(f"Join {left_label}.{args.left_key} → {right_label}.{args.right_key} ({args.how}); "
                         f"{report.result_rows:,} result rows, {report.relationship}"),
            payload={"left_source": args.left_source, "right_source": args.right_source,
                     "left_key": args.left_key, "right_key": args.right_key, "how": args.how,
                     "report": report.as_dict()},
        )
        ctx.pending_approvals[approval.id] = approval
        return ToolOutput(True, {
            "status": "awaiting_user_approval", "join_report": report.as_dict(),
            "instruction": "Summarise the join report for the user and ask them to approve it with the Approve "
                           "button. Stop here; do not run further analysis on the joined data yet.",
        }, warnings=report.warnings, requires_approval=True, method="join risk report")

    merged, report = join_frames(left, right, args.left_key, args.right_key, args.how, left_label, right_label)
    record = ctx.add_result(
        merged, f"{left_label} ⋈ {right_label}",
        ctx.resolve_frame(args.left_source)[2], f"{left_label} + {right_label}",
        warnings=report.warnings, refreshable=False,
    )
    return ToolOutput(True, {"status": "joined", "join_report": report.as_dict(),
                             **frame_summary(merged, ctx.settings.llm_max_sample_rows)},
                      result_id=record.id, warnings=report.warnings, method=f"{args.how} join")


@registry.register(
    "run_statistical_analysis",
    "Run a statistical method on a result or CSV and store the output table as a new result. Reports the number "
    "of observations, assumptions and warnings. Tests are skipped when samples are too small.",
    StatsArgs,
)
def run_statistical_analysis(ctx: AgentContext, args: StatsArgs) -> ToolOutput:
    data, label, recipe, refreshable = ctx.resolve_frame(args.source)
    analysis = run_transform(data, args.method, args.params)
    derived_recipe = recipe.model_copy(
        update={"transforms": [*recipe.transforms, {"method": args.method, "params": args.params}]}
    )
    record = ctx.add_result(analysis.table, f"{analysis.method} ({label})", derived_recipe, label,
                            warnings=analysis.warnings, refreshable=refreshable)
    summary = {k: v for k, v in analysis.summary.items() if k != "matrix"}
    return ToolOutput(True, {
        "method": analysis.method, "observations": analysis.observations, "summary": summary,
        "assumptions": analysis.assumptions,
        **frame_summary(analysis.table, ctx.settings.llm_max_sample_rows),
    }, result_id=record.id, warnings=analysis.warnings, method=analysis.method)


class ForecastArgs(BaseModel):
    source: str = Field(description="result_id or csv:<name> with a date column and a numeric column.")
    date_col: str
    value: str
    horizon: int = Field(default=30, ge=1, le=365, description="Number of future periods.")
    freq: Literal["D", "W", "MS", "QS", "YS"] = Field(default="D", description="Day, week, month, quarter, year.")
    agg: Literal["sum", "mean", "count"] = "sum"


class AnomalyArgs(BaseModel):
    source: str = Field(description="result_id or csv:<name>.")
    columns: list[str] = Field(default_factory=list, description="Numeric columns to check (one for robust_z/iqr).")
    method: Literal["auto", "robust_z", "iqr", "isolation_forest", "time_series"] = "auto"
    date_col: str | None = Field(default=None, description="Needed for time_series.")
    threshold: float | None = Field(default=None, gt=0)
    id_columns: list[str] = Field(default_factory=list, description="Columns that identify a record, shown first.")


def _run_and_record(ctx: AgentContext, source: str, method: str, params: dict[str, Any]) -> ToolOutput:
    data, label, recipe, refreshable = ctx.resolve_frame(source)
    analysis = run_transform(data, method, params)
    derived = recipe.model_copy(update={"transforms": [*recipe.transforms, {"method": method, "params": params}]})
    record = ctx.add_result(analysis.table, f"{analysis.method} ({label})", derived, label,
                            warnings=analysis.warnings, refreshable=refreshable)
    summary = {k: v for k, v in analysis.summary.items() if k != "matrix"}
    return ToolOutput(True, {"method": analysis.method, "observations": analysis.observations, "summary": summary,
                             "assumptions": analysis.assumptions,
                             **frame_summary(analysis.table, ctx.settings.llm_max_sample_rows)},
                      result_id=record.id, warnings=analysis.warnings, method=analysis.method)


@registry.register(
    "run_forecast",
    "Forecast a time series. Aggregates to the chosen period, scores several models (including naive and "
    "seasonal-naive baselines) on a holdout the models never saw, picks the best, and returns forecasts with an "
    "approximate 80% prediction interval and error metrics. Chart the result with chart_type 'forecast'. "
    "Always report the holdout error and whether the model beat the baseline.",
    ForecastArgs,
)
def run_forecast(ctx: AgentContext, args: ForecastArgs) -> ToolOutput:
    params = {"date_col": args.date_col, "value": args.value, "horizon": args.horizon, "freq": args.freq,
              "agg": args.agg}
    return _run_and_record(ctx, args.source, "forecast", params)


@registry.register(
    "detect_anomalies",
    "Flag unusual records (never removes them): robust z-score or IQR for one column, IsolationForest for "
    "combinations of columns, or time_series for spikes relative to the local typical value. Returns only the "
    "flagged rows with a score and a plain-language reason.",
    AnomalyArgs,
)
def detect_anomalies(ctx: AgentContext, args: AnomalyArgs) -> ToolOutput:
    params = {"columns": args.columns, "method": args.method, "date_col": args.date_col,
              "threshold": args.threshold, "id_columns": args.id_columns}
    return _run_and_record(ctx, args.source, "anomalies", params)
