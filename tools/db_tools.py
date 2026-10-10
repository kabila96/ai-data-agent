"""Database tools: sources, schemas, tables, metadata, previews and SQL."""

from __future__ import annotations

from pydantic import BaseModel, Field

from analysis.profiling import profile_dataframe
from models.schemas import SourceRecipe
from security.sql_validator import quote_ident
from tools.context import AgentContext, frame_summary
from tools.registry import ToolOutput, registry


class NoArgs(BaseModel):
    pass


class SchemaArg(BaseModel):
    schema_name: str | None = Field(default=None, description="Schema to list; omit for all approved schemas.")


class TableArg(BaseModel):
    table: str = Field(description="Table or view name.")
    schema_name: str | None = Field(default=None, description="Schema name; optional if the table name is unique.")


class PreviewArgs(TableArg):
    limit: int = Field(default=10, ge=1, le=50)


class SQLArgs(BaseModel):
    sql: str = Field(description="A single PostgreSQL SELECT statement. No comments, no semicolons inside.")


class ExecuteArgs(SQLArgs):
    purpose: str = Field(default="", description="One line: what this query answers.")
    label: str = Field(default="", description="Short human-readable name for the result table.")


@registry.register(
    "list_available_sources",
    "List the data sources the user has selected: database status, approved schemas and uploaded CSV files.",
    NoArgs,
)
def list_available_sources(ctx: AgentContext, args: NoArgs) -> ToolOutput:
    db = {"connected": ctx.database_available, "selected": "database" in ctx.selected_sources}
    if ctx.metadata is not None:
        db.update({
            "database": ctx.settings.pg_database,
            "approved_schemas_present": ctx.metadata.list_schemas(),
            "approved_schemas_missing": ctx.metadata.missing_schemas(),
            "postgis": ctx.metadata.has_postgis(),
        })
    csvs = [
        {"name": n, "rows": len(s.data), "columns": len(s.data.columns),
         "selected": f"csv:{n}" in ctx.selected_sources or "all_csv" in ctx.selected_sources}
        for n, s in ctx.csv_sources.items()
    ]
    recent = [{"result_id": r.id, "label": r.label, "rows": len(r.data)} for r in list(ctx.results.values())[-10:]]
    return ToolOutput(True, {"database": db, "csv_files": csvs, "recent_results": recent})


@registry.register("list_database_schemas", "List approved schemas that exist and are readable.", NoArgs)
def list_database_schemas(ctx: AgentContext, args: NoArgs) -> ToolOutput:
    metadata, _ = ctx.require_database()
    return ToolOutput(True, {"schemas": metadata.list_schemas(), "configured_but_missing": metadata.missing_schemas()})


@registry.register(
    "list_database_tables",
    "List approved tables and views with type, estimated row count and description.",
    SchemaArg,
)
def list_database_tables(ctx: AgentContext, args: SchemaArg) -> ToolOutput:
    metadata, _ = ctx.require_database()
    tables = metadata.list_tables(args.schema_name)
    dictionary = {(d["schema_name"], d["table_name"]): d["description"]
                  for d in ctx.store.get_dictionary() if not d["column_name"]}
    return ToolOutput(True, {"tables": [
        {"table": t.qualified, "type": t.table_type, "estimated_rows": t.estimated_rows,
         "description": dictionary.get((t.schema_name, t.table_name)) or t.description}
        for t in tables[:400]
    ], "count": len(tables)})


