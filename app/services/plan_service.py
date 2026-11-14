"""Plan limits, tracked server-side. There is no billing here: a plan is just a label on the account
(users.plan: free | pro | business) that an operator sets, and a monthly analysis allowance read from config.
Enforcement is off unless ENFORCE_PLAN_LIMITS=true. A limit of 0 means unlimited."""

from __future__ import annotations

from datetime import datetime, timezone

from config import Config
from app.database.connection import app_store
from app.security.request_scope import owner_scope
from app.utils.exceptions import PlanLimitError

PLANS = ("free", "pro", "business")


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def limit_for(plan: str) -> int:
    return {"free": Config.PLAN_LIMIT_FREE, "pro": Config.PLAN_LIMIT_PRO, "business": Config.PLAN_LIMIT_BUSINESS}.get(plan, Config.PLAN_LIMIT_FREE)


def plan_of(owner: str | None) -> str:
    if owner is None:
        return "free"
    with app_store() as conn:
        row = conn.execute("SELECT plan FROM users WHERE id = ?", (owner,)).fetchone()
    return (row["plan"] if row and row["plan"] in PLANS else "free")


def used(owner: str | None) -> int:
    with app_store() as conn:
        row = conn.execute("SELECT analyses FROM usage_counters WHERE owner_id = ? AND month = ?", (owner or "-", _month())).fetchone()
    return int(row["analyses"]) if row else 0


def consume() -> None:
    """Count one analysis for the signed-in user; refuse when the plan's allowance is spent (if enforcement is on)."""
    owner = owner_scope()
    plan = plan_of(owner)
    limit = limit_for(plan)
    if Config.ENFORCE_PLAN_LIMITS and limit and used(owner) >= limit:
        raise PlanLimitError(f"You have used all {limit} analyses included in the {plan} plan this month.",
                             "plan limit reached")
    with app_store() as conn:
        conn.execute("""INSERT INTO usage_counters (owner_id, month, analyses) VALUES (?, ?, 1)
                        ON CONFLICT(owner_id, month) DO UPDATE SET analyses = analyses + 1""", (owner or "-", _month()))


def status() -> dict:
    owner = owner_scope()
    plan = plan_of(owner)
    limit = limit_for(plan)
    n = used(owner)
    return {"plan": plan, "analyses_used": n, "analyses_limit": limit or None,
            "analyses_remaining": (max(limit - n, 0) if limit else None), "enforced": Config.ENFORCE_PLAN_LIMITS}
