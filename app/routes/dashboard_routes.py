"""Dashboard builder endpoints."""

from __future__ import annotations

import io

from flask import Blueprint, jsonify, request, send_file

from app.services import auto_dashboard_service as auto
from app.services import dashboard_service as svc

dashboard_bp = Blueprint("dashboards", __name__, url_prefix="/api")


@dashboard_bp.get("/dashboards")
def list_dashboards():
    return jsonify({"dashboards": svc.list_all()})


@dashboard_bp.post("/dashboards")
def create_dashboard():
    b = request.get_json(silent=True) or {}
    d = svc.create(str(b.get("dataset_id") or ""), b.get("title"), b.get("measure"), b.get("from_history"))
    return jsonify({"dashboard": {"id": d["id"], "name": d["name"], "dataset_id": d["dataset_id"], "config": d["config"]}}), 201


@dashboard_bp.get("/dashboards/<dashboard_id>")
def view_dashboard(dashboard_id: str):
    filters = {k[2:]: v for k, v in request.args.items() if k.startswith("f_")}
    return jsonify(svc.render(dashboard_id, filters))


@dashboard_bp.patch("/dashboards/<dashboard_id>")
def rename_dashboard(dashboard_id: str):
    return jsonify(svc.rename(dashboard_id, (request.get_json(silent=True) or {}).get("name", "")))


@dashboard_bp.delete("/dashboards/<dashboard_id>")
def delete_dashboard(dashboard_id: str):
    svc.delete(dashboard_id)
    return jsonify({"status": "deleted", "id": dashboard_id})


def _auto_args():
    filters = {k[2:]: request.args.getlist(k) for k in request.args if k.startswith("f_")}
    return filters, request.args.get("date_from") or None, request.args.get("date_to") or None


@dashboard_bp.get("/dashboards/<dashboard_id>/auto")
def auto_dashboard(dashboard_id: str):
    filters, d_from, d_to = _auto_args()
    return jsonify(auto.render(dashboard_id, filters, d_from, d_to, request.args.get("measure") or None, request.args.get("part", "all")))


@dashboard_bp.post("/dashboards/<dashboard_id>/refresh")
def refresh_dashboard(dashboard_id: str):
    return jsonify(auto.refresh(dashboard_id))


@dashboard_bp.put("/dashboards/<dashboard_id>/layout")
def save_dashboard_layout(dashboard_id: str):
    return jsonify({"layout": auto.save_layout(dashboard_id, request.get_json(silent=True) or {})})


@dashboard_bp.post("/dashboards/<dashboard_id>/duplicate")
def duplicate_dashboard(dashboard_id: str):
    return jsonify({"dashboard": auto.duplicate(dashboard_id)}), 201


@dashboard_bp.get("/dashboards/<dashboard_id>/export.<fmt>")
def export_dashboard(dashboard_id: str, fmt: str):
    filters, d_from, d_to = _auto_args()
    data, mimetype, name = auto.export_data(dashboard_id, fmt, filters, d_from, d_to)
    return send_file(io.BytesIO(data), mimetype=mimetype, as_attachment=True, download_name=name)
