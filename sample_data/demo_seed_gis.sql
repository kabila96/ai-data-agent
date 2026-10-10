-- =============================================================================
-- demo_seed_gis.sql (optional demonstration data, requires PostGIS)
-- Creates two small layers in the `training` schema around central Birmingham:
--   training.demo_wards             12 polygons, EPSG:27700 (British National Grid, metres)
--   training.demo_health_facilities 80 points,   EPSG:4326  (WGS84 longitude/latitude)
-- The layers deliberately use DIFFERENT CRSs, and include one invalid polygon,
-- one facility with no geometry and one facility far outside the study area,
-- so the spatial checks and join diagnostics have something to find.
--
-- Run as an administrator (not powell_agent_ro):
--   psql -U postgres -d powell_analytics -f sample_data/demo_seed_gis.sql
-- =============================================================================
\set ON_ERROR_STOP on
BEGIN;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS training;
DROP TABLE IF EXISTS training.demo_health_facilities, training.demo_wards;

CREATE TABLE training.demo_wards (
    ward_id     integer PRIMARY KEY,
    ward_name   text NOT NULL,
    population  integer NOT NULL,
    geom        geometry(MultiPolygon, 27700)
);
COMMENT ON TABLE training.demo_wards IS 'Synthetic wards around central Birmingham (EPSG:27700). Population is synthetic.';

INSERT INTO training.demo_wards (ward_id, ward_name, population, geom)
SELECT row_number() OVER (ORDER BY r, c),
       (ARRAY['Ladywood','Aston','Nechells','Bordesley','Edgbaston','Digbeth','Highgate','Sparkbrook',
              'Balsall Heath','Moseley','Small Heath','Bournville'])[(row_number() OVER (ORDER BY r, c))::int],
       (12000 + ((r * 7 + c * 13) % 9) * 1500)::int,
       ST_Multi(ST_MakeEnvelope(404000 + c * 2000, 284000 + r * 2000, 406000 + c * 2000, 286000 + r * 2000, 27700))
FROM generate_series(0, 2) AS r, generate_series(0, 3) AS c;

-- One deliberately invalid (self-intersecting "bow-tie") ward boundary.
UPDATE training.demo_wards
SET geom = ST_Multi(ST_GeomFromText(
    'POLYGON((410000 288000, 412000 290000, 412000 288000, 410000 290000, 410000 288000))', 27700))
WHERE ward_id = 12;

CREATE INDEX ON training.demo_wards USING gist (geom);

CREATE TABLE training.demo_health_facilities (
    facility_id   integer PRIMARY KEY,
    facility_name text NOT NULL,
    facility_type text NOT NULL,
    beds          integer,
    geom          geometry(Point, 4326)
);
COMMENT ON TABLE training.demo_health_facilities IS 'Synthetic health facilities (EPSG:4326). Note the CRS differs from demo_wards.';

SELECT setseed(0.21);
INSERT INTO training.demo_health_facilities
SELECT g,
       'Facility ' || g,
       (ARRAY['GP surgery','Pharmacy','Clinic','Hospital'])[1 + (g % 4)],
       CASE WHEN g % 4 = 3 THEN 50 + (random() * 400)::int ELSE NULL END,
       ST_Transform(ST_SetSRID(ST_MakePoint(404000 + random() * 8000, 284000 + random() * 6000), 27700), 4326)
FROM generate_series(1, 78) AS g;
INSERT INTO training.demo_health_facilities VALUES
 (79, 'Facility 79 (no location)', 'Clinic', NULL, NULL),
 (80, 'Facility 80 (London)', 'Hospital', 300, ST_SetSRID(ST_MakePoint(-0.1276, 51.5072), 4326));
CREATE INDEX ON training.demo_health_facilities USING gist (geom);

ANALYZE training.demo_wards, training.demo_health_facilities;
COMMIT;

SELECT 'demo_wards' AS layer, count(*) FROM training.demo_wards
UNION ALL SELECT 'demo_health_facilities', count(*) FROM training.demo_health_facilities;
