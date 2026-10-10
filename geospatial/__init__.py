"""Geospatial services (Phase 3): CRS selection, geometry QA, spatial joins and maps.

Planned modules:
- crs.py         choose a metric CRS (EPSG:27700 for Great Britain, otherwise local UTM; override via
                 METRIC_CRS_OVERRIDE_EPSG) and flag geographic-CRS distance/area misuse
- spatial_qa.py  validity, empty/null geometries, SRID mismatch, lon/lat swap and extent-overlap checks;
                 diagnoses zero-match spatial joins
- maps.py        choropleth, point, proportional-symbol and time-series maps (Plotly MapLibre)
"""
