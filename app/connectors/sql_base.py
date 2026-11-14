"""Shared logic for SQL connectors that speak DB-API (PostgreSQL, MySQL, SQL Server, SQLite)."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Iterator

from config import Config
from app.connectors.base import (
    ColumnInfo, DataConnector, QueryOutput, TableInfo, TableRef, clean_value, semantic_from_sql_type,
)
from app.utils.exceptions import ConnectorError, ConnectorQueryError
from app.utils.helpers import json_safe, slugify
from app.utils.logger import get_logger

log = get_logger("connectors.sql")

MAX_PROFILE_COLUMNS = 60
DISTINCT_SKIP_ROWS = 5_000_000  # COUNT(DISTINCT) on giant tables is too costly to run on connect


class SQLConnector(DataConnector):
    supports_sql = True
    # Parameter marker used by the driver. Introspection SQL below is written with %s.
    paramstyle = "%s"

    # -- subclasses implement -------------------------------------------
    def _open(self, timeout_s: int):  # pragma: no cover - abstract
        raise NotImplementedError

    def _tables_sql(self) -> str:  # pragma: no cover
        raise NotImplementedError

    def _columns_sql(self) -> str:  # pragma: no cover
        raise NotImplementedError

    def _pk_sql(self) -> str:  # pragma: no cover
        raise NotImplementedError

    def _fk_sql(self) -> str:  # pragma: no cover
        raise NotImplementedError

    def _version_sql(self) -> str:  # pragma: no cover
        raise NotImplementedError

    def _write_privilege_warnings(self, conn) -> list[str]:
        return []

    def _period_sql(self, column: str, grain: str) -> str:  # pragma: no cover
        raise NotImplementedError

    # -- plumbing --------------------------------------------------------
    def _p(self, sql: str) -> str:
        return sql.replace("%s", self.paramstyle)

    @contextmanager
    def _session(self, timeout_s: int | None = None) -> Iterator[Any]:
        timeout_s = timeout_s or Config.QUERY_TIMEOUT_SECONDS
        try:
            conn = self._open(timeout_s)
        except ConnectorError:
            raise
        except Exception as exc:
            raise self.fail(self._friendly_connect_error(exc), exc) from exc
        try:
            yield conn
        finally:
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass

    def _friendly_connect_error(self, exc: Exception) -> str:
        text = str(exc).lower()
        if any(k in text for k in ("password", "authentication", "access denied", "login failed")):
            return "The database rejected the user name or password."
        if any(k in text for k in ("timeout", "timed out")):
            return "The connection timed out. Check the host and port, and that the database accepts outside connections."
        if any(k in text for k in ("ssl", "tls", "certificate")):
            return "A secure (TLS) connection could not be established. Check the SSL setting for this database."
        if any(k in text for k in ("does not exist", "unknown database", "cannot open database")):
            return "The database name was not found on that server."
        if any(k in text for k in ("refused", "could not connect", "can't connect", "unreachable")):
            return "The server refused the connection. Check the host, port and any firewall."
        return "Could not connect to the database."

    def _rows(self, conn, sql: str, params: tuple = (), limit: int | None = None) -> tuple[list[str], list[tuple]]:
        cur = conn.cursor()
        try:
            cur.execute(self._p(sql), params) if params else cur.execute(sql)
            cols = [d[0] for d in (cur.description or [])]
            rows = cur.fetchmany(limit) if limit else cur.fetchall()
            return cols, list(rows)
        finally:
            try:
                cur.close()
            except Exception:
                pass

    # -- contract --------------------------------------------------------
    def test(self) -> dict:
        started = time.perf_counter()
        with self._session(Config.CONNECTOR_CONNECT_TIMEOUT) as conn:
            _, rows = self._rows(conn, self._version_sql())
            version = str(rows[0][0]).splitlines()[0][:120] if rows else ""
            warnings = []
            try:
                warnings = self._write_privilege_warnings(conn)
            except Exception as exc:  # privilege probing is best effort
                log.info("Privilege check skipped for %s: %s", self.kind, self.scrub(exc))
                warnings = ["AInsight could not verify that this account is read-only. Use a dedicated read-only account."]
        if not self.enforces_read_only_session:
            warnings.append(
                "This database type has no read-only session mode AInsight can switch on, so protection "
                "relies on AInsight's SQL checks plus your account's permissions. Use a read-only account."
            )
        return {
            "ok": True,
            "message": "Connected successfully.",
            "server_version": version,
            "warnings": warnings,
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }

    def list_tables(self) -> list[TableRef]:
        with self._session(Config.CONNECTOR_CONNECT_TIMEOUT) as conn:
            _, rows = self._rows(conn, self._tables_sql())
        return [
            TableRef(schema=str(r[0] or ""), name=str(r[1]), kind="view" if "view" in str(r[2]).lower() else "table")
            for r in rows
        ]

    def describe_table(self, ref: TableRef) -> TableInfo:
        with self._session(Config.CONNECTOR_CONNECT_TIMEOUT) as conn:
            _, cols = self._rows(conn, self._columns_sql(), (ref.schema, ref.name))
            if not cols:
                raise ConnectorError("That table was not found or this account cannot see it.", f"no columns for {ref.ref_id}")
            _, pks = self._rows(conn, self._pk_sql(), (ref.schema, ref.name))
            _, fks = self._rows(conn, self._fk_sql(), (ref.schema, ref.name))
        pk_names = [str(r[0]) for r in pks]
        return TableInfo(
            ref=ref,
            columns=[
                ColumnInfo(name=str(c[0]), type=str(c[1]), nullable=str(c[2]).upper() in {"YES", "TRUE", "1"},
                           primary_key=str(c[0]) in pk_names)
                for c in cols
            ],
            primary_keys=pk_names,
            foreign_keys=[{"column": str(r[0]), "references": f"{r[1]}.{r[2]}"} for r in fks],
        )

    def run_query(self, sql: str, max_rows: int, timeout_s: int) -> QueryOutput:
        started = time.perf_counter()
        with self._session(timeout_s) as conn:
            try:
                cols, rows = self._rows(conn, sql, limit=max_rows + 1)
            except ConnectorError:
                raise
            except Exception as exc:
                raise ConnectorQueryError(
                    "The data source could not run that analysis.", self.scrub(f"{type(exc).__name__}: {exc} | sql={sql}")
                ) from exc
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        records = [{cols[i]: clean_value(v) for i, v in enumerate(r)} for r in rows]
        return QueryOutput(columns=cols, rows=records, truncated=truncated,
                           elapsed_ms=round((time.perf_counter() - started) * 1000, 2))

    # -- profiling -------------------------------------------------------
    def profile_table(self, info: TableInfo, name: str | None = None) -> dict:
        """Deterministic profile computed *inside the database* with aggregate SQL.

        Mirrors the shape produced for uploaded files so the planner, validators,
        RAG indexing and UI treat every source identically.
        """
        fk_cols = {fk["column"] for fk in info.foreign_keys}
        table_sql = self.table_sql(info.ref)
        columns = info.columns[:MAX_PROFILE_COLUMNS]
        semantics = {
            c.name: semantic_from_sql_type(c.type, c.name, c.primary_key, c.name in fk_cols) for c in columns
        }

        warnings: list[str] = []
        if len(info.columns) > MAX_PROFILE_COLUMNS:
            warnings.append(f"Only the first {MAX_PROFILE_COLUMNS} of {len(info.columns)} columns were profiled.")

        total = None
        stats: dict[str, dict] = {}
        samples: list[dict] = []
        try:
            with self._session(Config.QUERY_TIMEOUT_SECONDS * 2) as conn:
                _, rows = self._rows(conn, f"SELECT COUNT(*) FROM {table_sql}")
                total = int(rows[0][0])
                skip_distinct = total > DISTINCT_SKIP_ROWS
                parts = []
                for i, c in enumerate(columns):
                    q = self.quote(c.name)
                    sem = semantics[c.name]
                    parts.append(f"COUNT({q}) AS c{i}_n")
                    if not skip_distinct and sem != "datetime":
                        parts.append(f"COUNT(DISTINCT {q}) AS c{i}_d")
                    if sem == "numeric":
                        parts += [f"MIN({q}) AS c{i}_min", f"MAX({q}) AS c{i}_max", f"AVG({q} * 1.0) AS c{i}_avg"]
                    elif sem == "datetime":
                        parts += [f"MIN({q}) AS c{i}_min", f"MAX({q}) AS c{i}_max"]
                if parts and total:
                    cols, vals = self._rows(conn, f"SELECT {', '.join(parts)} FROM {table_sql}")
                    stats = dict(zip(cols, vals[0])) if vals else {}
                if skip_distinct:
                    warnings.append("Distinct-value counts were skipped because this table is very large.")
                sample_sql = self._sample_sql(table_sql, 10)
                s_cols, s_rows = self._rows(conn, sample_sql)
                samples = [{s_cols[j]: clean_value(v) for j, v in enumerate(r)} for r in s_rows]
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError(
                "The table was found but could not be profiled.", self.scrub(f"{type(exc).__name__}: {exc}")
            ) from exc

        profile_columns = []
        for i, c in enumerate(columns):
            sem = semantics[c.name]
            non_null = int(stats.get(f"c{i}_n") or 0)
            nulls = max((total or 0) - non_null, 0)
            distinct = stats.get(f"c{i}_d")
            sample_values = []
            for row in samples:
                v = row.get(c.name)
                if v is not None and v not in sample_values:
                    sample_values.append(v)
                if len(sample_values) >= Config.SAMPLE_VALUES_PER_COLUMN:
                    break
            entry = {
                "name": c.name,
                "dtype": c.type,
                "semantic_type": sem,
                "null_count": nulls,
                "null_percentage": round(nulls / total * 100, 2) if total else 0.0,
                "unique_count": int(distinct) if distinct is not None else 0,
                "sample_values": sample_values,
                "min": json_safe(clean_value(stats.get(f"c{i}_min"))) if sem in {"numeric", "datetime"} else None,
                "max": json_safe(clean_value(stats.get(f"c{i}_max"))) if sem in {"numeric", "datetime"} else None,
                "mean": json_safe(round(float(stats[f"c{i}_avg"]), 4)) if stats.get(f"c{i}_avg") is not None else None,
            }
            entry["description"] = (
                f"Column '{c.name}' is a {sem} field stored as {c.type}."
                + (f" Values range from {entry['min']} to {entry['max']}." if sem == "numeric" and entry["min"] is not None else "")
                + (f" {entry['null_percentage']}% of rows are missing this value." if entry["null_percentage"] else "")
            )
            profile_columns.append(entry)

        display = name or info.ref.name
        total_cells = (total or 0) * len(profile_columns) or 1
        missing = sum(c["null_count"] for c in profile_columns)
        profile = {
            "name": display,
            "slug": slugify(display),
            "source_type": self.kind,
            "row_count": total if total is not None else 0,
            "column_count": len(info.columns),
            "columns": profile_columns,
            "column_names": [c["name"] for c in profile_columns],
            "measures": [c["name"] for c in profile_columns if c["semantic_type"] == "numeric"],
            "dimensions": [c["name"] for c in profile_columns if c["semantic_type"] in {"categorical", "boolean"}],
            "date_columns": [c["name"] for c in profile_columns if c["semantic_type"] == "datetime"],
            "primary_keys": info.primary_keys,
            "foreign_keys": info.foreign_keys,
            "quality": {
                "duplicate_rows": None,  # not computed in-database: would need a full-table GROUP BY
                "missing_cells": missing,
                "missing_percentage": round(missing / total_cells * 100, 2),
                "complete_columns": sum(1 for c in profile_columns if c["null_count"] == 0),
                "score": round(max(0.0, 100 - missing / total_cells * 100), 1),
            },
            "statistics": {
                c["name"]: {"min": c["min"], "max": c["max"], "mean": c["mean"]}
                for c in profile_columns if c["semantic_type"] == "numeric"
            },
            "sample_rows": samples,
            "connection": {"schema": info.ref.schema, "table": info.ref.name, "kind": info.ref.kind, "dialect": self.dialect},
            "profile_warnings": warnings,
        }
        from app.files.file_profiler import suggested_questions

        profile["suggested_questions"] = suggested_questions(profile)
        return profile

    def _sample_sql(self, table_sql: str, n: int) -> str:
        return f"SELECT * FROM {table_sql} LIMIT {int(n)}"
