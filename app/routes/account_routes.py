"""Usage, plan and organization endpoints."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.services import org_service, plan_service, usage_service

account_bp = Blueprint("account", __name__, url_prefix="/api")


@account_bp.get("/usage")
def usage():
    return jsonify({"plan": plan_service.status(), "llm": usage_service.summary()})


@account_bp.get("/orgs")
def my_orgs():
    return jsonify({"organizations": org_service.mine()})


@account_bp.post("/orgs")
def create_org():
    return jsonify(org_service.create((request.get_json(silent=True) or {}).get("name", ""))), 201


@account_bp.get("/orgs/<org_id>/members")
def org_members(org_id: str):
    return jsonify({"members": org_service.members(org_id)})


@account_bp.put("/orgs/<org_id>/members")
def set_member(org_id: str):
    b = request.get_json(silent=True) or {}
    return jsonify(org_service.set_member(org_id, b.get("email", ""), b.get("role", "")))
