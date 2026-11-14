"""Chat service — the entry point for every question.

Responsibilities: screen the question, pick a data source, run the workflow,
persist the turn and history, and shape the response the UI renders.
"""

from __future__ import annotations

from app.ai.agent_orchestrator import run_analysis, state_to_response
from app.core.datasource import DataSource
from app.database import repository
from app.security.prompt_security import screen_user_question
from app.services import dataset_service, memory_service
from app.utils.exceptions import AInsightError, DatasetNotFoundError, PromptInjectionError
from app.utils.helpers import Timer, truncate
from app.utils.logger import get_logger

log = get_logger("services.chat")

MAX_QUESTION_LENGTH = 1000


def _schema_answer(question: str, source: DataSource) -> dict:
    """Answer schema/dataset questions from metadata, with no computation."""
    profile = source.profile
    lines = [
        f"{source.name} has {source.row_count:,} rows and {len(source.columns)} columns.",
    ]
    if source.measures:
        lines.append(f"Numeric measures: {', '.join(source.measures)}.")
    if source.dimensions:
        lines.append(f"Categorical dimensions: {', '.join(source.dimensions[:10])}.")
    if source.date_columns:
        lines.append(f"Date columns: {', '.join(source.date_columns)}.")
    quality = profile.get("quality") or {}
    if quality:
        lines.append(
            f"Data quality: {quality.get('missing_percentage', 0)}% of cells are missing and "
            f"{quality.get('duplicate_rows', 0)} duplicate rows were found."
        )
    return {
        "answer": f"{source.name}: {source.row_count:,} rows × {len(source.columns)} columns",
        "insight": " ".join(lines),
        "intent": "schema_question",
        "columns_table": {
            "columns": ["Column", "Type", "Missing %", "Distinct"],
            "rows": [
                {
                    "Column": c["name"],
                    "Type": c["semantic_type"],
                    "Missing %": c["null_percentage"],
                    "Distinct": c["unique_count"],
                }
                for c in profile.get("columns", [])
            ],
        },
        "source": {
            "dataset": source.name,
            "dataset_id": source.id,
            "kind": source.kind,
            "operation": "metadata lookup (no computation required)",
            "columns_used": [],
        },
        "validation": {"verified": True, "result": {"valid": True, "checks": [
            {"name": "metadata_only", "passed": True, "detail": "answered from the stored dataset profile"}
        ], "errors": [], "warnings": []}},
        "suggestions": dataset_service.starter_questions(source.id),
    }


