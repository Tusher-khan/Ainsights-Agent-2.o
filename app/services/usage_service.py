"""Model usage and cost tracking.

Records tokens, model, tier, purpose and an *estimated* cost per model call. Prompts and
responses are never stored. Prices come only from LLM_PRICES_JSON; a model without a
configured price is recorded with tokens only, so no cost is ever invented.
"""

from __future__ import annotations

import contextvars
import json
from datetime import datetime, timezone

from config import Config
from app.database.connection import app_store
from app.security.request_scope import current_user
from app.utils.helpers import new_id, utcnow
from app.utils.logger import get_logger

log = get_logger("usage")

_analysis_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("analysis_id", default=None)
_step: contextvars.ContextVar[int | None] = contextvars.ContextVar("analysis_step", default=None)


def set_step(n: int | None):
    return _step.set(n)


def set_analysis(analysis_id: str | None):
    return _analysis_id.set(analysis_id)


def reset_analysis(token) -> None:
    _analysis_id.reset(token)


def current_analysis() -> str | None:
    return _analysis_id.get()


def _tokens(provider: str, usage: dict) -> tuple[int | None, int | None]:
    usage = usage or {}
    pairs = (("prompt_tokens", "completion_tokens"), ("input_tokens", "output_tokens"),
             ("promptTokenCount", "candidatesTokenCount"))
    for a, b in pairs:
        if a in usage or b in usage:
            return int(usage.get(a) or 0), int(usage.get(b) or 0)
    return None, None


def _prices() -> dict:
    try:
        data = json.loads(Config.LLM_PRICES_JSON) if Config.LLM_PRICES_JSON else {}
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def estimate_cost(model: str, tokens_in: int | None, tokens_out: int | None) -> float | None:
    price = _prices().get(model)
    if not price or tokens_in is None or tokens_out is None:
        return None
    return round((tokens_in * float(price.get("input", 0)) + tokens_out * float(price.get("output", 0))) / 1_000_000, 6)


def record(provider: str, model: str, tier: str, purpose: str, usage: dict, ms: float, ok: bool = True) -> dict:
    """Best effort: usage accounting must never break an analysis."""
    tin, tout = _tokens(provider, usage)
    cost = estimate_cost(model, tin, tout)
    entry = {"provider": provider, "model": model, "tier": tier, "purpose": purpose,
             "input_tokens": tin, "output_tokens": tout, "est_cost_usd": cost,
             "usage": "reported" if tin is not None else "unavailable"}  # never invent token counts
    try:
        user = current_user()
        if Config.AUTH_REQUIRED and not user:
            return entry
        with app_store() as conn:
            conn.execute(
                "INSERT INTO llm_usage (id,owner_id,analysis_id,provider,model,tier,purpose,step,input_tokens,output_tokens,"
                "est_cost_usd,ms,ok,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("use"), user["id"] if user else None, current_analysis(), provider, model, tier, purpose, _step.get(),
                 tin, tout, cost, round(ms, 1), 1 if ok else 0, utcnow()),
            )
    except Exception as exc:
        log.warning("Could not record model usage: %s", exc)
    return entry


def _month_start() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-01")


def summary() -> dict:
    """Usage for the signed-in user this month."""
    from app.security.request_scope import owner_scope

    owner = owner_scope()
    where, params = ("AND owner_id = ?", (owner,)) if owner is not None else ("", ())
    with app_store() as conn:
        rows = conn.execute(
            f"SELECT model, tier, COUNT(*) calls, COALESCE(SUM(input_tokens),0) tin, COALESCE(SUM(output_tokens),0) tout, "
            f"SUM(est_cost_usd) cost FROM llm_usage WHERE created_at >= ? {where} GROUP BY model, tier",
            (_month_start(), *params),
        ).fetchall()
    models = [{"model": r["model"], "tier": r["tier"], "calls": r["calls"], "input_tokens": r["tin"],
               "output_tokens": r["tout"], "est_cost_usd": r["cost"]} for r in rows]
    priced = [m["est_cost_usd"] for m in models if m["est_cost_usd"] is not None]
    return {
        "month_start": _month_start(),
        "models": models,
        "total_calls": sum(m["calls"] for m in models),
        "total_input_tokens": sum(m["input_tokens"] for m in models),
        "total_output_tokens": sum(m["output_tokens"] for m in models),
        "est_cost_usd": round(sum(priced), 6) if priced else None,
        "cost_note": "Estimated from LLM_PRICES_JSON; models without a configured price show tokens only.",
    }


def analysis_usage(analysis_id: str) -> dict:
    from app.security.request_scope import owner_scope

    owner = owner_scope()
    where, params = ("AND owner_id = ?", (owner,)) if owner is not None else ("", ())
    with app_store() as conn:
        r = conn.execute(
            f"SELECT COUNT(*) calls, COALESCE(SUM(input_tokens),0) tin, COALESCE(SUM(output_tokens),0) tout, "
            f"SUM(est_cost_usd) cost, SUM(CASE WHEN input_tokens IS NULL THEN 1 ELSE 0 END) nousage "
            f"FROM llm_usage WHERE analysis_id = ? {where}", (analysis_id, *params),
        ).fetchone()
    return {"calls": r["calls"], "input_tokens": r["tin"], "output_tokens": r["tout"], "est_cost_usd": r["cost"],
            "calls_without_usage": r["nousage"] or 0,
            "usage": "unavailable" if r["calls"] and r["nousage"] == r["calls"] else "reported" if r["calls"] else "none"}
