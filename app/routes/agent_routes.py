"""Agent tool endpoints: let desktop / mobile / API clients (or an LLM tool loop) call the
same authorized tools the web app uses."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.agent import tools
from app.utils.exceptions import UnsupportedRequestError

agent_bp = Blueprint("agent", __name__, url_prefix="/api")


@agent_bp.get("/agent/tools")
def list_tools():
    return jsonify({"tools": tools.tool_specs()})


@agent_bp.post("/agent/tools/<name>")
def call_tool(name: str):
    body = request.get_json(silent=True)
    if body is not None and not isinstance(body, dict):
        raise UnsupportedRequestError("The request was not valid.")
    return jsonify({"tool": name, "result": tools.call(name, body or {}, analysis_id=request.headers.get("X-Analysis-Id"))})


@agent_bp.get("/agent/events")
def events():
    return jsonify({"events": tools.recent_events(min(request.args.get("limit", 100, type=int), 500))})
