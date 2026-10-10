"""Geospatial tools: layer checks / join diagnostics and map generation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from geospatial.maps import MapError, build_map, find_geojson_column
from geospatial.spatial_qa import diagnose_join, interpret_layer, join_test_sql, layer_stats_sql
from models.schemas import ChartSpec, TableInfo
from security.errors import DataSourceError
from tools.context import AgentContext
from tools.registry import ToolOutput, registry


class LayerRef(BaseModel):
    table: str
    schema_name: str | None = None
    geometry_column: str | None = Field(default=None, description="Omit to use the table's only geometry column.")


class SpatialCheckArgs(BaseModel):
    layer: LayerRef
    other_layer: LayerRef | None = Field(
        default=None, description="Second layer: adds spatial-join diagnostics (CRS, extents, validity, test match).")
    test_join: bool = Field(default=True, description="With two layers, count CRS-aligned ST_Intersects matches.")


class MapArgs(BaseModel):
    source: str = Field(description="result_id (res_...) or csv:<name>.")
    map_type: Literal["choropleth", "point_map", "proportional_map"]
    title: str
    value: str | None = Field(default=None, description="Numeric column for colour (choropleth) or size (proportional).")
    geometry: str | None = Field(default=None, description="GeoJSON column in EPSG:4326; auto-detected if omitted.")
    lat: str | None = Field(default=None, description="Latitude column (point maps from CSV).")
    lon: str | None = Field(default=None, description="Longitude column.")
    label: str | None = Field(default=None, description="Column with feature names for hover.")
    color: str | None = Field(default=None, description="Category column for point colours.")
    frame: str | None = Field(default=None, description="Time column to animate (time-series map).")
    classification: Literal["continuous", "quantile"] = "continuous"
    units: str | None = None
    filters_text: str = "None"
    date_range: str | None = None
    interpretation: str = ""
    swap_lat_lon: bool = Field(default=False, description="Only after the user confirmed coordinates are swapped.")


def _resolve_layer(ctx: AgentContext, ref: LayerRef) -> tuple[TableInfo, str, str]:
    metadata, _ = ctx.require_database()
    info = metadata.describe_table(ref.schema_name, ref.table)
    if info is None:
        raise DataSourceError(f"Table '{ref.table}' is not approved or does not exist.")
    geoms = info.geometry_columns
    if not geoms:
        raise DataSourceError(f"{info.qualified} has no geometry or geography column.")
    if ref.geometry_column:
        match = [g for g in geoms if g.name == ref.geometry_column]
        if not match:
            raise DataSourceError(f"'{ref.geometry_column}' is not a geometry column of {info.qualified}. "
                                  f"Options: {', '.join(g.name for g in geoms)}")
        col = match[0]
    elif len(geoms) == 1:
        col = geoms[0]
    else:
        raise DataSourceError(f"{info.qualified} has several geometry columns ({', '.join(g.name for g in geoms)}); "
                              "say which one to use.")
    col_type = "geography" if "geography" in col.data_type else "geometry"
    return info, col.name, col_type


def _layer_stats(ctx: AgentContext, ref: LayerRef):  # type: ignore[no-untyped-def]
    info, col, col_type = _resolve_layer(ctx, ref)
    _, executor = ctx.require_database()
    result = executor.run(layer_stats_sql(info.schema_name, info.table_name, col), source="spatial_check")
    row = result.data.iloc[0].to_dict() if len(result.data) else {}
    stats = interpret_layer(row, info.qualified, col, col_type, ctx.settings.metric_crs_override_epsg,
                            ctx.settings.british_crs_epsg)
    return info, col, stats


@registry.register(
    "check_spatial_data",
    "Inspect a PostGIS layer before spatial analysis: SRID, geometry types, null/empty/invalid geometries, extent "
    "(native and WGS84), lon/lat swap and wrong-SRID detection, and the metric CRS to use for distance and area "
    "(EPSG:27700 for Great Britain). With a second layer it diagnoses spatial joins (CRS mismatch, non-overlapping "
    "extents, invalid geometries, predicate choice) and counts CRS-aligned matches. Use it whenever a spatial join "
    "returns zero or surprisingly few rows.",
    SpatialCheckArgs,
)
def check_spatial_data(ctx: AgentContext, args: SpatialCheckArgs) -> ToolOutput:
    if ctx.metadata is not None and not ctx.metadata.has_postgis():
        return ToolOutput(False, {"error": "PostGIS is not installed in this database."})
    info, col, left = _layer_stats(ctx, args.layer)
    payload: dict = {"layer": left.as_dict()}
    warnings = list(left.warnings)
    if args.other_layer:
        info2, col2, right = _layer_stats(ctx, args.other_layer)
        payload["other_layer"] = right.as_dict()
        warnings += right.warnings
        matches = None
        if args.test_join and left.srid and right.srid:
            _, executor = ctx.require_database()
            try:
                sql = join_test_sql((info.schema_name, info.table_name, col),
                                    (info2.schema_name, info2.table_name, col2))
                res = executor.run(sql, source="spatial_check")
                matches = int(res.data["matched_pairs"].iloc[0])
                payload["join_test"] = {"predicate": "ST_Intersects after ST_Transform to left SRID",
                                        "matched_pairs": matches,
                                        "matched_right_features": int(res.data["matched_right"].iloc[0]),
                                        "right_sample_limit": 5000}
            except Exception as exc:  # timeouts on huge layers should not hide the other diagnostics
                payload["join_test"] = {"error": getattr(exc, "user_message", str(exc))[:300]}
        payload["join_diagnosis"] = diagnose_join(left, right, matches)
    return ToolOutput(True, payload, warnings=warnings, method="spatial data check")


@registry.register(
    "generate_geospatial_map",
    "Draw an interactive map from a result. For PostGIS data, first query geometry as GeoJSON in WGS84, e.g. "
    "ST_AsGeoJSON(ST_Transform(ST_SimplifyPreserveTopology(geom, 10), 4326), 6) AS geojson (tolerance in the "
    "layer's units: ~10 for EPSG:27700 metres, ~0.0001 for degrees), together with the value and a name column. "
    "choropleth = areas coloured by a numeric value (use rates or densities rather than raw counts when area sizes "
    "differ); point_map = locations; proportional_map = symbols sized by a value. CSV points can use lat/lon columns.",
    MapArgs,
)
def generate_geospatial_map(ctx: AgentContext, args: MapArgs) -> ToolOutput:
    data, label, recipe, _ = ctx.resolve_frame(args.source)
    record = ctx.results.get(args.source)
    spec = ChartSpec(
        chart_type=args.map_type, title=args.title, value=args.value, geometry=args.geometry, lat=args.lat,
        lon=args.lon, label=args.label, color=args.color, frame=args.frame, classification=args.classification,
        units=args.units, source=record.source_label if record else label, filters_text=args.filters_text,
        date_range=args.date_range, interpretation=args.interpretation,
        warnings=(record.warnings[:5] if record else []), swap_lat_lon=args.swap_lat_lon,
    )
    try:
        figure = build_map(spec, data)
    except MapError as exc:
        return ToolOutput(False, {"map_created": False, "error": str(exc)})
    result_id = args.source if args.source in ctx.results else ctx.add_result(data, label, recipe, label).id
    chart = ctx.add_chart(spec, figure, result_id)
    geo_col = args.geometry or find_geojson_column(data)
    if geo_col and result_id not in ctx.export_offers:
        ctx.export_offers.append(result_id)  # GeoJSON download offered alongside the map
    return ToolOutput(True, {"map_created": True, "map_type": args.map_type, "features": len(data),
                             "geometry_column": geo_col}, chart_id=chart.id, result_id=result_id,
                      method=f"{args.map_type}")