def ask(
    question: str,
    dataset_id: str | None = None,
    conversation_id: str | None = None,
    chart_history_id: str | None = None,
) -> dict:
    """Answer one question end to end.

    ``chart_history_id`` attaches the analysis behind a chart the user is asking about: it is
    resolved against the caller's own history and becomes the conversation's previous turn."""
    question = (question or "").strip()
    if not question:
        raise AInsightError("Type a question about your data to get started.")
    if len(question) > MAX_QUESTION_LENGTH:
        question = question[:MAX_QUESTION_LENGTH]

    allowed, reason = screen_user_question(question)
    if not allowed:
        raise PromptInjectionError(reason, "prompt injection screen triggered")

    from app.services import plan_service

    plan_service.consume()  # server-side plan allowance (enforced only when ENFORCE_PLAN_LIMITS=true)
    if chart_history_id:
        from app.services import chart_service

        # Resolve first: an unknown / foreign id fails before anything is stored.
        dataset_id = chart_service.describe(chart_history_id)["dataset_id"] or dataset_id
    conversation_id = repository.ensure_conversation(conversation_id, dataset_id)
    if chart_history_id:
        chart_service.prime_context(chart_history_id, conversation_id)
    repository.add_message(conversation_id, "user", question, {"dataset_id": dataset_id})

    from app.services import chart_service as _charts

    asked = question
    why = _charts.rewrite_why_leader(question, dataset_id, conversation_id)
    if why:
        question = why[0]  # the validated breakdown is what runs (and what history stores, so Refresh is self-contained)

    with Timer() as total:
        hybrid = _try_multi_source(question, conversation_id)
        if hybrid is not None:
            hybrid["total_ms"] = total.ms
            return hybrid
        agent_payload = _try_agent(question, dataset_id, conversation_id)
        if agent_payload is not None:
            agent_payload["total_ms"] = total.ms
            return agent_payload
        source, routing_reason = dataset_service.select_source(question, dataset_id, conversation_id)

        # Schema/dataset questions are answered from metadata, never computed.
        from app.ai.intent_classifier import classify_with_rules

        quick_intent = classify_with_rules(question).intent
        if quick_intent in {"schema_question", "dataset_question"}:
            payload = _schema_answer(question, source)
            payload.update(
                {
                    "conversation_id": conversation_id,
                    "routing_reason": routing_reason,
                    "stages": [
                        {"stage": "classify_intent", "status": "done", "detail": quick_intent},
                        {"stage": "retrieve_metadata", "status": "done", "detail": "dataset profile loaded"},
                        {"stage": "generate_response", "status": "done", "detail": "answered from metadata"},
                    ],
                }
            )
            repository.add_message(conversation_id, "assistant", payload["answer"], {"dataset_id": source.id})
            memory_service.remember_turn(conversation_id, question, {}, source.id, payload["answer"])
            payload["total_ms"] = 0.0
            payload["history_id"] = repository.add_history(
                {
                    "conversation_id": conversation_id,
                    "question": question,
                    "dataset_id": source.id,
                    "dataset_name": source.name,
                    "intent": quick_intent,
                    "engine": "metadata",
                    "status": "success",
                    "answer": payload["answer"],
                    "row_count": len(source.columns),
                }
            )
            return payload

        memory = memory_service.get_context(conversation_id)
        memory_text = memory_service.memory_text(conversation_id)

        try:
            state = run_analysis(question, source, conversation_id, memory, memory_text)
        except AInsightError as exc:
            repository.add_history(
                {
                    "conversation_id": conversation_id,
                    "question": question,
                    "dataset_id": source.id,
                    "dataset_name": source.name,
                    "status": "error",
                    "error": exc.user_message,
                    "engine": source.kind,
                }
            )
            repository.add_message(conversation_id, "assistant", exc.user_message, {"error": True})
            raise

        payload = state_to_response(state)

    payload["conversation_id"] = conversation_id
    payload["routing_reason"] = routing_reason
    payload["total_ms"] = total.ms
    payload["suggestions"] = _followups(state)
    if why:
        payload["interpreted_as"] = question
        payload["assumptions"] = [f"You asked “{asked}”. {why[1]}"] + list(payload.get("assumptions") or [])

    repository.add_message(
        conversation_id,
        "assistant",
        payload.get("answer", ""),
        {"dataset_id": source.id, "plan": payload.get("plan")},
    )
    payload["history_id"] = repository.add_history(
        {
            "conversation_id": conversation_id,
            "question": question,
            "dataset_id": source.id,
            "dataset_name": source.name,
            "intent": payload.get("intent"),
            "engine": (payload.get("result") or {}).get("engine", "pandas"),
            "sql": payload.get("sql"),
            "plan": payload.get("plan"),
            "status": "success",
            "row_count": (payload.get("result") or {}).get("row_count", 0),
            "execution_ms": (payload.get("result") or {}).get("execution_ms", 0),
            "total_ms": total.ms,
            "answer": truncate(payload.get("answer", ""), 300),
            "result": payload.get("result"),
            "response": payload,
        }
    )
    return payload


