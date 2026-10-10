"""CRS rules, spatial QA interpretation, join diagnostics, maps and GeoJSON export (offline)."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from geospatial.crs import BBox, coordinate_warnings, recommend_metric_crs, utm_epsg
from geospatial.maps import MapError, build_map, find_geojson_column, to_geojson_download
from geospatial.spatial_qa import diagnose_join, interpret_layer
from models.schemas import ChartSpec
from tools import registry


def square(lon: float, lat: float, d: float = 0.01) -> str:
    ring = [[lon, lat], [lon + d, lat], [lon + d, lat + d], [lon, lat + d], [lon, lat]]
    return json.dumps({"type": "Polygon", "coordinates": [ring]})


WARDS = pd.DataFrame({
    "ward_name": [f"Ward {i}" for i in range(6)],
    "facilities": [3, 0, 5, 2, 8, 1],
    "geojson": [square(-1.92 + 0.012 * i, 52.47) for i in range(6)],
})


def test_metric_crs_choice():
    assert recommend_metric_crs(BBox(-1.95, 52.45, -1.85, 52.52))[0] == 27700  # Birmingham
    assert recommend_metric_crs(BBox(31.0, -17.9, 31.1, -17.8))[0] == utm_epsg(31.05, -17.85) == 32736  # Harare
    assert recommend_metric_crs(BBox(31.0, -17.9, 31.1, -17.8), override=20936)[0] == 20936


def test_coordinate_warnings():
    assert any("wrong order" in w for w in coordinate_warnings(4326, 52.4, -1.95, 52.5, -1.85))
    assert any("wrong SRID" in w for w in coordinate_warnings(4326, 404000, 284000, 412000, 290000))
    assert any("degrees" in w for w in coordinate_warnings(27700, -1.95, 52.4, -1.85, 52.5))
    assert coordinate_warnings(27700, 404000, 284000, 412000, 290000) == []
    assert any("SRID 0" in w for w in coordinate_warnings(0, 1, 1, 2, 2))


def _row(**kw):
    base = dict(n_rows=12, n_geom=12, n_empty=0, n_invalid=0, n_srids=1, srid=27700, geom_types="MULTIPOLYGON",
                xmin=404000, ymin=284000, xmax=412000, ymax=290000,
                lon_min=-1.94, lat_min=52.45, lon_max=-1.82, lat_max=52.51)
    base.update(kw)
    return base


def test_interpret_layer_flags_problems():
    stats = interpret_layer(_row(n_invalid=1, n_geom=11), "training.demo_wards", "geom", "geometry")
    text = " ".join(stats.warnings)
    assert "1 invalid" in text and "no geometry" in text
    assert stats.metric_epsg == 27700


def test_join_diagnosis_crs_mismatch_and_extent():
    wards = interpret_layer(_row(), "training.demo_wards", "geom", "geometry")
    pts = interpret_layer(_row(srid=4326, geom_types="POINT", xmin=-1.94, ymin=52.45, xmax=-0.12, ymax=52.51,
                               lon_min=-1.94, lat_min=52.45, lon_max=-0.12, lat_max=52.51),
                          "training.demo_health_facilities", "geom", "geometry")
    d = diagnose_join(wards, pts, match_count=77)
    joined = " ".join(d["findings"] + d["suggested_fixes"])
    assert "CRS mismatch" in joined and "ST_Transform" in joined and "ST_Covers" in joined
    far = interpret_layer(_row(srid=4326, lon_min=30, lat_min=-18, lon_max=31, lat_max=-17,
                               xmin=30, ymin=-18, xmax=31, ymax=-17), "zw.points", "geom", "geometry")
    assert any("do not overlap" in f for f in diagnose_join(wards, far)["findings"])


def test_choropleth_continuous_and_quantile():
    spec = ChartSpec(chart_type="choropleth", title="Facilities per ward", value="facilities", label="ward_name",
                     units="facilities", source="training.demo_wards")
    fig = build_map(spec, WARDS)
    assert fig.layout.title.text == "Facilities per ward"
    assert fig.data[0].geojson["features"][0]["geometry"]["type"] == "Polygon"
    assert "Source: training.demo_wards" in fig.layout.annotations[-1].text
    q = build_map(spec.model_copy(update={"classification": "quantile"}), WARDS)
    assert "quantiles" in q.layout.legend.title.text
    q.to_json()


def test_choropleth_rejects_projected_coordinates_and_points():
    projected = WARDS.assign(geojson=[json.dumps({"type": "Polygon", "coordinates": [[[404000, 284000],
                             [406000, 284000], [406000, 286000], [404000, 284000]]]})] * 6)
    with pytest.raises(MapError, match="ST_Transform"):
        build_map(ChartSpec(chart_type="choropleth", title="t", value="facilities"), projected)
    points = WARDS.assign(geojson=[json.dumps({"type": "Point", "coordinates": [-1.9, 52.48]})] * 6)
    with pytest.raises(MapError, match="point map"):
        build_map(ChartSpec(chart_type="choropleth", title="t", value="facilities"), points)


def test_point_and_proportional_maps_from_lat_lon():
    df = pd.DataFrame({"name": list("abcd"), "lat": [52.48, 52.47, 52.49, 52.50],
                       "lon": [-1.90, -1.89, -1.91, -1.88], "beds": [10, 200, 50, 0]})
    fig = build_map(ChartSpec(chart_type="point_map", title="Facilities", lat="lat", lon="lon", label="name"), df)
    assert len(fig.data[0].lat) == 4
    prop = build_map(ChartSpec(chart_type="proportional_map", title="Beds", lat="lat", lon="lon", value="beds"), df)
    assert prop.data[0].marker.size is not None
    swapped = df.rename(columns={"lat": "lon", "lon": "lat"})
    with pytest.raises(MapError, match="wrong order"):
        build_map(ChartSpec(chart_type="point_map", title="t", lat="lat", lon="lon"), swapped)
    fixed = build_map(ChartSpec(chart_type="point_map", title="t", lat="lat", lon="lon", swap_lat_lon=True), swapped)
    assert len(fixed.data[0].lat) == 4


def test_geojson_export_round_trip():
    assert find_geojson_column(WARDS) == "geojson"
    fc = json.loads(to_geojson_download(WARDS))
    assert fc["type"] == "FeatureCollection" and len(fc["features"]) == 6
    assert fc["features"][0]["properties"] == {"ward_name": "Ward 0", "facilities": 3}


def test_map_tool_and_geometry_masked_from_model(offline_ctx):
    from analysis.csv_loader import load_csv
    from tools.context import CSVSource, frame_summary

    raw = WARDS.to_csv(index=False).encode()
    loaded = load_csv("wards.csv", raw)
    offline_ctx.csv_sources["wards.csv"] = CSVSource("wards.csv", loaded.raw, loaded.data, loaded.inferred_types)
    summary = frame_summary(loaded.data, 5)
    assert "geojson" not in json.dumps(summary["sample_rows"])
    assert any(c["dtype"] == "geometry (masked)" for c in summary["columns"])
    out = registry.dispatch(offline_ctx, "generate_geospatial_map", {
        "source": "csv:wards.csv", "map_type": "choropleth", "title": "Facilities per ward",
        "value": "facilities", "label": "ward_name"})
    assert out.ok, out.payload
    assert offline_ctx.charts[out.chart_id].figure.data[0].type == "choroplethmap"
    assert out.result_id in offline_ctx.export_offers


def test_spatial_tools_need_database(offline_ctx):
    offline_ctx.selected_sources = ["database"]
    out = registry.dispatch(offline_ctx, "check_spatial_data", {"layer": {"table": "demo_wards"}})
    assert not out.ok and "not connected" in out.payload["error"]
