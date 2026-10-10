"""Function and schema allow/deny lists used by the SQL validator.

Extend `EXTRA_ALLOWED_FUNCTIONS` if your analyses need a PostgreSQL function
that sqlglot does not recognise. Never add anything that reads files, sleeps,
changes settings, opens network connections or writes data.
"""

from __future__ import annotations

# Schemas that agent-generated SQL may never reference, regardless of settings.
# Schema inspection uses fixed, parameterised catalog queries in `database.metadata`.
ALWAYS_BLOCKED_SCHEMAS: frozenset[str] = frozenset(
    {"pg_catalog", "information_schema", "pg_toast", "pg_temp", "topology", "tiger", "tiger_data"}
)

# Read-only PostGIS functions approved for analysis.
POSTGIS_READ_FUNCTIONS: frozenset[str] = frozenset(
    name.lower()
    for name in """
    ST_Area ST_Length ST_Length2D ST_Perimeter ST_Distance ST_DistanceSphere ST_DistanceSpheroid
    ST_DWithin ST_Intersects ST_Contains ST_ContainsProperly ST_Within ST_Covers ST_CoveredBy
    ST_Touches ST_Crosses ST_Overlaps ST_Disjoint ST_Equals ST_Relate
    ST_Transform ST_SetSRID ST_SRID ST_GeometryType ST_IsValid ST_IsValidReason ST_MakeValid
    ST_IsEmpty ST_IsSimple ST_IsClosed ST_Centroid ST_PointOnSurface ST_Buffer ST_Union
    ST_Collect ST_Envelope ST_Extent ST_3DExtent ST_AsGeoJSON ST_AsText ST_AsEWKT ST_AsBinary
    ST_AsEWKB ST_GeomFromText ST_GeomFromEWKT ST_GeomFromGeoJSON ST_GeomFromWKB
    ST_GeogFromText ST_MakePoint ST_Point ST_PointZ ST_X ST_Y ST_Z ST_NPoints ST_NumGeometries
    ST_NumInteriorRings ST_GeometryN ST_Simplify ST_SimplifyPreserveTopology ST_ClusterDBSCAN
    ST_ClusterKMeans ST_Intersection ST_Difference ST_SymDifference ST_XMin ST_XMax ST_YMin
    ST_YMax ST_Expand ST_MakeEnvelope ST_Azimuth ST_ClosestPoint ST_ShortestLine
    ST_HexagonGrid ST_SquareGrid ST_Force2D ST_CoordDim ST_Dimension ST_Multi
    ST_CollectionExtract ST_Subdivide ST_Boundary ST_ConvexHull ST_ConcaveHull ST_StartPoint
    ST_EndPoint ST_LineInterpolatePoint ST_LineLocatePoint ST_Project ST_Segmentize
    ST_FlipCoordinates ST_Summary ST_MemSize ST_HasArc ST_Reverse ST_Rotate ST_Scale
    ST_Translate ST_SnapToGrid ST_VoronoiPolygons ST_DelaunayTriangles ST_MaxDistance
    ST_HausdorffDistance ST_FrechetDistance ST_LongestLine ST_MinimumBoundingCircle
    ST_OrientedEnvelope ST_GeoHash ST_Dump ST_DumpPoints ST_Points
    Box2D Box3D GeometryType Geography Geometry
    """.split()
)

# Ordinary PostgreSQL functions that sqlglot parses as "anonymous".
STANDARD_EXTRA_FUNCTIONS: frozenset[str] = frozenset(
    """
    to_char to_date to_timestamp to_number age date_bin make_date make_timestamp make_interval
    justify_days justify_hours justify_interval isfinite date_part extract clock_timestamp
    initcap split_part btrim ltrim rtrim lpad rpad translate regexp_match regexp_matches
    regexp_split_to_array regexp_split_to_table regexp_count regexp_like starts_with strpos
    format quote_ident quote_literal concat_ws left right reverse repeat chr ascii md5 encode
    width_bucket corr covar_pop covar_samp regr_slope regr_intercept regr_r2 regr_count
    regr_avgx regr_avgy stddev_pop stddev_samp var_pop var_samp percentile_cont percentile_disc
    mode cume_dist percent_rank ntile nth_value first_value last_value bool_and bool_or every
    array_length array_position array_remove array_to_string string_to_array cardinality
    jsonb_extract_path_text json_extract_path_text jsonb_array_elements jsonb_array_length
    jsonb_typeof jsonb_object_keys json_build_object jsonb_build_object json_agg jsonb_agg
    to_json to_jsonb row_to_json trunc sign cbrt degrees radians pi random_normal div mod gcd lcm
    scale num_nulls num_nonnulls gen_random_uuid
    """.split()
)

EXTRA_ALLOWED_FUNCTIONS: frozenset[str] = frozenset()

ALLOWED_ANONYMOUS_FUNCTIONS: frozenset[str] = (
    POSTGIS_READ_FUNCTIONS | STANDARD_EXTRA_FUNCTIONS | EXTRA_ALLOWED_FUNCTIONS
)

# Functions that are always rejected, whether or not sqlglot recognises them.
DENIED_FUNCTIONS: frozenset[str] = frozenset(
    """
    pg_sleep pg_sleep_for pg_sleep_until pg_read_file pg_read_binary_file pg_ls_dir pg_stat_file
    pg_ls_logdir pg_ls_waldir pg_ls_tmpdir pg_ls_archive_statusdir pg_file_write lo_import lo_export
    lo_get lo_put lo_open lo_create lo_unlink lo_from_bytea set_config current_setting
    pg_terminate_backend pg_cancel_backend pg_reload_conf pg_rotate_logfile pg_notify nextval setval
    currval lastval query_to_xml query_to_xml_and_xmlschema table_to_xml cursor_to_xml
    database_to_xml schema_to_xml pg_switch_wal pg_promote pg_backup_start pg_backup_stop
    txid_current pg_current_xact_id addgeometrycolumn dropgeometrycolumn dropgeometrytable
    updategeometrysrid populate_geometry_columns postgis_extensions_upgrade copy
    pg_get_userbyid has_table_privilege inet_server_addr inet_server_port pg_postmaster_start_time
    """.split()
)

DENIED_FUNCTION_PREFIXES: tuple[str, ...] = (
    "dblink",
    "pg_advisory",
    "pg_try_advisory",
    "pg_logical",
    "pg_create",
    "pg_drop",
    "pg_replication",
    "pg_ls_",
    "pg_read",
    "pg_stat_reset",
    "lo_",
    "pg_catalog.",
)


def is_denied_function(name: str) -> bool:
    lowered = name.lower()
    return lowered in DENIED_FUNCTIONS or lowered.startswith(DENIED_FUNCTION_PREFIXES)