def _try_agent(question: str, dataset_id: str | None, conversation_id: str) -> dict | None:
    """Complex business questions (why did X change, anomalies, correlations, data quality, cross-source)
    run through the bounded agent loop. Simple questions return None and use the single-pass pipeline."""
    from app.agent import intents, response as agent_response

    intent = intents.classify(question)
    if intent.name == "forecasting":
        return _unsupported(question, conversation_id, intent.name,
                            "AInsight does not generate forecasts yet. It can compare your actuals with a forecast file you upload, "
                            "e.g. “Compare actual sales with our forecast”.")
    if intent.name == "dashboard_generation":
        return _dashboard_from_chat(question, dataset_id, conversation_id)
    if intent.name not in intents.AGENT_INTENTS:
        # a cross-source question can look like a plain trend/diagnostic question
        if intent.name not in {"trend", "comparison", "other", "kpi"}:
            return None
    cross = None if dataset_id else agent_response.find_cross_sources(question)
    if cross:
        sources, measures = cross
        intent = intents.BusinessIntent("cross_source", "complex", intent.method)
        routing = "combined two data sources: " + " + ".join(s.name for s in sources)
    elif intent.name in intents.AGENT_INTENTS:
        source, routing = dataset_service.select_source(question, dataset_id, conversation_id)
        sources, measures = [source], None
    else:
        return None
    state, payload = agent_response.run_for_question(question, intent, sources, cross_measures=measures)
    if state.status == "failed":
        # nothing usable came out of the loop (e.g. no date column): let the normal pipeline answer instead
        if intent.name in {"diagnostic", "root_cause"}:
            return None
        payload["answer"] = (state.errors[0] if state.errors else "The analysis could not be completed.")
    payload["conversation_id"] = conversation_id
    payload["routing_reason"] = routing
    payload["suggestions"] = _agent_followups(state)
    repository.add_message(conversation_id, "assistant", payload.get("answer", ""), {"dataset_id": sources[0].id, "agent": state.analysis_id})
    payload["history_id"] = repository.add_history({
        "conversation_id": conversation_id, "question": question, "dataset_id": sources[0].id, "dataset_name": sources[0].name,
        "intent": payload.get("intent"), "engine": "agent", "status": "success" if state.status != "failed" else "error",
        "row_count": (payload.get("result") or {}).get("row_count", 0), "execution_ms": payload["agent"]["budget"]["elapsed_ms"],
        "total_ms": payload["agent"]["budget"]["elapsed_ms"], "answer": truncate(payload.get("answer", ""), 300),
        "result": payload.get("result"), "response": payload, "analysis_id": state.analysis_id,
    })
    return payload


def _dashboard_from_chat(question: str, dataset_id: str | None, conversation_id: str) -> dict:
    from app.services import dashboard_service

    source, routing = dataset_service.select_source(question, dataset_id, conversation_id)
    dash = dashboard_service.create(source.id, None, next((m for m in source.measures if m.lower().replace("_", " ") in question.lower()), None))
    cfg = dash["config"]
    msg = (f"I created the dashboard “{dash['name']}” from {source.name}: {len(cfg['kpis'])} key numbers, "
           f"{len(cfg['charts'])} charts and {len(cfg['filters'])} filters, chosen from the dataset's columns. Values are calculated from your data when you open it.")
    repository.add_message(conversation_id, "assistant", msg, {"dashboard": dash["id"]})
    payload = {"answer": msg, "insight": msg, "intent": "dashboard_generation", "terminal": True, "conversation_id": conversation_id,
               "stages": [{"stage": "classify_intent", "status": "done", "detail": "dashboard_generation"},
                          {"stage": "build_dashboard", "status": "done", "detail": f"{len(cfg['charts'])} charts"}],
               "validation": {"verified": True, "result": {"valid": True, "checks": [], "errors": [], "warnings": []}},
               "dashboard": {"id": dash["id"], "name": dash["name"], "url": f"/dashboards/{dash['id']}"},
               "routing_reason": routing, "suggestions": dataset_service.starter_questions(source.id)[:3]}
    payload["history_id"] = repository.add_history({
        "conversation_id": conversation_id, "question": question, "dataset_id": source.id, "dataset_name": source.name,
        "intent": "dashboard_generation", "engine": "dashboard", "status": "success", "row_count": 0, "execution_ms": 0, "total_ms": 0,
        "answer": truncate(msg, 300), "result": None, "response": payload})
    return payload


def _unsupported(question: str, conversation_id: str, intent: str, message: str) -> dict:
    repository.add_message(conversation_id, "assistant", message, {"unsupported": intent})
    return {"answer": message, "insight": message, "intent": intent, "terminal": True, "conversation_id": conversation_id,
            "stages": [{"stage": "classify_intent", "status": "done", "detail": intent}],
            "validation": {"verified": True, "result": {"valid": True, "checks": [], "errors": [], "warnings": []}},
            "suggestions": ["Compare actual sales with our forecast", "Show monthly sales"], "agent": {"mode": "none", "status": "unsupported"}}


