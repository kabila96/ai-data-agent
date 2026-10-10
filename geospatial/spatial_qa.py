"""Spatial data-quality checks and zero-match spatial-join diagnostics.

All SQL here is built by the application from catalog-verified names (quoted
identifiers) and still passes through the SQL validator and read-only executor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from geospatial.crs import BBox, coordinate_warnings, is_geographic, recommend_metric_crs
from security.sql_validator import quote_ident


@dataclass
class LayerStats:
    table: str
    column: str
    column_type: str  # geometry | geography
    n_rows: int = 0
    n_geom: int = 0
    n_empty: int = 0
    n_invalid: int = 0
    n_srids: int = 0
    srid: int | None = None
    geom_types: list[str] = field(default_factory=list)
    native_extent: tuple[float, float, float, float] | None = None
    bbox: BBox | None = None  # in EPSG:4326
    warnings: list[str] = field(default_factory=list)
    metric_epsg: int | None = None
    metric_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table, "geometry_column": self.column, "column_type": self.column_type,
            "rows": self.n_rows, "non_null_geometries": self.n_geom, "null_geometries": self.n_rows - self.n_geom,
            "empty": self.n_empty, "invalid": self.n_invalid, "srid": self.srid, "distinct_srids": self.n_srids,
            "geometry_types": self.geom_types,
            "extent_native": self.native_extent,
            "extent_wgs84": None if self.bbox is None else
            [round(v, 6) for v in (self.bbox.lon_min, self.bbox.lat_min, self.bbox.lon_max, self.bbox.lat_max)],
            "recommended_metric_crs": self.metric_epsg, "metric_crs_reason": self.metric_reason,
            "warnings": self.warnings,
        }


def layer_stats_sql(schema: str, table: str, column: str) -> str:
    src = f"{quote_ident(schema)}.{quote_ident(table)}"
    col = quote_ident(column)
    return f"""
WITH q AS (SELECT {col}::geometry AS g FROM {src}),
s AS (
  SELECT count(*) AS n_rows, count(g) AS n_geom,
         sum(CASE WHEN ST_IsEmpty(g) THEN 1 ELSE 0 END) AS n_empty,
         sum(CASE WHEN NOT ST_IsValid(g) THEN 1 ELSE 0 END) AS n_invalid,
         count(DISTINCT ST_SRID(g)) AS n_srids, max(ST_SRID(g)) AS srid,
         string_agg(DISTINCT GeometryType(g), ',') AS geom_types,
         ST_Extent(g) AS ext
  FROM q
)
SELECT n_rows, n_geom, n_empty, n_invalid, n_srids, srid, geom_types,
       ST_XMin(ext) AS xmin, ST_YMin(ext) AS ymin, ST_XMax(ext) AS xmax, ST_YMax(ext) AS ymax,
       CASE WHEN srid > 0 AND ext IS NOT NULL
            THEN ST_XMin(ST_Transform(ST_SetSRID(ext::geometry, srid), 4326)) END AS lon_min,
       CASE WHEN srid > 0 AND ext IS NOT NULL
            THEN ST_YMin(ST_Transform(ST_SetSRID(ext::geometry, srid), 4326)) END AS lat_min,
       CASE WHEN srid > 0 AND ext IS NOT NULL
            THEN ST_XMax(ST_Transform(ST_SetSRID(ext::geometry, srid), 4326)) END AS lon_max,
       CASE WHEN srid > 0 AND ext IS NOT NULL
            THEN ST_YMax(ST_Transform(ST_SetSRID(ext::geometry, srid), 4326)) END AS lat_max
