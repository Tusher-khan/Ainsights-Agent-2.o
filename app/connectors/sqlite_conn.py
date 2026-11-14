"""SQLite connector — for an SQLite database file the user *uploaded*.

Security: a user-supplied path would let any account read any SQLite file on the
server, including AInsight's own application store with every user's data. So the
connector never accepts a path. It accepts only the stored file name produced by the
upload endpoint, resolved inside CONNECTIONS_DIR, and opens it read-only behind the
same authorizer used for analytics queries.
"""

from __future__ import annotations

import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from config import Config
from app.connectors.base import ColumnInfo, TableInfo, TableRef
from app.connectors.sql_base import SQLConnector
from app.database.connection import _make_authorizer
from app.utils.exceptions import ConnectorError, ConnectorQueryError

STORED_NAME_RE = re.compile(r"^[a-f0-9]{12}-[a-f0-9]{32}\.sqlite$")
SQLITE_MAGIC = b"SQLite format 3\x00"


def connections_dir() -> Path:
    path = Config.UPLOAD_DIR / "connections"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_stored_file(stored_name: str) -> Path:
    if not STORED_NAME_RE.match(stored_name or ""):
        raise ConnectorError("Upload the SQLite file again.", "invalid stored file name")
    path = (connections_dir() / stored_name).resolve()
    if connections_dir().resolve() not in path.parents:
        raise ConnectorError("Upload the SQLite file again.", "path outside connections dir")
    if not path.is_file():
        raise ConnectorError("The uploaded SQLite file is missing. Upload it again.", f"missing {stored_name}")
    return path


def looks_like_sqlite(path: Path) -> bool:
    with open(path, "rb") as handle:
        return handle.read(16) == SQLITE_MAGIC


class SQLiteConnector(SQLConnector):
    kind = "sqlite"
    dialect = "sqlite"
    default_schema = "main"
    enforces_read_only_session = True

    def _path(self) -> Path:
        return resolve_stored_file(self.config.get("stored_file", ""))

    @contextmanager
    def _session(self, timeout_s: int | None = None, introspect: bool = False):
        timeout_s = timeout_s or Config.QUERY_TIMEOUT_SECONDS
        path = self._path()
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=min(timeout_s, 10))
        except sqlite3.Error as exc:
            raise self.fail("That file could not be opened as an SQLite database.", exc) from exc
        conn.set_authorizer(_make_authorizer(allow_introspection=introspect))
        deadline = time.monotonic() + timeout_s
        # Abort runaway queries (e.g. accidental cross joins) once the time budget is spent.
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 20000)
        try:
            yield conn
        finally:
            conn.set_authorizer(None)
            conn.close()

    def _rows(self, conn, sql, params=(), limit=None):
        try:
            cur = conn.execute(sql, params) if params else conn.execute(sql)
            cols = [d[0] for d in (cur.description or [])]
            rows = cur.fetchmany(limit) if limit else cur.fetchall()
            return cols, list(rows)
        except sqlite3.DatabaseError as exc:
            if "interrupted" in str(exc).lower():
                raise ConnectorQueryError("The query took too long and was stopped.", str(exc)) from exc
            raise

    def test(self) -> dict:
        started = time.perf_counter()
        path = self._path()
        if not looks_like_sqlite(path):
            raise ConnectorError("That file is not an SQLite database.", "bad magic header")
        with self._session(introspect=True) as conn:
            tables = self._rows(conn, "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view')")[1][0][0]
            version = sqlite3.sqlite_version
        return {
            "ok": True,
            "message": f"Opened the SQLite file ({tables} tables/views).",
            "server_version": f"SQLite {version}",
            "warnings": [],
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }

    def list_tables(self) -> list[TableRef]:
        with self._session(introspect=True) as conn:
            _, rows = self._rows(
                conn, "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        return [TableRef(schema="main", name=r[0], kind=r[1]) for r in rows]

    def describe_table(self, ref: TableRef) -> TableInfo:
        if ref.name not in {t.name for t in self.list_tables()}:
            raise ConnectorError("That table was not found in the SQLite file.", ref.ref_id)
        q = self.quote(ref.name)
        with self._session(introspect=True) as conn:
            _, cols = self._rows(conn, f"PRAGMA table_info({q})")
            _, fks = self._rows(conn, f"PRAGMA foreign_key_list({q})")
            _, count = self._rows(conn, f"SELECT COUNT(*) FROM {q}")
        return TableInfo(
            ref=ref,
            columns=[ColumnInfo(name=c[1], type=(c[2] or "TEXT").upper(), nullable=not c[3], primary_key=bool(c[5])) for c in cols],
            primary_keys=[c[1] for c in cols if c[5]],
            foreign_keys=[{"column": f[3], "references": f"{f[2]}.{f[4]}"} for f in fks],
            row_estimate=int(count[0][0]),
        )

    def profile_table(self, info: TableInfo, name: str | None = None) -> dict:
        profile = super().profile_table(info, name)
        # SQLite stores dates as text; treat ISO-looking samples in date-named columns as datetime
        # so monthly/weekly grouping with strftime works.
        from app.connectors.base import DATE_ONLY_RE

        for col in profile["columns"]:
            if col["semantic_type"] == "categorical" and re.search(r"(date|time|created|updated)", col["name"], re.I):
                samples = [str(v) for v in col["sample_values"]]
                if samples and all(DATE_ONLY_RE.match(s) for s in samples):
                    col["semantic_type"] = "datetime"
                    col["description"] = f"Column '{col['name']}' holds dates stored as text (ISO format)."
        profile["dimensions"] = [c["name"] for c in profile["columns"] if c["semantic_type"] in {"categorical", "boolean"}]
        profile["date_columns"] = [c["name"] for c in profile["columns"] if c["semantic_type"] == "datetime"]
        return profile

    def _version_sql(self) -> str:
        return "SELECT sqlite_version()"
