"""Role → permission model used by the agent tool registry (and later by organizations).

Today every account is the sole "owner" of its own data. The structure exists so that
organization members (owner / admin / analyst / viewer) can be enforced with the same
checks the moment organizations are switched on.
"""

from __future__ import annotations

from app.security.request_scope import current_user

READ = "read:datasets"
METRICS = "read:metrics"
ANALYZE = "run:analysis"
SQL = "query:sql"
REST = "call:rest"
SOURCES = "read:connections"

ALL = {READ, METRICS, ANALYZE, SQL, REST, SOURCES}

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "owner": set(ALL),
    "admin": set(ALL),
    "analyst": {READ, METRICS, ANALYZE, SQL, REST, SOURCES},
    # Viewers can read saved results and run built-in analyses, but not run raw SQL or call external APIs.
    "viewer": {READ, METRICS, ANALYZE},
}


def current_role() -> str:
    user = current_user()
    return (user or {}).get("role") or "owner"


def permissions_for(role: str | None = None) -> set[str]:
    return ROLE_PERMISSIONS.get(role or current_role(), set())
