"""Interactive maps (Plotly MapLibre; no API token needed).

Geometry arrives as GeoJSON text in EPSG:4326, produced by PostGIS in the
query (ST_AsGeoJSON(ST_Transform(geom, 4326))). Points can also come from
latitude/longitude columns, e.g. a CSV. Nothing is reprojected in Python.
"""

from __future__ import annotations

import json
import math
from typing import Any, Iterable

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from models.schemas import ChartSpec
from visualisations.theme import SEQUENTIAL_BLUE, TEMPLATE_NAME, TEXT_SECONDARY

MAX_FEATURES = 20_000
MAX_GEOJSON_MB = 25
MAP_STYLE = "carto-positron"
QUANTILE_COLOURS = ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"]


class MapError(ValueError):
    pass


# ------------------------------------------------------------------ geometry helpers
def is_geojson_series(series: pd.Series) -> bool:
    sample = series.dropna().astype(str).head(20)
    return bool(len(sample)) and sample.str.match(r'^\s*\{\s*"type"\s*:').mean() > 0.8


def find_geojson_column(df: pd.DataFrame) -> str | None:
    for col in df.columns:
        if df[col].dtype == object or str(df[col].dtype).startswith("str"):
            if is_geojson_series(df[col]):
                return str(col)
    return None


def _coords(geometry: dict[str, Any]) -> Iterable[tuple[float, float]]:
    def walk(c: Any) -> Iterable[tuple[float, float]]:
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            yield float(c[0]), float(c[1])
        elif isinstance(c, (list, tuple)):
            for item in c:
                yield from walk(item)
    if geometry.get("type") == "GeometryCollection":
        for g in geometry.get("geometries", []):
            yield from _coords(g)
    else:
        yield from walk(geometry.get("coordinates", []))


def parse_geometries(series: pd.Series) -> list[dict[str, Any] | None]:
    out: list[dict[str, Any] | None] = []
    for value in series:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            out.append(None)
            continue
        try:
            geom = json.loads(value) if isinstance(value, str) else value
            out.append(geom if isinstance(geom, dict) and "type" in geom else None)
        except (json.JSONDecodeError, TypeError):
            out.append(None)
    return out


def bbox_of(geometries: list[dict[str, Any] | None]) -> tuple[float, float, float, float] | None:
    xs, ys = [], []
    for g in geometries:
        if g:
            for x, y in _coords(g):
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def check_wgs84(bbox: tuple[float, float, float, float] | None) -> None:
    if bbox is None:
        raise MapError("No usable geometry found in the data.")
    xmin, ymin, xmax, ymax = bbox
    if max(abs(xmin), abs(xmax)) > 180 or max(abs(ymin), abs(ymax)) > 90:
        raise MapError(
            "Coordinates are not longitude/latitude (values exceed ±180/±90). Transform in SQL with "
            "ST_AsGeoJSON(ST_Transform(geom, 4326)) before mapping."
        )
    if 49 <= xmin and xmax <= 61 and -9 <= ymin and ymax <= 2.5:
        raise MapError("Coordinates look like UK latitude/longitude in the wrong order (x holds latitude). "
                       "Use ST_FlipCoordinates in SQL or swap the columns, after confirming with the user.")


def view_for(bbox: tuple[float, float, float, float]) -> tuple[dict[str, float], float]:
    xmin, ymin, xmax, ymax = bbox
    centre = {"lon": (xmin + xmax) / 2, "lat": (ymin + ymax) / 2}
    span = max(xmax - xmin, (ymax - ymin) * 1.6, 1e-4)
    zoom = float(np.clip(math.log2(360 / span) - 0.35, 1, 16))
    return centre, zoom


def feature_collection(df: pd.DataFrame, geom_col: str, id_col: str = "__fid") -> dict[str, Any]:
    features = []
    for fid, geom in zip(df[id_col], parse_geometries(df[geom_col])):
        if geom:
            features.append({"type": "Feature", "id": str(fid), "geometry": geom, "properties": {}})
    return {"type": "FeatureCollection", "features": features}