@registry.register(
    "describe_table",
    "Describe a table: columns, data types, primary/foreign keys, geometry type and SRID, "
    "and data-dictionary descriptions and units.",
    TableArg,
)
def describe_table(ctx: AgentContext, args: TableArg) -> ToolOutput:
    metadata, _ = ctx.require_database()
    info = metadata.describe_table(args.schema_name, args.table)
    if info is None:
        return ToolOutput(False, {"error": f"Table '{args.table}' is not an approved table or does not exist."})
    notes = {(d["column_name"]): d for d in ctx.store.get_dictionary(info.schema_name, info.table_name)}
    columns = []
    for c in info.columns:
        entry = {"name": c.name, "type": c.data_type, "nullable": c.nullable}
        if c.is_primary_key:
            entry["primary_key"] = True
        if c.geometry_type:
            entry.update({"geometry_type": c.geometry_type, "srid": c.srid})
        note = notes.get(c.name)
        if note:
            entry["description"] = note["description"]
            if note.get("units"):
                entry["units"] = note["units"]
        elif c.description:
            entry["description"] = c.description
        columns.append(entry)
    warnings = []
    for g in info.geometry_columns:
        if g.srid in (None, 0):
            warnings.append(f"Geometry column '{g.name}' has no SRID recorded; CRS is unknown.")
    table_note = notes.get("")
    return ToolOutput(True, {
        "table": info.qualified, "type": info.table_type, "estimated_rows": info.estimated_rows,
        "description": (table_note or {}).get("description") or info.description,
        "primary_key": info.primary_key,
        "foreign_keys": [fk.model_dump() for fk in info.foreign_keys],
        "columns": columns,
    }, warnings=warnings)


@registry.register(
    "preview_table",
    "Preview a few rows of an approved table (max 50). Use for understanding values, not for answers.",
    PreviewArgs,
)
def preview_table(ctx: AgentContext, args: PreviewArgs) -> ToolOutput:
    metadata, executor = ctx.require_database()
    resolved = metadata.resolve_table(args.schema_name, args.table)
    if resolved is None:
        return ToolOutput(False, {"error": f"Table '{args.table}' is not approved or does not exist."})
    schema, table = resolved
    sql = f"SELECT * FROM {quote_ident(schema)}.{quote_ident(table)} LIMIT {int(args.limit)}"
    result = executor.run(sql, source="preview")
    record = ctx.add_result(result.data, f"Preview of {schema}.{table}",
                            SourceRecipe(kind="sql", sql=sql, description=f"Preview {schema}.{table}"),
                            f"{ctx.settings.pg_database}.{schema}.{table}", sql=result.sql,
                            warnings=result.warnings)
    return ToolOutput(True, {"table": f"{schema}.{table}",
                             **frame_summary(result.data, min(args.limit, ctx.settings.llm_max_sample_rows or 0))},
                      result_id=record.id, sql=result.sql, warnings=result.warnings, method="table preview")


@registry.register(
    "validate_sql",
    "Check a candidate SELECT without running it. Returns errors or the final safe SQL that would run.",
    SQLArgs,
)
def validate_sql(ctx: AgentContext, args: SQLArgs) -> ToolOutput:
    _, executor = ctx.require_database()
    result = executor.validate(args.sql)
    return ToolOutput(result.ok, {"valid": result.ok, "errors": result.errors, "final_sql": result.safe_sql,
                                  "tables": result.tables}, warnings=result.warnings, sql=result.safe_sql)


@registry.register(
    "execute_read_only_sql",
    "Validate and run a single read-only SELECT on the approved database. Aggregate in SQL where possible. "
    "Returns a result_id for charts and analysis, the row count, column types, a small sample and data-quality warnings.",
    ExecuteArgs,
)
def execute_read_only_sql(ctx: AgentContext, args: ExecuteArgs) -> ToolOutput:
    _, executor = ctx.require_database()
    result = executor.run(args.sql, source="agent")
    profile = profile_dataframe(result.data) if result.row_count else None
    warnings = list(result.warnings) + ((profile.warnings[:10]) if profile else [])
    tables = ", ".join(result.tables) or "no table"
    record = ctx.add_result(
        result.data, args.label or args.purpose[:60] or "Query result",
        SourceRecipe(kind="sql", sql=args.sql, description=args.purpose),
        f"{ctx.settings.pg_database}: {tables}", sql=result.sql, warnings=warnings,
    )
    return ToolOutput(True, {
        "purpose": args.purpose, "executed_sql": result.sql, "truncated": result.truncated,
        "elapsed_ms": result.elapsed_ms, "tables": result.tables,
        **frame_summary(result.data, ctx.settings.llm_max_sample_rows),
    }, result_id=record.id, sql=result.sql, warnings=warnings, method="SQL query")
