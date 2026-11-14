"""Chart interaction support: "Ask AInsight about this chart" and drill-down.

Nothing here computes a business number or runs SQL from the browser. Both features
only help the user *ask a question*, and the question then goes through the normal
validated pipeline (planner -> allow-listed tools -> validation):

* ``prime_context`` makes a saved analysis (owned by the caller) the conversation's
  previous turn, so a follow-up such as "why did February fall?" resolves against the
  chart's dataset, measure, dimension and filters.
* ``drilldown`` proposes follow-up questions for one clicked category. The category
  must be a value that really appears in that analysis' stored result, so a forged
  value cannot be injected, and the questions use only columns of the dataset.
"""

from __future__ import annotations

from app.database import repository
from app.services import dataset_service, memory_service
from app.utils.exceptions import AInsightError
from app.utils.logger import get_logger

log = get_logger("services.chart")

MAX_LABEL = 120


def _entry(history_id: str) -> dict:
    entry = repository.get_history_entry(str(history_id or ""))  # owner-scoped: another user's id is simply "not found"
    if not entry:
        raise AInsightError("That chart's analysis is no longer in your history.")
    return entry


def describe(history_id: str) -> dict:
    """Plain facts about the analysis behind a chart (no data rows)."""
    entry = _entry(history_id)
    plan = entry.get("plan") or {}
    return {
        "history_id": entry["id"],
        "question": entry.get("question"),
        "dataset_id": entry.get("dataset_id"),
        "dataset_name": entry.get("dataset_name"),
        "measures": plan.get("measures") or [],
        "dimensions": plan.get("dimensions") or [],
        "filters": plan.get("filters") or [],
        "aggregation": plan.get("aggregation"),
    }


def prime_context(history_id: str, conversation_id: str) -> str | None:
    """Make the chart's analysis the 'previous turn' of this conversation. Returns its dataset id."""
    entry = _entry(history_id)
    plan = entry.get("plan") or {}
    dataset_id = entry.get("dataset_id")
    memory_service.remember_turn(
        conversation_id,
        entry.get("question") or "",
        plan,
        dataset_id,
        (entry.get("answer") or "")[:300],
    )
    return dataset_id


def _first_column(result: dict, name: str) -> str | None:
    for column in result.get("columns") or []:
        if str(column).lower() == str(name).lower():
            return column
    return None


def drilldown(history_id: str, label: str) -> dict:
    label = str(label or "").strip()[:MAX_LABEL]
    if not label:
        raise AInsightError("Choose a chart element first.")
    entry = _entry(history_id)
    plan = entry.get("plan") or {}
    dims = plan.get("dimensions") or []
    measures = plan.get("measures") or []
    if not dims or not measures:
        return {"label": label, "questions": [], "note": "This chart is not a category breakdown, so there is nothing to drill into."}

    dimension, measure = dims[0], measures[0]
    result = entry.get("result") or {}
    column = _first_column(result, dimension)
    known = {str(row.get(column)) for row in (result.get("rows") or [])} if column else set()
    if label not in known:
        raise AInsightError("That value is not part of this chart's result.")

    try:
        source = dataset_service.get_source(entry["dataset_id"])
    except Exception:  # noqa: BLE001 - the dataset may have been deleted since
        return {"label": label, "questions": [], "note": "The dataset behind this chart is no longer available."}

    already = {d.lower() for d in dims}
    others = [d for d in source.dimensions if d.lower() not in already][:3]
    questions = [f"Show {measure} by {d} where {dimension} is {label}" for d in others]
    if source.date_columns:
        questions.append(f"Show monthly {measure} trend where {dimension} is {label}")
    return {
        "label": label,
        "dimension": dimension,
        "measure": measure,
        "questions": questions[:4],
        "note": "These are follow-up questions, not explanations: they show how the figure breaks down, not why it is what it is.",
    }


# --------------------------------------------------------------------------- "why is X ahead?"

import re  # noqa: E402

_WHY_LEADER = re.compile(
    r"\bwhy\b.{0,40}\b(ahead|leading|leads?|top|best|highest|higher|strongest|outperform\w*|better|winning|wins?|lower|lowest|behind|lagging|weakest|worse|underperform\w*|worst)\b", re.I)
_TIMEISH = re.compile(r"\b(month|week|year|quarter|day|daily|last|previous|since|fell|fall|drop\w*|declin\w*|decreas\w*|increas\w*|grew|grow\w*)\b", re.I)


def rewrite_why_leader(question: str, dataset_id: str | None, conversation_id: str) -> tuple[str, str] | None:
    """"Why is West ahead?" cannot be answered causally from a table of totals. What the data *can* show is what
    West's total is made up of. Turn the question into that validated breakdown and say so plainly.

    Returns (rewritten_question, note) or None when the question is not of this form. Never invents a value:
    the named category must match a real value of a column in the dataset."""
    if not _WHY_LEADER.search(question or "") or _TIMEISH.search(question or ""):
        return None
    previous = (memory_service.get_context(conversation_id) or {}).get("last_plan") or {}
    measures = previous.get("measures") or []
    dims = previous.get("dimensions") or []
    dataset_id = dataset_id or memory_service.active_dataset(conversation_id)
    if not (measures and dims and dataset_id):
        return None
    try:
        from app.ai.query_planner import _detect_filters  # literal match against the dataset's real category values

        source = dataset_service.get_source(dataset_id)
        found = [f for f in _detect_filters(question, source) if f.operator == "="]
    except Exception:  # noqa: BLE001
        return None
    if not found:
        return None
    pinned = found[0]
    others = [d for d in source.dimensions if d.lower() != str(pinned.column).lower()]
    if not others:
        return None

    def distance(name: str) -> int:
        profile = source.column_profile(name)
        return abs((profile or {}).get("unique_count", 999) - 12)

    dimension = min(others, key=distance)
    rewritten = f"Show {measures[0]} by {dimension} where {pinned.column} is {pinned.value}"
    note = (
        f"Your data can show what {pinned.value}'s {measures[0]} is made up of, not prove why it is ahead. "
        f"This breaks it down by {dimension}; compare it with other {pinned.column} values before drawing conclusions."
    )
    return rewritten, note