def to_geojson_download(df: pd.DataFrame, geom_col: str | None = None) -> str:
    """Export a result as a GeoJSON FeatureCollection (properties = other columns)."""
    geom_col = geom_col or find_geojson_column(df)
    if not geom_col:
        raise MapError("No GeoJSON geometry column in this result.")
    props = df.drop(columns=[geom_col])
    features = []
    for (_, row), geom in zip(props.iterrows(), parse_geometries(df[geom_col])):
        properties = {}
        for k, v in row.items():
            if isinstance(v, (np.integer,)):
                v = int(v)
            elif isinstance(v, (np.floating,)):
                v = None if np.isnan(v) else float(v)
            elif isinstance(v, (pd.Timestamp,)):
                v = v.isoformat()
            elif v is pd.NA or (isinstance(v, float) and math.isnan(v)):
                v = None
            elif not isinstance(v, (str, int, float, bool, type(None))):
                v = str(v)
            properties[str(k)] = v
        features.append({"type": "Feature", "geometry": geom, "properties": properties})
    return json.dumps({"type": "FeatureCollection", "features": features})


# ------------------------------------------------------------------ builders
def _footer(fig: go.Figure, spec: ChartSpec, notes: list[str]) -> go.Figure:
    from visualisations.charts import footer_text

    fig.update_layout(template=TEMPLATE_NAME, title=dict(text=spec.title),
                      margin=dict(l=10, r=10, t=60, b=95), height=600)
    fig.add_annotation(text=footer_text(spec, notes), xref="paper", yref="paper", x=0, y=-0.03,
                       xanchor="left", yanchor="top", showarrow=False, align="left",
                       font=dict(size=11, color=TEXT_SECONDARY))
    return fig


def _value_label(spec: ChartSpec) -> str:
    base = spec.y_label or (spec.value or "value").replace("_", " ").capitalize()
    if not spec.units or spec.units.lower() in base.lower() or base.lower().startswith("per "):
        return base
    return f"{base} ({spec.units})"


def _choropleth(spec: ChartSpec, df: pd.DataFrame, notes: list[str]) -> go.Figure:
    geom_col = spec.geometry or find_geojson_column(df)
    if not geom_col or geom_col not in df.columns:
        raise MapError("A choropleth needs a GeoJSON geometry column. Select "
                       "ST_AsGeoJSON(ST_Transform(geom, 4326)) AS geojson in the query.")
    if not spec.value or spec.value not in df.columns:
        raise MapError("A choropleth needs a numeric value column to colour the areas.")
    data = df.copy()
    data[spec.value] = pd.to_numeric(data[spec.value], errors="coerce")
    if data[spec.value].notna().sum() == 0:
        raise MapError(f"'{spec.value}' has no numeric values.")
    geoms = parse_geometries(data[geom_col])
    kinds = {g["type"] for g in geoms if g}
    if kinds and not kinds & {"Polygon", "MultiPolygon"}:
        raise MapError(f"The geometries are {', '.join(sorted(kinds))}, not areas; use a point map instead.")
    box = bbox_of(geoms)
    check_wgs84(box)
    data["__fid"] = range(len(data))
    fc = feature_collection(data, geom_col)
    missing = int(data[spec.value].isna().sum())
    if missing:
        notes.append(f"{missing} area(s) have no value and are left blank.")
    hover = [c for c in (spec.label, spec.x) if c and c in data.columns]
    centre, zoom = view_for(box)
    if spec.classification == "quantile" and data[spec.value].nunique() >= 5:
        bins = pd.qcut(data[spec.value], 5, duplicates="drop")
        names = {iv: f"{iv.left:,.3g} – {iv.right:,.3g}" for iv in bins.cat.categories}
        cats = list(names.values())
        data["__class"] = bins.map(names).astype(str)
        fig = px.choropleth_map(
            data, geojson=fc, locations="__fid", featureidkey="id", color="__class",
            category_orders={"__class": cats}, color_discrete_sequence=QUANTILE_COLOURS[: len(cats)],
            hover_name=hover[0] if hover else None, hover_data={spec.value: ":,.4g", "__fid": False, "__class": False},
            map_style=MAP_STYLE, center=centre, zoom=zoom, opacity=0.8,
            animation_frame=spec.frame if spec.frame in data.columns else None,
        )
        fig.update_layout(legend_title_text=_value_label(spec) + " (quantiles)")
        notes.append("Classes are quantiles: each holds about a fifth of the areas.")
    else:
        fig = px.choropleth_map(
            data, geojson=fc, locations="__fid", featureidkey="id", color=spec.value,
            color_continuous_scale=SEQUENTIAL_BLUE, hover_name=hover[0] if hover else None,
            hover_data={spec.value: ":,.4g", "__fid": False}, map_style=MAP_STYLE, center=centre, zoom=zoom,
            opacity=0.8, animation_frame=spec.frame if spec.frame in data.columns else None,
        )
        fig.update_layout(coloraxis_colorbar=dict(title=_value_label(spec)))
    fig.update_traces(marker_line_width=0.6, marker_line_color="white")
    notes.append(f"{len(fc['features']):,} areas mapped (EPSG:4326 for display).")
    return fig


