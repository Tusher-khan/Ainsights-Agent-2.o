"""Data source (connection manager) endpoints.

Every handler goes through connection_service, which scopes all lookups to the
signed-in user. No response ever contains a password, API key or token: secrets can be
written but only their *names* are ever read back.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.services import connection_service as svc
from app.utils.exceptions import ConnectorError, FileValidationError

source_bp = Blueprint("sources", __name__, url_prefix="/api")


def _body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ConnectorError("The request was not valid.")
    return data


@source_bp.get("/data-sources/catalogue")
def catalogue():
    return jsonify({"types": svc.catalogue()})


@source_bp.get("/data-sources")
def list_sources():
    return jsonify({"sources": svc.list_sources()})


@source_bp.post("/data-sources")
def create_source():
    body = _body()
    source = svc.create_source(str(body.get("kind", "")), body.get("name", ""), body.get("settings") or {})
    return jsonify({"source": source}), 201


@source_bp.post("/data-sources/test")
def test_unsaved():
    body = _body()
    return jsonify(svc.test_unsaved(str(body.get("kind", "")), body.get("settings") or {}))


@source_bp.post("/data-sources/upload-sqlite")
def upload_sqlite():
    file = request.files.get("file")
    if not file or not file.filename:
        raise FileValidationError("Choose an SQLite file to upload.")
    return jsonify(svc.save_sqlite_upload(file)), 201


@source_bp.get("/data-sources/<source_id>")
def get_source(source_id: str):
    return jsonify({"source": svc.get_source(source_id)})


@source_bp.patch("/data-sources/<source_id>")
def update_source(source_id: str):
    body = _body()
    return jsonify({"source": svc.update_source(source_id, body.get("name"), body.get("settings"))})


@source_bp.delete("/data-sources/<source_id>")
def delete_source(source_id: str):
    svc.delete_source(source_id)
    return jsonify({"status": "deleted", "id": source_id})


@source_bp.post("/data-sources/<source_id>/test")
def test_source(source_id: str):
    return jsonify(svc.test_source(source_id))


@source_bp.post("/data-sources/<source_id>/enable")
def enable_source(source_id: str):
    return jsonify({"source": svc.set_enabled(source_id, True)})


@source_bp.post("/data-sources/<source_id>/disable")
def disable_source(source_id: str):
    return jsonify({"source": svc.set_enabled(source_id, False)})


@source_bp.get("/data-sources/<source_id>/tables")
def tables(source_id: str):
    return jsonify(svc.discover(source_id))


@source_bp.get("/data-sources/<source_id>/schema")
def table_schema(source_id: str):
    return jsonify(svc.inspect_table(source_id, request.args.get("table", "")))


@source_bp.get("/data-sources/<source_id>/preview")
def preview(source_id: str):
    rows = request.args.get("rows", "20", type=int)
    return jsonify(svc.preview(source_id, request.args.get("table", ""), rows))


@source_bp.post("/data-sources/<source_id>/import")
def import_tables(source_id: str):
    body = _body()
    tables_ = body.get("tables") or []
    if not isinstance(tables_, list):
        raise ConnectorError("The request was not valid.")
    return jsonify(svc.import_tables(source_id, [str(t) for t in tables_])), 201


@source_bp.post("/data-sources/<source_id>/refresh")
def refresh(source_id: str):
    return jsonify(svc.refresh_source(source_id))
