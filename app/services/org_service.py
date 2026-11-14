"""Organizations and roles (scaffolding). Stores organizations, memberships and roles with validated
role changes. It does NOT yet share datasets between members: data is still private to the account
that uploaded it, so no role here can widen anyone's data access."""

from __future__ import annotations

from app.database.connection import app_store
from app.security.permissions import ROLE_PERMISSIONS
from app.security.request_scope import current_user, owner_scope
from app.utils.exceptions import AInsightError, AuthRequiredError, PermissionDeniedError
from app.utils.helpers import new_id, utcnow

ROLES = tuple(ROLE_PERMISSIONS)  # owner, admin, analyst, viewer


def _me() -> str:
    uid = owner_scope()
    if not uid:
        raise AuthRequiredError()
    return uid


def _role(conn, org_id: str, user_id: str) -> str | None:
    row = conn.execute("SELECT role FROM org_members WHERE org_id=? AND user_id=?", (org_id, user_id)).fetchone()
    return row["role"] if row else None


def create(name: str) -> dict:
    uid = _me()
    name = (name or "").strip()
    if not 2 <= len(name) <= 80:
        raise AInsightError("Give the organization a name of 2–80 characters.")
    org_id, now = new_id("org"), utcnow()
    with app_store() as conn:
        conn.execute("INSERT INTO organizations (id,name,created_by,created_at) VALUES (?,?,?,?)", (org_id, name, uid, now))
        conn.execute("INSERT INTO org_members (org_id,user_id,role,created_at) VALUES (?,?,?,?)", (org_id, uid, "owner", now))
    return {"id": org_id, "name": name, "role": "owner"}


def mine() -> list[dict]:
    uid = _me()
    with app_store() as conn:
        rows = conn.execute("""SELECT o.id, o.name, m.role FROM organizations o JOIN org_members m ON m.org_id=o.id
                               WHERE m.user_id=? ORDER BY o.name""", (uid,)).fetchall()
    return [dict(r) for r in rows]


def members(org_id: str) -> list[dict]:
    uid = _me()
    with app_store() as conn:
        if _role(conn, org_id, uid) is None:
            raise PermissionDeniedError()  # non-members cannot even tell whether the org exists
        rows = conn.execute("""SELECT u.id, u.email, u.name, m.role FROM org_members m JOIN users u ON u.id=m.user_id
                               WHERE m.org_id=? ORDER BY u.email""", (org_id,)).fetchall()
    return [dict(r) for r in rows]


def set_member(org_id: str, email: str, role: str) -> dict:
    uid = _me()
    if role not in ROLES:
        raise AInsightError(f"Role must be one of: {', '.join(ROLES)}.")
    with app_store() as conn:
        mine_role = _role(conn, org_id, uid)
        if mine_role not in {"owner", "admin"}:
            raise PermissionDeniedError()
        if role in {"owner", "admin"} and mine_role != "owner":
            raise PermissionDeniedError()  # only an owner may grant admin/owner
        target = conn.execute("SELECT id FROM users WHERE email = ?", ((email or "").strip().lower(),)).fetchone()
        if not target:
            raise AInsightError("No account with that email exists yet.")
        current = _role(conn, org_id, target["id"])
        if current == "owner" and mine_role != "owner":
            raise PermissionDeniedError()
        if current == "owner" and role != "owner":
            owners = conn.execute("SELECT COUNT(*) c FROM org_members WHERE org_id=? AND role='owner'", (org_id,)).fetchone()["c"]
            if owners <= 1:
                raise AInsightError("An organization needs at least one owner.")
        conn.execute("""INSERT INTO org_members (org_id,user_id,role,created_at) VALUES (?,?,?,?)
                        ON CONFLICT(org_id,user_id) DO UPDATE SET role=excluded.role""", (org_id, target["id"], role, utcnow()))
    return {"org_id": org_id, "email": email, "role": role}