def _point_xy(spec: ChartSpec, df: pd.DataFrame) -> pd.DataFrame:
    data = df.copy()
    if spec.lat and spec.lon and spec.lat in data.columns and spec.lon in data.columns:
        data["__lat"] = pd.to_numeric(data[spec.lat], errors="coerce")
        data["__lon"] = pd.to_numeric(data[spec.lon], errors="coerce")
    else:
        geom_col = spec.geometry or find_geojson_column(data)
        if not geom_col:
            raise MapError("A point map needs latitude and longitude columns or a GeoJSON point column.")
        lats, lons = [], []
        for g in parse_geometries(data[geom_col]):
            pts = list(_coords(g)) if g else []
            if pts:  # centroid of vertices for non-point geometries
                lons.append(sum(p[0] for p in pts) / len(pts))
                lats.append(sum(p[1] for p in pts) / len(pts))
            else:
                lats.append(np.nan)
                lons.append(np.nan)
        data["__lat"], data["__lon"] = lats, lons
    if spec.swap_lat_lon:
        data["__lat"], data["__lon"] = data["__lon"], data["__lat"]
    return data


def _points(spec: ChartSpec, df: pd.DataFrame, notes: list[str], proportional: bool) -> go.Figure:
    data = _point_xy(spec, df)
    bad = data["__lat"].isna() | data["__lon"].isna()
    if bad.any():
        notes.append(f"{int(bad.sum())} row(s) without coordinates were not mapped.")
    data = data[~bad]
    if data.empty:
        raise MapError("No rows have usable coordinates.")
    if len(data) > MAX_FEATURES:
        data = data.sample(MAX_FEATURES, random_state=0)
        notes.append(f"Random sample of {MAX_FEATURES:,} points shown.")
    box = (data["__lon"].min(), data["__lat"].min(), data["__lon"].max(), data["__lat"].max())
    check_wgs84(box)
    centre, zoom = view_for(box)
    size = None
    if proportional:
        if not spec.value or spec.value not in data.columns:
            raise MapError("A proportional-symbol map needs a numeric value column for symbol size.")
        data[spec.value] = pd.to_numeric(data[spec.value], errors="coerce")
        if (data[spec.value] < 0).any():
            raise MapError("Symbol sizes cannot represent negative values; use a choropleth or diverging colours.")
        size = spec.value
        notes.append("Symbol area is proportional to the value.")
    colour = spec.color if spec.color in data.columns else None
    hover = [c for c in (spec.label, spec.x) if c and c in data.columns]
    fig = px.scatter_map(
        data, lat="__lat", lon="__lon", size=size, size_max=38 if proportional else None, color=colour,
        hover_name=hover[0] if hover else None,
        hover_data={c: True for c in [spec.value] if c and c in data.columns} | {"__lat": ":.5f", "__lon": ":.5f"},
        map_style=MAP_STYLE, center=centre, zoom=zoom, opacity=0.8,
        animation_frame=spec.frame if spec.frame in data.columns else None,
        color_discrete_sequence=["#0E7C7B", "#eb6834", "#4a3aa7", "#eda100", "#e87ba4", "#008300"],
    )
    if not proportional:
        fig.update_traces(marker=dict(size=9))
    notes.append(f"{len(data):,} locations mapped.")
    return fig


def build_map(spec: ChartSpec, df: pd.DataFrame) -> go.Figure:
    if df is None or df.empty:
        raise MapError("There is no data to map.")
    notes: list[str] = []
    if spec.chart_type == "choropleth":
        fig = _choropleth(spec, df, notes)
    elif spec.chart_type == "point_map":
        fig = _points(spec, df, notes, proportional=False)
    elif spec.chart_type == "proportional_map":
        fig = _points(spec, df, notes, proportional=True)
    else:
        raise MapError(f"Unknown map type {spec.chart_type}")
    return _footer(fig, spec, notes)
