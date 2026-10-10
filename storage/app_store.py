"""Local SQLite store for application metadata.

The analytical database is accessed by a read-only role, so application state
(history, audit log, data dictionary, dashboards) lives here instead. No
credentials and no row-level result data are written to this store.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from models.schemas import DashboardSpec, HistoryEntry, utc_now
from security.redaction import redact

_SCHEMA = """
CREATE TABLE IF NOT EXISTS query_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    verdict TEXT NOT NULL,
    sql TEXT NOT NULL,
    detail TEXT,
    row_count INTEGER,
    elapsed_ms INTEGER
);
CREATE TABLE IF NOT EXISTS history (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS data_dictionary (
    schema_name TEXT NOT NULL,
    table_name TEXT NOT NULL,
    column_name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL,
    units TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (schema_name, table_name, column_name)
);
CREATE TABLE IF NOT EXISTS dashboards (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    spec TEXT NOT NULL
);
"""


class AppStore:
    def __init__(self, path: Path, json_dir: Path | None = None) -> None:
        self.path = Path(path)
        self.json_dir = Path(json_dir) if json_dir else None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    # --------------------------------------------------------------- audit
    def audit(
        self,
        sql: str,
        verdict: str,
        detail: str | None = None,
        row_count: int | None = None,
        elapsed_ms: int | None = None,
        source: str = "agent",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO query_audit (created_at, source, verdict, sql, detail, row_count, elapsed_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (utc_now().isoformat(), source, verdict, redact(sql or "")[:20000],
                 redact(detail) if detail else None, row_count, elapsed_ms),
            )

    def recent_audit(self, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM query_audit ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- history
    def add_history(self, entry: HistoryEntry) -> None:
        payload = entry.model_dump(mode="json")
        payload["question"] = redact(payload["question"])
        payload["errors"] = [redact(e) for e in payload["errors"]]
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO history (id, created_at, payload) VALUES (?, ?, ?)",
                (entry.id, entry.created_at.isoformat(), json.dumps(payload)),
            )

    def list_history(self, limit: int = 500) -> list[HistoryEntry]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM history ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [HistoryEntry.model_validate_json(r["payload"]) for r in rows]

    def clear_history(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM history")

    # ---------------------------------------------------- data dictionary
    def set_description(
        self, schema: str, table: str, column: str | None, description: str, units: str | None = None
    ) -> None:
        description = (description or "").strip()
        with self._connect() as conn:
            if not description and not units:
                conn.execute(
                    "DELETE FROM data_dictionary WHERE schema_name=? AND table_name=? AND column_name=?",
                    (schema, table, column or ""),
                )
                return
            conn.execute(
                "INSERT INTO data_dictionary (schema_name, table_name, column_name, description, units, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(schema_name, table_name, column_name) DO UPDATE SET"
                " description=excluded.description, units=excluded.units, updated_at=excluded.updated_at",
                (schema, table, column or "", description[:2000], (units or None), utc_now().isoformat()),
            )

    def get_dictionary(self, schema: str | None = None, table: str | None = None) -> list[dict]:
        query = "SELECT schema_name, table_name, column_name, description, units, updated_at FROM data_dictionary"
        clauses, args = [], []
        if schema:
            clauses.append("schema_name = ?")
            args.append(schema)
        if table:
            clauses.append("table_name = ?")
            args.append(table)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY schema_name, table_name, column_name"
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(query, args).fetchall()]

    # ---------------------------------------------------------- dashboards
    def save_dashboard(self, spec: DashboardSpec) -> DashboardSpec:
        spec = spec.model_copy(update={"updated_at": utc_now()})
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO dashboards (id, name, updated_at, spec) VALUES (?, ?, ?, ?)",
                (spec.id, spec.name, spec.updated_at.isoformat(), spec.model_dump_json()),
            )
        if self.json_dir is not None:
            # Portable copy of the configuration (recipes only, no data).
            self.json_dir.mkdir(parents=True, exist_ok=True)
            (self.json_dir / f"{spec.id}.json").write_text(spec.model_dump_json(indent=2), encoding="utf-8")
        return spec

    def list_dashboards(self) -> list[DashboardSpec]:
        with self._connect() as conn:
            rows = conn.execute("SELECT spec FROM dashboards ORDER BY updated_at DESC").fetchall()
        return [DashboardSpec.model_validate_json(r["spec"]) for r in rows]

    def get_dashboard(self, dashboard_id: str) -> DashboardSpec | None:
        with self._connect() as conn:
            row = conn.execute("SELECT spec FROM dashboards WHERE id = ?", (dashboard_id,)).fetchone()
        return DashboardSpec.model_validate_json(row["spec"]) if row else None

    def find_dashboard_by_name(self, name: str) -> DashboardSpec | None:
        for spec in self.list_dashboards():
            if spec.name.strip().lower() == name.strip().lower():
                return spec
        return None

    def delete_dashboard(self, dashboard_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM dashboards WHERE id = ?", (dashboard_id,))
        if self.json_dir is not None:
            (self.json_dir / f"{dashboard_id}.json").unlink(missing_ok=True)
