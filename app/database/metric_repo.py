"""Owner-scoped storage for semantic-layer metrics."""

from __future__ import annotations

from typing import Any

from app.database.connection import app_store
from app.database.repository import _AUTO, _and_owner, _owner
from app.utils.helpers import new_id, utcnow


def create(dataset_id: str, name: str, expression: str, description: str = "") -> dict:
    now = utcnow()
    row = {"id": new_id("met"), "owner_id": _owner(), "dataset_id": dataset_id, "name": name,
           "description": description, "expression": expression, "created_at": now, "updated_at": now}
    with app_store() as conn:
        conn.execute(
            """INSERT INTO semantic_metrics (id,owner_id,dataset_id,name,description,expression,created_at,updated_at)
               VALUES (:id,:owner_id,:dataset_id,:name,:description,:expression,:created_at,:updated_at)""", row)
    return get(row["id"], owner_id=row["owner_id"])


def get(metric_id: str, owner_id: Any = _AUTO) -> dict | None:
    extra, params = _and_owner(_owner(owner_id))
    with app_store() as conn:
        row = conn.execute(f"SELECT * FROM semantic_metrics WHERE id = ?{extra}", (metric_id, *params)).fetchone()
    return dict(row) if row else None


def list_for(dataset_id: str | None = None, owner_id: Any = _AUTO) -> list[dict]:
    extra, params = _and_owner(_owner(owner_id))
    sql, args = f"SELECT * FROM semantic_metrics WHERE 1=1{extra}", list(params)
    if dataset_id:
        sql += " AND dataset_id = ?"
        args.append(dataset_id)
    with app_store() as conn:
        rows = conn.execute(sql + " ORDER BY name", args).fetchall()
    return [dict(r) for r in rows]


def update(metric_id: str, **fields: Any) -> dict | None:
    fields["updated_at"] = utcnow()
    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    params = dict(fields, id=metric_id)
    owner = _owner()
    clause = ""
    if owner is not None:
        clause, params["owner"] = " AND owner_id = :owner", owner
    with app_store() as conn:
        conn.execute(f"UPDATE semantic_metrics SET {assignments} WHERE id = :id{clause}", params)
    return get(metric_id)


def delete(metric_id: str) -> bool:
    extra, params = _and_owner(_owner())
    with app_store() as conn:
        cur = conn.execute(f"DELETE FROM semantic_metrics WHERE id = ?{extra}", (metric_id, *params))
    return cur.rowcount > 0
