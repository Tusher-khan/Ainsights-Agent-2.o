"""Semantic layer endpoints (business metric definitions)."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.agent.metrics import MetricError
from app.services import metric_service as svc

metric_bp = Blueprint("metrics", __name__, url_prefix="/api")


def _body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise MetricError("The request was not valid.")
    return data


@metric_bp.get("/business-metrics")
def list_metrics():
    return jsonify({"metrics": svc.list_metrics(request.args.get("dataset_id"))})


@metric_bp.post("/business-metrics")
def create_metric():
    b = _body()
    return jsonify({"metric": svc.create_metric(str(b.get("dataset_id", "")), b.get("name", ""),
                                                 b.get("expression", ""), b.get("description", ""))}), 201


@metric_bp.patch("/business-metrics/<metric_id>")
def update_metric(metric_id: str):
    b = _body()
    return jsonify({"metric": svc.update_metric(metric_id, b.get("name"), b.get("expression"), b.get("description"))})


@metric_bp.delete("/business-metrics/<metric_id>")
def delete_metric(metric_id: str):
    if not svc.delete_metric(metric_id):
        raise MetricError("That metric does not exist.")
    return jsonify({"status": "deleted", "id": metric_id})