def _agent_followups(state) -> list[str]:
    ctx = state.context
    out = []
    chain = [f for f in state.findings if f["kind"] in {"driver", "drill"}]
    if chain:
        seg = chain[-1]["subject"].get("segment")
        if seg:
            out.append(f"Show the top 5 products in {seg}")
    if ctx.get("measure"):
        out.append(f"Show monthly {ctx['measure']} trends")
    out.append("Create a dashboard from this analysis") if False else None
    return [q for q in out if q][:4] or dataset_service.starter_questions(state.selected_sources[0]["id"])


def _try_multi_source(question: str, conversation_id: str) -> dict | None:
    """Cross-source questions (actual vs forecast/target/budget). Returns None when the question
    is not one, or when no dataset looks like a reference — the normal single-source flow then runs."""
    from app.agent import multi_source

    if not multi_source.detect(question):
        return None
    try:
        payload = multi_source.run(question)
    except multi_source.ComparisonError as exc:
        if "No dataset looks like" in exc.user_message or "at least two" in exc.user_message:
            return None
        repository.add_message(conversation_id, "assistant", exc.user_message, {"error": True})
        raise
    payload["conversation_id"] = conversation_id
    payload["routing_reason"] = "compared two data sources: " + " + ".join(s["dataset"] for s in payload["sources_used"])
    payload["suggestions"] = [
        f"Why is {payload['result']['rows'][0][payload['result']['columns'][0]]} different?",
        "Show only the periods below target",
    ]
    repository.add_message(conversation_id, "assistant", payload["answer"], {"multi_source": True})
    payload["history_id"] = repository.add_history({
        "conversation_id": conversation_id, "question": question, "dataset_id": payload["sources_used"][0]["dataset_id"],
        "dataset_name": payload["source"]["dataset"], "intent": payload["intent"], "engine": "hybrid", "sql": payload.get("sql"),
        "status": "success", "row_count": payload["result"]["row_count"], "execution_ms": payload["result"]["execution_ms"],
        "total_ms": payload["result"]["execution_ms"], "answer": truncate(payload["answer"], 300), "result": payload["result"],
        "response": payload,
    })
    return payload


def _followups(state) -> list[str]:
    """Suggest next questions from the plan that just ran."""
    if getattr(state, "investigation", None):
        inv = state.investigation
        out = []
        if inv["drivers"]:
            dim = next(iter(inv["drivers"]))
            out.append(f"Show {inv['measure']} by {dim}")
        out.append(f"Show monthly {inv['measure']} trends")
        if inv["top_drivers"]:
            lead = inv["top_drivers"][0]
            out.append(f"Why did {lead['segment']} change?")
        return out[:4]
    plan = state.plan
    if not plan:
        return dataset_service.starter_questions(state.source.id)
    try:
        from app.ai import narrative

        smart = narrative.next_analyses(state.result, plan, state.source) if state.result else []
    except Exception:  # noqa: BLE001
        smart = []
    if smart:
        return smart
    suggestions = []
    if plan.dimensions and plan.measures:
        suggestions.append(f"Show only the top 5 {plan.dimensions[0]}")
        suggestions.append(f"Show this as a chart")
    if state.source.date_columns and not plan.time_column:
        suggestions.append(f"Show monthly {plan.measures[0] if plan.measures else 'totals'} trends")
    other_measures = [m for m in state.source.measures if m not in plan.measures]
    if other_measures:
        suggestions.append(f"Do the same for {other_measures[0]}")
    return suggestions[:4] or dataset_service.starter_questions(state.source.id)


def conversation_messages(conversation_id: str) -> list[dict]:
    return repository.get_messages(conversation_id, limit=50)  # empty unless it is yours


def reset_conversation(conversation_id: str) -> None:
    if not repository.conversation_owned(conversation_id):
        raise AInsightError("That conversation is not available.")
    memory_service.clear(conversation_id)
