"""Live PostGIS tests. Need TEST_PG_* settings plus the demo layers:
    psql -U postgres -d powell_analytics -f sample_data/demo_seed_gis.sql
Skipped automatically when PostGIS or the demo layers are absent.
"""

from __future__ import annotations

import pytest

from tests.conftest import requires_db
from tools import registry

pytestmark = requires_db


@pytest.fixture()
def gis_ctx(live_ctx):
    if not live_ctx.metadata.has_postgis():
        pytest.skip("PostGIS not installed in the test database")
    if live_ctx.metadata.resolve_table("training", "demo_wards") is None:
        pytest.skip("Run sample_data/demo_seed_gis.sql to create the demo layers")
    return live_ctx


def test_describe_reports_geometry_and_srid(gis_ctx):
    out = registry.dispatch(gis_ctx, "describe_table", {"schema_name": "training", "table": "demo_wards"})
    geom = next(c for c in out.payload["columns"] if c["name"] == "geom")
    assert geom["srid"] == 27700 and "POLYGON" in geom["geometry_type"].upper()


def test_layer_check_finds_invalid_and_metric_crs(gis_ctx):
    out = registry.dispatch(gis_ctx, "check_spatial_data", {"layer": {"schema_name": "training", "table": "demo_wards"}})
    assert out.ok, out.payload
    layer = out.payload["layer"]
    assert layer["rows"] == 12 and layer["invalid"] == 1 and layer["srid"] == 27700
    assert layer["recommended_metric_crs"] == 27700
    assert -2.1 < layer["extent_wgs84"][0] < -1.7 and 52.3 < layer["extent_wgs84"][1] < 52.6


def test_join_diagnosis_detects_crs_mismatch_and_matches_after_transform(gis_ctx):
    out = registry.dispatch(gis_ctx, "check_spatial_data", {
        "layer": {"schema_name": "training", "table": "demo_wards"},
        "other_layer": {"schema_name": "training", "table": "demo_health_facilities"}})
    assert out.ok, out.payload
    findings = " ".join(out.payload["join_diagnosis"]["findings"])
    assert "CRS mismatch" in findings and "null geometries" in findings
    assert out.payload["join_test"]["matched_pairs"] > 60  # 78 facilities placed inside the wards' extent


def test_naive_join_without_transform_fails_then_choropleth(gis_ctx):
    from security.errors import SQLValidationError

    naive = ("SELECT w.ward_name, count(f.facility_id) AS n FROM training.demo_wards w "
             "JOIN training.demo_health_facilities f ON ST_Contains(w.geom, f.geom) GROUP BY 1")
    with pytest.raises(SQLValidationError, match="(?i)mixed srid|srid"):
        gis_ctx.executor.run(naive)
    sql = ("SELECT w.ward_name, w.population, count(f.facility_id) AS facilities, "
           "ST_AsGeoJSON(ST_Transform(ST_SimplifyPreserveTopology(w.geom, 10), 4326), 6) AS geojson "
           "FROM training.demo_wards w LEFT JOIN training.demo_health_facilities f "
           "ON ST_Covers(ST_MakeValid(w.geom), ST_Transform(f.geom, 27700)) "
           "GROUP BY w.ward_id, w.ward_name, w.population, w.geom")
    out = registry.dispatch(gis_ctx, "execute_read_only_sql", {"sql": sql, "purpose": "facilities per ward"})
    assert out.ok, out.payload
    data = gis_ctx.results[out.result_id].data
    assert len(data) == 12 and data["facilities"].sum() >= 70
    m = registry.dispatch(gis_ctx, "generate_geospatial_map", {
        "source": out.result_id, "map_type": "choropleth", "title": "Facilities per ward",
        "value": "facilities", "label": "ward_name", "units": "facilities"})
    assert m.ok, m.payload


def test_distance_in_metres(gis_ctx):
    sql = ("SELECT ST_Distance(ST_Transform(a.geom, 27700), ST_Transform(b.geom, 27700)) AS metres "
           "FROM training.demo_health_facilities a, training.demo_health_facilities b "
           "WHERE a.facility_id = 1 AND b.facility_id = 80")
    out = gis_ctx.executor.run(sql)
    assert 150_000 < float(out.data["metres"].iloc[0]) < 190_000  # Birmingham to London ~160 km
