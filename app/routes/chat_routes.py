"""Chat and conversation endpoints."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.services import chat_service, dataset_service, progress
from app.utils.logger import get_logger

chat_bp = Blueprint("chat", __name__, url_prefix="/api")
log = get_logger("routes.chat")


@chat_bp.post("/chat")
def post_chat():
    payload = request.get_json(silent=True) or {}
    response = chat_service.ask(
        question=payload.get("question", ""),
        dataset_id=payload.get("dataset_id") or None,
        conversation_id=payload.get("conversation_id") or None,
        chart_history_id=(payload.get("chart_context") or {}).get("history_id") or None,
    )
    return jsonify(response), 200


@chat_bp.post("/chat/async")
def post_chat_async():
    """Start an analysis in the background; poll /chat/jobs/<id> for the stages that really happened."""
    payload = request.get_json(silent=True) or {}
    args = dict(question=payload.get("question", ""), dataset_id=payload.get("dataset_id") or None,
                conversation_id=payload.get("conversation_id") or None,
                chart_history_id=(payload.get("chart_context") or {}).get("history_id") or None)
    return jsonify({"job_id": progress.start(chat_service.ask, **args)}), 202


@chat_bp.get("/chat/jobs/<job_id>")
def get_chat_job(job_id: str):
    return jsonify(progress.get(job_id, since=request.args.get("since", 0, type=int))), 200


@chat_bp.get("/chat/suggestions")
def get_suggestions():
    dataset_id = request.args.get("dataset_id")
    return jsonify({"suggestions": dataset_service.starter_questions(dataset_id)})


@chat_bp.get("/conversations/<conversation_id>")
def get_conversation(conversation_id: str):
    return jsonify({"messages": chat_service.conversation_messages(conversation_id)})


@chat_bp.delete("/conversations/<conversation_id>")
def reset_conversation(conversation_id: str):
    chat_service.reset_conversation(conversation_id)
    return jsonify({"status": "cleared", "conversation_id": conversation_id})


@chat_bp.get("/chart/<history_id>/drilldown")
def chart_drilldown(history_id: str):
    """Follow-up questions for one clicked chart category (validated against the stored result)."""
    from app.services import chart_service

    return jsonify(chart_service.drilldown(history_id, request.args.get("label", ""))), 200