FROM s
""".strip()


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def interpret_layer(row: dict[str, Any], table: str, column: str, column_type: str,
                    metric_override: int | None = None, british_epsg: int = 27700) -> LayerStats:
    stats = LayerStats(table=table, column=column, column_type=column_type)
    stats.n_rows = int(row.get("n_rows") or 0)
    stats.n_geom = int(row.get("n_geom") or 0)
    stats.n_empty = int(row.get("n_empty") or 0)
    stats.n_invalid = int(row.get("n_invalid") or 0)
    stats.n_srids = int(row.get("n_srids") or 0)
    stats.srid = int(row["srid"]) if row.get("srid") is not None else None
    stats.geom_types = sorted(t for t in str(row.get("geom_types") or "").split(",") if t)
    native = tuple(_num(row.get(k)) for k in ("xmin", "ymin", "xmax", "ymax"))
    stats.native_extent = native if None not in native else None  # type: ignore[assignment]
    wgs = tuple(_num(row.get(k)) for k in ("lon_min", "lat_min", "lon_max", "lat_max"))
    if None not in wgs:
        stats.bbox = BBox(*wgs)  # type: ignore[arg-type]

    w = stats.warnings
    nulls = stats.n_rows - stats.n_geom
    if stats.n_rows == 0:
        w.append("The layer has no rows.")
    if nulls:
        w.append(f"{nulls} row(s) have no geometry and will be excluded from spatial operations.")
    if stats.n_empty:
        w.append(f"{stats.n_empty} empty geometr{'y' if stats.n_empty == 1 else 'ies'}.")
    if stats.n_invalid:
        w.append(f"{stats.n_invalid} invalid geometr{'y' if stats.n_invalid == 1 else 'ies'} (e.g. self-intersections); "
                 "spatial predicates may fail or miss matches. ST_MakeValid can repair them in a query.")
    if stats.n_srids > 1:
        w.append(f"The layer mixes {stats.n_srids} SRIDs; transform to one CRS before analysis.")
    if stats.native_extent:
        w.extend(coordinate_warnings(stats.srid, *stats.native_extent))
    if is_geographic(stats.srid) and column_type == "geometry":
        w.append(f"Coordinates are in degrees (EPSG:{stats.srid}). Transform before measuring distance or area.")
    stats.metric_epsg, stats.metric_reason = recommend_metric_crs(stats.bbox, metric_override, british_epsg)
    return stats


def join_test_sql(left: tuple[str, str, str], right: tuple[str, str, str], predicate: str = "ST_Intersects",
                  sample: int = 5000) -> str:
    """Count matches between two layers after aligning CRS (right is transformed to the left's SRID)."""
    if predicate not in ("ST_Intersects", "ST_Contains", "ST_Within", "ST_Covers", "ST_CoveredBy"):
        raise ValueError("Unsupported predicate")
    ls, lt, lc = left
    rs, rt, rc = right
    return f"""
WITH a AS (SELECT {quote_ident(lc)}::geometry AS g FROM {quote_ident(ls)}.{quote_ident(lt)} WHERE {quote_ident(lc)} IS NOT NULL),
b AS (SELECT {quote_ident(rc)}::geometry AS g FROM {quote_ident(rs)}.{quote_ident(rt)} WHERE {quote_ident(rc)} IS NOT NULL LIMIT {int(sample)})
SELECT count(*) AS matched_pairs, count(DISTINCT b.g) AS matched_right
FROM a JOIN b ON {predicate}(a.g, ST_Transform(b.g, ST_SRID(a.g)))
""".strip()


def diagnose_join(left: LayerStats, right: LayerStats, match_count: int | None = None) -> dict[str, Any]:
    """Explain why a spatial join might return zero (or too few) records."""
    findings: list[str] = []
    fixes: list[str] = []
    if left.srid != right.srid:
        findings.append(f"CRS mismatch: {left.table} is EPSG:{left.srid}, {right.table} is EPSG:{right.srid}.")
        fixes.append(f"Transform one side, e.g. ST_Transform({right.column}, {left.srid or 27700}).")
    if 0 in (left.srid, right.srid) or None in (left.srid, right.srid):
        findings.append("At least one layer has no SRID, so transformation is impossible until it is set.")
    if left.bbox and right.bbox and not left.bbox.overlaps(right.bbox, tolerance=0.01):
        findings.append("The two layers' extents do not overlap at all, so no feature can match.")
        fixes.append("Check that both layers cover the same area and that longitude/latitude are not swapped.")
    for layer in (left, right):
        if layer.n_invalid:
            findings.append(f"{layer.table} has {layer.n_invalid} invalid geometries.")
            fixes.append(f"Wrap with ST_MakeValid({layer.column}) in the join.")
        if layer.n_geom < layer.n_rows:
            findings.append(f"{layer.table} has {layer.n_rows - layer.n_geom} null geometries (never match).")
        swap = [w for w in layer.warnings if "swapped" in w or "wrong order" in w or "wrong SRID" in w]
        findings.extend(f"{layer.table}: {w}" for w in swap)
    if left.column_type != right.column_type:
        findings.append("One column is geography and the other geometry; cast both to the same type.")
        fixes.append("Use ::geometry on the geography column (then ST_Transform) for predicate tests.")
    point_types = {"POINT", "MULTIPOINT"}
    poly_types = {"POLYGON", "MULTIPOLYGON"}
    if set(left.geom_types) & poly_types and set(right.geom_types) & point_types:
        fixes.append("For points in polygons, ST_Contains misses points exactly on boundaries; "
                     "ST_Covers or ST_Intersects includes them.")
    if match_count is not None:
        findings.append(f"A CRS-aligned ST_Intersects test found {match_count} matching pairs.")
        if match_count > 0 and left.srid != right.srid:
            fixes.append("Matches appear once CRSs are aligned, so the original join was missing ST_Transform.")
    if not findings:
        findings.append("No structural problem found: CRS, extents and validity look compatible.")
    return {"findings": findings, "suggested_fixes": list(dict.fromkeys(fixes))}
