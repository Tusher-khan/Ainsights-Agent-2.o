"""Saved analyses: reopen (no recompute), refresh (recompute on current data), rename, favorite, delete, search."""

from __future__ import annotations

from app.database import repository
from app.utils.exceptions import AInsightError
from app.utils.helpers import utcnow


def reopen(history_id: str) -> dict:
    """The saved analysis exactly as it was produced. Never re-runs anything."""
    saved = repository.get_saved_response(history_id)
    if not saved:
        raise AInsightError("That analysis is no longer in your history.")
    saved["data_last_analyzed"] = saved["refreshed_at"] or saved["created_at"]
    saved["replayed"] = True
    if saved["response"] is None:
        saved["notice"] = "This analysis was saved before full results were stored, so only its summary is available. Use Refresh to run it again."
    return saved


def refresh(history_id: str) -> dict:
    """Run the saved question again against the current data and replace the stored result."""
    from app.services import chat_service

    saved = repository.get_saved_response(history_id)
    if not saved:
        raise AInsightError("That analysis is no longer in your history.")
    payload = chat_service.ask(saved["question"], saved.get("dataset_id"), None)
    new_id = payload.get("history_id")
    agent = payload.get("agent") or {}
    repository.update_history(history_id, response=payload, answer=payload.get("answer"), analysis_id=agent.get("analysis_id"))
    if new_id and new_id != history_id:
        repository.delete_history(new_id)  # the refresh replaces the old result; it does not add a second entry
    payload["history_id"] = history_id
    payload["data_last_analyzed"] = utcnow()
    payload["refreshed"] = True
    return payload


def update(history_id: str, title=None, favorite=None) -> dict:
    if title is not None and not str(title).strip():
        raise AInsightError("A name cannot be empty.")
    if not repository.update_history(history_id, title=title, favorite=favorite):
        raise AInsightError("That analysis is no longer in your history.")
    return {"id": history_id, "title": title, "favorite": favorite}
