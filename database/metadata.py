"""Fixed, parameterised catalog queries for schema inspection.

These are the only queries that touch `pg_catalog` / `information_schema`.
They are written by the application, not by the model, and every value is a
bound parameter. Results are always filtered to the approved schemas.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from sqlalchemy import Engine, bindparam, text

from config import Settings
from models.schemas import ColumnInfo, ForeignKeyInfo, TableInfo
from security.sql_validator import TableCatalog

logger = logging.getLogger(__name__)

_SCHEMAS_SQL = text(
    """
    SELECT nspname FROM pg_namespace
    WHERE lower(nspname) IN :approved AND has_schema_privilege(oid, 'USAGE')
    ORDER BY nspname
    """
).bindparams(bindparam("approved", expanding=True))

_TABLES_SQL = text(
    """
    SELECT n.nspname AS schema_name, c.relname AS table_name,
           CASE c.relkind WHEN 'r' THEN 'table' WHEN 'p' THEN 'table' WHEN 'v' THEN 'view'
                          WHEN 'm' THEN 'materialized view' WHEN 'f' THEN 'foreign table' END AS table_type,
           GREATEST(c.reltuples, -1)::bigint AS estimated_rows,
           obj_description(c.oid, 'pg_class') AS description
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname IN :schemas
      AND c.relkind IN ('r','p','v','m','f')
      AND NOT c.relispartition
      AND has_table_privilege(c.oid, 'SELECT')
    ORDER BY n.nspname, c.relname
    """
).bindparams(bindparam("schemas", expanding=True))

_COLUMNS_SQL = text(
    """
    SELECT a.attname AS name,
           format_type(a.atttypid, a.atttypmod) AS data_type,
           t.typname AS base_type,
           NOT a.attnotnull AS nullable,
           col_description(c.oid, a.attnum) AS description
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_type t ON t.oid = a.atttypid
    WHERE n.nspname = :schema AND c.relname = :table AND a.attnum > 0 AND NOT a.attisdropped
    ORDER BY a.attnum
    """
)

_PK_SQL = text(
    """
    SELECT a.attname
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
    WHERE i.indisprimary AND n.nspname = :schema AND c.relname = :table
    """
)

_FK_SQL = text(
    """
    SELECT a.attname AS column_name,
           rn.nspname || '.' || rc.relname AS references_table,
           ra.attname AS references_column
    FROM pg_constraint con
    JOIN pg_class c ON c.oid = con.conrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_class rc ON rc.oid = con.confrelid
    JOIN pg_namespace rn ON rn.oid = rc.relnamespace
    JOIN LATERAL unnest(con.conkey, con.confkey) AS k(attnum, refnum) ON TRUE
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
    JOIN pg_attribute ra ON ra.attrelid = rc.oid AND ra.attnum = k.refnum
    WHERE con.contype = 'f' AND n.nspname = :schema AND c.relname = :table
    """
)

_HAS_POSTGIS_SQL = text("SELECT 1 FROM pg_extension WHERE extname = 'postgis'")

_GEOM_SQL = text(
    """
    SELECT f_geometry_column AS col, type AS geometry_type, srid FROM public.geometry_columns
    WHERE f_table_schema = :schema AND f_table_name = :table
    UNION ALL
    SELECT f_geography_column, type, srid FROM public.geography_columns
    WHERE f_table_schema = :schema AND f_table_name = :table
    """
)

_SEARCH_SQL = text(
    """
    SELECT n.nspname AS schema_name, c.relname AS table_name, a.attname AS column_name,
           format_type(a.atttypid, a.atttypmod) AS data_type
    FROM pg_attribute a
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname IN :schemas AND c.relkind IN ('r','p','v','m','f')
      AND a.attnum > 0 AND NOT a.attisdropped AND has_table_privilege(c.oid, 'SELECT')
      AND (c.relname ILIKE :pattern ESCAPE '\\' OR a.attname ILIKE :pattern ESCAPE '\\')
    ORDER BY 1, 2, a.attnum
    LIMIT 300
    """
).bindparams(bindparam("schemas", expanding=True))


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass
class _CacheEntry:
    value: object
    created: float


class MetadataService:
    """Schema inspection restricted to approved schemas, with a short TTL cache."""

    def __init__(self, engine: Engine, settings: Settings, ttl_seconds: int = 300) -> None:
        self.engine = engine
        self.settings = settings
        self.ttl = ttl_seconds
        self._cache: dict[str, _CacheEntry] = {}

    def _cached(self, key: str, loader):  # type: ignore[no-untyped-def]
        entry = self._cache.get(key)
        if entry and time.monotonic() - entry.created < self.ttl:
            return entry.value
        value = loader()
        self._cache[key] = _CacheEntry(value, time.monotonic())
        return value

    def clear_cache(self) -> None:
        self._cache.clear()

    # ----------------------------------------------------------- schemas
    def list_schemas(self) -> list[str]:
        approved = [s.lower() for s in self.settings.approved_schemas]

        def load() -> list[str]:
            with self.engine.connect() as conn:
                rows = conn.execute(_SCHEMAS_SQL, {"approved": approved}).scalars().all()
                conn.rollback()
            return list(rows)

        return self._cached("schemas", load)

    def missing_schemas(self) -> list[str]:
        found = {s.lower() for s in self.list_schemas()}
        return [s for s in self.settings.approved_schemas if s.lower() not in found]

    # ------------------------------------------------------------ tables
    def list_tables(self, schema: str | None = None) -> list[TableInfo]:
        schemas = self.list_schemas()
        if schema is not None:
            schemas = [s for s in schemas if s.lower() == schema.lower()]
        if not schemas:
            return []

        def load() -> list[TableInfo]:
            with self.engine.connect() as conn:
                rows = conn.execute(_TABLES_SQL, {"schemas": schemas}).mappings().all()
                conn.rollback()
            blocked = self.settings.blocked_tables
            return [
                TableInfo(
                    schema_name=r["schema_name"],
                    table_name=r["table_name"],
                    table_type=r["table_type"] or "table",
                    estimated_rows=r["estimated_rows"] if r["estimated_rows"] >= 0 else None,
                    description=r["description"],
                )
                for r in rows
                if f"{r['schema_name']}.{r['table_name']}".lower() not in blocked
            ]

        return self._cached(f"tables:{','.join(schemas)}", load)

    def catalog(self) -> TableCatalog:
        return TableCatalog.from_pairs([(t.schema_name, t.table_name) for t in self.list_tables()])

    def resolve_table(self, schema: str | None, table: str) -> tuple[str, str] | None:
        resolved = self.catalog().resolve(schema, table)
        return resolved if isinstance(resolved, tuple) else None

    def has_postgis(self) -> bool:
        def load() -> bool:
            with self.engine.connect() as conn:
                found = conn.execute(_HAS_POSTGIS_SQL).first() is not None
                conn.rollback()
            return found

        return self._cached("postgis", load)

    def describe_table(self, schema: str | None, table: str) -> TableInfo | None:
        resolved = self.resolve_table(schema, table)
        if resolved is None:
            return None
        real_schema, real_table = resolved
        base = next(
            t for t in self.list_tables(real_schema) if t.table_name == real_table
        )
        params = {"schema": real_schema, "table": real_table}
        with self.engine.connect() as conn:
            cols = conn.execute(_COLUMNS_SQL, params).mappings().all()
            pk = set(conn.execute(_PK_SQL, params).scalars().all())
            fks = conn.execute(_FK_SQL, params).mappings().all()
            geoms: dict[str, tuple[str, int]] = {}
            if self.has_postgis():
                for g in conn.execute(_GEOM_SQL, params).mappings():
                    geoms[g["col"]] = (g["geometry_type"], g["srid"])
            conn.rollback()
        columns = []
        for c in cols:
            geo = geoms.get(c["name"])
            is_spatial = c["base_type"] in ("geometry", "geography")
            columns.append(
                ColumnInfo(
                    name=c["name"],
                    data_type=c["data_type"],
                    nullable=bool(c["nullable"]),
                    is_primary_key=c["name"] in pk,
                    geometry_type=(geo[0] if geo else ("GEOMETRY" if is_spatial else None)),
                    srid=(geo[1] if geo else None),
                    description=c["description"],
                )
            )
        return base.model_copy(
            update={
                "columns": columns,
                "primary_key": [c.name for c in columns if c.is_primary_key],
                "foreign_keys": [
                    ForeignKeyInfo(
                        column=f["column_name"],
                        references_table=f["references_table"],
                        references_column=f["references_column"],
                    )
                    for f in fks
                ],
            }
        )

    def search(self, term: str) -> list[dict[str, str]]:
        term = (term or "").strip()[:100]
        if not term:
            return []
        schemas = self.list_schemas()
        if not schemas:
            return []
        with self.engine.connect() as conn:
            rows = conn.execute(
                _SEARCH_SQL, {"schemas": schemas, "pattern": f"%{_escape_like(term)}%"}
            ).mappings().all()
            conn.rollback()
        blocked = self.settings.blocked_tables
        return [
            dict(r) for r in rows if f"{r['schema_name']}.{r['table_name']}".lower() not in blocked
        ]
