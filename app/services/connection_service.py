"""Data source lifecycle: create, test, discover, import, refresh, disable, delete.

Authorization model
-------------------
Every function reads or writes through `source_repo`, which filters by the signed-in
owner and fails closed when there is no user. A source or dataset id taken from a
request is therefore only ever a *lookup key inside the caller's own rows*: another
user's id simply is not found. The connector layer receives decrypted credentials
only inside this module, for the length of one call.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path

import pandas as pd

from config import Config
from app.connectors import dialects, registry
from app.connectors.base import DataConnector, TableRef
from app.connectors.sqlite_conn import connections_dir, looks_like_sqlite
from app.database import repository, source_repo
from app.files.dataframe_manager import frame_manager
from app.files.file_profiler import build_profile
from app.rag import metadata_retriever
from app.security.request_scope import owner_scope
from app.security.secrets import SecretDecryptError, decrypt_json, encrypt_json
from app.services import dataset_service
from app.utils.exceptions import (
    AInsightError, ConnectorError, DataSourceNotFoundError, FileValidationError, UnsupportedRequestError,
)
from app.utils.helpers import new_id, slugify, utcnow
from app.utils.logger import get_logger

log = get_logger("services.connections")

MAX_TABLES_PER_IMPORT = 25
SQLITE_EXTENSIONS = {".sqlite", ".sqlite3", ".db"}
MAX_TEXT = 500
MAX_JSON_FIELD = 5000


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _clean_name(name: str) -> str:
    name = "".join(ch for ch in str(name or "") if ch.isprintable()).strip()
    if not 1 <= len(name) <= 80:
        raise ConnectorError("Give this connection a name (up to 80 characters).")
    return name


def _clean_payload(payload: dict) -> dict:
    out = {}
    for key, value in (payload or {}).items():
        if isinstance(value, str):
            if len(value) > MAX_TEXT * 4 and key not in {"token", "api_key"}:
                raise ConnectorError(f"'{key}' is too long.")
            out[key] = value.strip()
        elif isinstance(value, (dict, list)):
            if len(json.dumps(value)) > MAX_JSON_FIELD:
                raise ConnectorError(f"'{key}' is too large.")
            out[key] = value
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        else:
            raise ConnectorError(f"'{key}' has an unsupported value.")
    return out


def _owner_tag() -> str:
    return hashlib.sha256((owner_scope() or "").encode()).hexdigest()[:12]


def _get(source_id: str, with_secrets: bool = False) -> dict:
    record = source_repo.get(source_id, with_secrets=with_secrets)
    if not record:
        raise DataSourceNotFoundError()
    return record


def _connector(record: dict) -> DataConnector:
    secrets = decrypt_json(record.get("secrets_enc"))
    return registry.build(record["kind"], record["config"], secrets)


def _public(record: dict, datasets: list[dict] | None = None) -> dict:
    cfg = {k: v for k, v in record["config"].items() if k != "stored_file"}
    secret_fields: list[str] = []
    needs_credentials = False
    if record.get("has_secrets") or record.get("secrets_enc"):
        try:
            full = source_repo.get(record["id"], with_secrets=True)
            secret_fields = sorted(decrypt_json(full["secrets_enc"]).keys())
        except SecretDecryptError:
            needs_credentials = True
    cls = registry.CONNECTORS.get(record["kind"])
    return {
        "id": record["id"],
        "name": record["name"],
        "kind": record["kind"],
        "status": "disabled" if not record["enabled"] else record["status"],
        "status_message": record.get("status_message") or "",
        "warnings": record.get("warnings", []),
        "last_checked_at": record.get("last_checked_at"),
        "enabled": record["enabled"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "config": cfg,
        "secret_fields_set": secret_fields,  # names only, never values
        "needs_credentials": needs_credentials,
        "read_only_session": bool(cls and cls.enforces_read_only_session),
        "dataset_count": len(datasets) if datasets is not None else None,
    }


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------


def catalogue() -> list[dict]:
    return registry.catalogue()


def list_sources() -> list[dict]:
    out = []
    for record in source_repo.list_all():
        out.append(_public(record, source_repo.datasets_for(record["id"])))
    return out


def get_source(source_id: str) -> dict:
    record = _get(source_id)
    return _public(record, source_repo.datasets_for(source_id))


def _check_sqlite_ownership(kind: str, config: dict) -> None:
    if kind == "sqlite":
        stored = str(config.get("stored_file", ""))
        if not stored.startswith(_owner_tag() + "-"):
            raise ConnectorError("Upload the SQLite file again.", "stored file not owned by caller")


def create_source(kind: str, name: str, payload: dict) -> dict:
    if kind not in registry.CONNECTORS:
        raise ConnectorError("That type of data source is not supported.")
    name = _clean_name(name)
    if source_repo.count() >= Config.CONNECTOR_MAX_SOURCES_PER_USER:
        raise ConnectorError(f"You can connect up to {Config.CONNECTOR_MAX_SOURCES_PER_USER} data sources.")
    config, secrets = registry.split_fields(kind, _clean_payload(payload))
    _check_sqlite_ownership(kind, config)
    if kind == "rest":
        registry.build(kind, config, secrets).validate_config()
    record = source_repo.create(name, kind, config, encrypt_json(secrets) if secrets else None)
    log.info("Created data source %s (%s)", record["id"], kind)
    return _public(record, [])


def update_source(source_id: str, name: str | None, payload: dict | None) -> dict:
    record = _get(source_id, with_secrets=True)
    fields: dict = {"status": "untested", "status_message": "", "warnings": []}
    if name is not None:
        fields["name"] = _clean_name(name)
    if payload:
        new_config, new_secrets = registry.split_fields(record["kind"], _clean_payload(payload))
        config = {**record["config"], **new_config}
        # A blank secret field means "keep the saved value"; only provided values replace it.
        secrets = decrypt_json(record.get("secrets_enc")) if record.get("secrets_enc") else {}
        secrets.update(new_secrets)
        _check_sqlite_ownership(record["kind"], config)
        if record["kind"] == "rest":
            registry.build(record["kind"], config, secrets).validate_config()
        fields["config"] = config
        fields["secrets_enc"] = encrypt_json(secrets) if secrets else None
    updated = source_repo.update(source_id, **fields)
    frame_cache_clear_for(source_id)
    return _public(updated, source_repo.datasets_for(source_id))


def set_enabled(source_id: str, enabled: bool) -> dict:
    _get(source_id)
    fields = {"enabled": enabled}
    if not enabled:
        fields.update(status="disabled", status_message="Disabled by you. Analyses will not use this source.")
    else:
        fields.update(status="untested", status_message="")
    updated = source_repo.update(source_id, **fields)
    frame_cache_clear_for(source_id)
    return _public(updated, source_repo.datasets_for(source_id))


def delete_source(source_id: str) -> bool:
    record = _get(source_id)
    for dataset in source_repo.datasets_for(source_id):
        dataset_service.delete_dataset(dataset["id"], remove_file=True)
    if record["kind"] == "sqlite":
        _remove_sqlite_file(record["config"].get("stored_file"))
    deleted = source_repo.delete(source_id)
    log.info("Deleted data source %s", source_id)
    return deleted


def delete_all_for_owner(user_id: str) -> None:
    """Used by account deletion (runs inside system_scope)."""
    for record in source_repo.list_all(owner_id=user_id):
        for dataset in source_repo.datasets_for(record["id"], owner_id=user_id):
            dataset_service.delete_dataset(dataset["id"], owner_id=user_id, remove_file=True)
        if record["kind"] == "sqlite":
            _remove_sqlite_file(record["config"].get("stored_file"))
        source_repo.delete(record["id"], owner_id=user_id)


def _remove_sqlite_file(stored: str | None) -> None:
    if not stored:
        return
    try:
        path = (connections_dir() / stored).resolve()
        if connections_dir().resolve() in path.parents and path.is_file():
            path.unlink()
    except OSError as exc:
        log.warning("Could not remove SQLite upload %s: %s", stored, exc)


def frame_cache_clear_for(source_id: str) -> None:
    for dataset in source_repo.datasets_for(source_id):
        frame_manager.invalidate(dataset["id"])


# --------------------------------------------------------------------------
# SQLite upload
# --------------------------------------------------------------------------


def save_sqlite_upload(file_storage) -> dict:
    filename = file_storage.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in SQLITE_EXTENSIONS:
        raise FileValidationError("Upload an SQLite database file (.sqlite, .sqlite3 or .db).")
    file_storage.stream.seek(0, 2)
    size = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size == 0 or size > Config.MAX_CONTENT_LENGTH:
        raise FileValidationError(f"The file must be between 1 byte and {Config.MAX_FILE_SIZE_MB} MB.")
    stored = f"{_owner_tag()}-{uuid.uuid4().hex}.sqlite"
    destination = connections_dir() / stored
    file_storage.save(destination)
    try:
        if not looks_like_sqlite(destination):
            raise FileValidationError("That file is not an SQLite database.")
        probe = registry.build("sqlite", {"stored_file": stored})
        if not probe.list_tables():
            raise FileValidationError("That SQLite database has no tables.")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return {"stored_file": stored, "original_name": Path(filename).name[:120], "size_bytes": size}


# --------------------------------------------------------------------------
# test / discover / preview
# --------------------------------------------------------------------------


def test_unsaved(kind: str, payload: dict) -> dict:
    """Try a connection from the wizard before anything is stored."""
    config, secrets = registry.split_fields(kind, _clean_payload(payload))
    _check_sqlite_ownership(kind, config)
    return _run_test(registry.build(kind, config, secrets))


def _run_test(connector: DataConnector) -> dict:
    try:
        result = connector.test()
        return {**result, "status": "connected"}
    except AInsightError as exc:
        log.info("Connection test failed (%s): %s", connector.kind, connector.scrub(exc.detail))
        return {"ok": False, "status": "failed", "message": exc.user_message, "warnings": []}
    except Exception as exc:  # a driver bug must never leak internals
        log.exception("Unexpected connector error (%s)", connector.kind)
        return {"ok": False, "status": "failed", "message": "The connection test failed unexpectedly.", "warnings": []}


def test_source(source_id: str) -> dict:
    record = _get(source_id, with_secrets=True)
    if not record["enabled"]:
        return {"ok": False, "status": "disabled", "message": "This data source is disabled.", "warnings": []}
    try:
        connector = _connector(record)
    except SecretDecryptError as exc:
        source_repo.update(source_id, status="failed", status_message=exc.user_message, last_checked_at=utcnow())
        return {"ok": False, "status": "failed", "message": exc.user_message, "warnings": []}
    result = _run_test(connector)
    source_repo.update(
        source_id, status=result["status"], status_message=result["message"],
        warnings=result.get("warnings", []), last_checked_at=utcnow(),
    )
    return result


def _live_connector(source_id: str) -> tuple[dict, DataConnector]:
    record = _get(source_id, with_secrets=True)
    if not record["enabled"]:
        raise ConnectorError("This data source is disabled. Enable it first.")
    return record, _connector(record)


def discover(source_id: str) -> dict:
    record, connector = _live_connector(source_id)
    tables = connector.list_tables()
    imported = {
        (d["profile"].get("connection") or {}).get("schema", "") + "." + (d["profile"].get("connection") or {}).get("table", "")
        for d in source_repo.datasets_for(source_id)
    }
    return {
        "source_id": source_id,
        "tables": [
            {"ref": t.ref_id, "schema": t.schema, "name": t.name, "kind": t.kind, "imported": t.ref_id in imported}
            for t in tables
        ],
    }


def _resolve_ref(connector: DataConnector, ref_id: str) -> TableRef:
    """Map a client-supplied table id onto a table the connector actually listed."""
    for table in connector.list_tables():
        if table.ref_id == ref_id:
            return table
    raise ConnectorError("That table was not found in this data source.", f"unknown table {ref_id!r}")


def inspect_table(source_id: str, ref_id: str) -> dict:
    record, connector = _live_connector(source_id)
    ref = _resolve_ref(connector, ref_id)
    info = connector.describe_table(ref)
    return {
        "ref": ref.ref_id,
        "columns": [{"name": c.name, "type": c.type, "nullable": c.nullable, "primary_key": c.primary_key} for c in info.columns],
        "primary_keys": info.primary_keys,
        "foreign_keys": info.foreign_keys,
    }


def preview(source_id: str, ref_id: str, rows: int = 20) -> dict:
    record, connector = _live_connector(source_id)
    rows = max(1, min(int(rows), 100))
    if not connector.supports_sql:
        frame = connector.fetch_frame(max_rows=rows)  # type: ignore[attr-defined]
        records = json.loads(frame.head(rows).to_json(orient="records", date_format="iso"))
        return {"columns": [str(c) for c in frame.columns], "rows": records}
    ref = _resolve_ref(connector, ref_id)
    table_sql = connector.table_sql(ref)
    sql = f"SELECT {dialects.top_clause(connector.dialect, rows)}* FROM {table_sql}"
    sql = dialects.apply_limit(connector.dialect, sql, rows)
    out = connector.run_query(sql, rows, Config.QUERY_TIMEOUT_SECONDS)
    return {"columns": out.columns, "rows": out.rows}


# --------------------------------------------------------------------------
# import / refresh
# --------------------------------------------------------------------------


def _register_sql_table(record: dict, connector: DataConnector, ref: TableRef, existing: dict | None) -> dict:
    info = connector.describe_table(ref)
    display = f"{record['name']}: {ref.name}"
    profile = connector.profile_table(info, display)
    profile["connection"]["table_sql"] = connector.table_sql(ref)
    profile["connection"]["source_name"] = record["name"]
    profile["source_type"] = "connection"
    if existing:
        repository.update_dataset(
            existing["id"], profile=profile, row_count=profile["row_count"],
            column_count=profile["column_count"], status="ready",
        )
        frame_manager.invalidate(existing["id"])
        metadata_retriever.index_dataset(existing["id"], profile)
        return repository.get_dataset(existing["id"])
    return dataset_service.register_dataset(
        {"name": display, "slug": slugify(display), "source_type": "connection", "table_name": ref.name,
         "connection_id": record["id"], "status": "ready"},
        profile,
    )


def _existing_dataset(source_id: str, ref: TableRef) -> dict | None:
    for dataset in source_repo.datasets_for(source_id):
        conn = dataset["profile"].get("connection") or {}
        if conn.get("schema", "") == ref.schema and conn.get("table") == ref.name:
            return dataset
    return None


def _snapshot_rest(record: dict, connector: DataConnector, existing: dict | None) -> dict:
    frame = connector.fetch_frame()  # type: ignore[attr-defined]
    dataset_id = existing["id"] if existing else new_id("ds")
    path = connections_dir() / f"{dataset_id}.csv"
    frame.to_csv(path, index=False)
    display = record["name"]
    profile = build_profile(
        frame, display, "api",
        extra={"connection": {"schema": "", "table": display, "kind": "endpoint", "dialect": "", "source_name": display,
                              "fetched_at": utcnow()}},
    )
    if existing:
        repository.update_dataset(
            dataset_id, profile=profile, row_count=profile["row_count"], column_count=profile["column_count"],
            size_bytes=path.stat().st_size, status="ready",
        )
        frame_manager.invalidate(dataset_id)
        metadata_retriever.index_dataset(dataset_id, profile)
        return repository.get_dataset(dataset_id)
    return dataset_service.register_dataset(
        {"id": dataset_id, "name": display, "slug": slugify(display), "source_type": "api", "file_path": str(path),
         "size_bytes": path.stat().st_size, "connection_id": record["id"], "status": "ready"},
        profile,
    )


def import_tables(source_id: str, refs: list[str]) -> dict:
    record, connector = _live_connector(source_id)
    if not connector.supports_sql:
        existing = (source_repo.datasets_for(source_id) or [None])[0]
        return {"datasets": [_dataset_summary(_snapshot_rest(record, connector, existing))]}
    refs = list(dict.fromkeys(refs or []))
    if not refs:
        raise ConnectorError("Choose at least one table to analyse.")
    if len(refs) > MAX_TABLES_PER_IMPORT:
        raise ConnectorError(f"Choose up to {MAX_TABLES_PER_IMPORT} tables at a time.")
    listed = {t.ref_id: t for t in connector.list_tables()}
    unknown = [r for r in refs if r not in listed]
    if unknown:
        raise ConnectorError("One of those tables was not found in this data source.", f"unknown {unknown}")
    datasets, failures = [], []
    for ref_id in refs:
        ref = listed[ref_id]
        try:
            datasets.append(_dataset_summary(_register_sql_table(record, connector, ref, _existing_dataset(source_id, ref))))
        except AInsightError as exc:
            failures.append({"table": ref_id, "message": exc.user_message})
    return {"datasets": datasets, "failures": failures}


def refresh_source(source_id: str) -> dict:
    record, connector = _live_connector(source_id)
    refreshed, failures = [], []
    if not connector.supports_sql:
        existing = (source_repo.datasets_for(source_id) or [None])[0]
        refreshed.append(_dataset_summary(_snapshot_rest(record, connector, existing)))
    else:
        listed = {t.ref_id: t for t in connector.list_tables()}
        for dataset in source_repo.datasets_for(source_id):
            conn = dataset["profile"].get("connection") or {}
            ref_id = f"{conn.get('schema', '')}.{conn.get('table')}" if conn.get("schema") else str(conn.get("table"))
            if ref_id not in listed:
                repository.update_dataset(dataset["id"], status="missing")
                failures.append({"table": ref_id, "message": "This table no longer exists in the data source."})
                continue
            try:
                refreshed.append(_dataset_summary(_register_sql_table(record, connector, listed[ref_id], dataset)))
            except AInsightError as exc:
                failures.append({"table": ref_id, "message": exc.user_message})
    source_repo.update(source_id, status="connected", status_message="Refreshed.", last_checked_at=utcnow())
    return {"datasets": refreshed, "failures": failures}


def refresh_dataset(dataset_id: str) -> dict:
    """Re-read one connected dataset (its table or API snapshot) from the live source."""
    dataset = repository.get_dataset(dataset_id)
    if not dataset or not dataset.get("connection_id"):
        raise DataSourceNotFoundError()
    record, connector = _live_connector(dataset["connection_id"])
    if not connector.supports_sql:
        return _snapshot_rest(record, connector, dataset)
    conn = dataset["profile"].get("connection") or {}
    ref = _resolve_ref(connector, f"{conn.get('schema')}.{conn['table']}" if conn.get("schema") else conn["table"])
    return _register_sql_table(record, connector, ref, dataset)


def _dataset_summary(dataset: dict) -> dict:
    return {
        "id": dataset["id"], "name": dataset["name"], "row_count": dataset["row_count"],
        "column_count": dataset["column_count"], "status": dataset["status"],
    }


# --------------------------------------------------------------------------
# execution (used by the SQL agent and the agent tools)
# --------------------------------------------------------------------------


def execute_for_source(source, sql: str, max_rows: int | None = None):
    """Run already-validated SQL for a ConnectedSQLSource, after re-checking ownership."""
    from app.core.models import ExecutionResult
    from app.utils.helpers import dataframe_to_records

    max_rows = max_rows or Config.MAX_RESULT_ROWS
    record = _get(source.connection_id, with_secrets=True)  # owner-scoped lookup
    if not record["enabled"]:
        raise ConnectorError("This data source is disabled. Enable it to run analyses.")
    connector = _connector(record)
    started = time.perf_counter()
    try:
        out = connector.run_query(sql, max_rows, Config.QUERY_TIMEOUT_SECONDS)
    except ConnectorError as exc:
        log.warning("source=%s query failed: %s", record["id"], connector.scrub(exc.detail)[:300])
        source_repo.update(record["id"], status="failed", status_message=exc.user_message, last_checked_at=utcnow())
        raise
    elapsed = round((time.perf_counter() - started) * 1000, 2)
    frame = pd.DataFrame(out.rows, columns=out.columns or None)
    scalar = frame.iloc[0, 0] if frame.shape == (1, 1) else None
    log.info("source=%s kind=%s rows=%s ms=%s", record["id"], record["kind"], len(out.rows), elapsed)
    return ExecutionResult(
        success=True, columns=[str(c) for c in frame.columns], rows=dataframe_to_records(frame),
        row_count=len(frame), scalar=scalar, sql=sql, operation=sql, engine="sql",
        execution_ms=elapsed, truncated=out.truncated,
    )


def load_frame(source) -> pd.DataFrame:
    """Pull a whole connected table into memory for multi-step analysis.

    Refuses (rather than silently sampling) when the table exceeds the configured cap:
    totals computed from a partial table would be wrong.
    """
    cap = Config.ANALYSIS_FRAME_MAX_ROWS
    if source.row_count > cap:
        raise UnsupportedRequestError(
            f"This table has {source.row_count:,} rows, which is too many for a multi-step investigation "
            f"(limit {cap:,}). Ask a more specific question, or analyse a smaller table or view.",
            "row_count above ANALYSIS_FRAME_MAX_ROWS",
        )

    def loader() -> pd.DataFrame:
        sql = f"SELECT {dialects.top_clause(source.dialect, cap + 1)}* FROM {source.table_sql}"
        sql = dialects.apply_limit(source.dialect, sql, cap + 1)
        result = execute_for_source(source, sql, max_rows=cap + 1)
        if result.truncated or result.row_count > cap:
            raise UnsupportedRequestError("This table grew beyond the analysis limit. Refresh the data source.")
        frame = pd.DataFrame(result.rows, columns=result.columns)
        for column in source.date_columns:
            if column in frame.columns:
                frame[column] = pd.to_datetime(frame[column], errors="coerce")
        return frame

    return frame_manager.get_or_load(source.id, loader, ttl_seconds=300)
