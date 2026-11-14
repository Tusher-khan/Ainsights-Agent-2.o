"""Owner-scoped storage for dashboard configurations (configuration only, never data rows)."""

from __future__ import annotations

import json
from typing import Any

from app.database.connection import app_store
from app.database.repository import _AUTO, _and_owner, _owner
from app.utils.helpers import new_id, utcnow


def _out(row) -> dict | None:
    if not row:
        return None
    item = dict(row)
    item["config"] = json.loads(item.pop("config_json") or "{}")
    return item


def create(name: str, dataset_id: str, config: dict) -> dict:
    now = utcnow()
    row = {"id": new_id("dash"), "owner_id": _owner(), "name": name, "dataset_id": dataset_id,
           "config_json": json.dumps(config), "created_at": now, "updated_at": now}
    with app_store() as conn:
        conn.execute("""INSERT INTO dashboards (id,owner_id,name,dataset_id,config_json,created_at,updated_at)
                        VALUES (:id,:owner_id,:name,:dataset_id,:config_json,:created_at,:updated_at)""", row)
    return get(row["id"], owner_id=row["owner_id"])


def get(dashboard_id: str, owner_id: Any = _AUTO) -> dict | None:
    extra, params = _and_owner(_owner(owner_id))
    with app_store() as conn:
        return _out(conn.execute(f"SELECT * FROM dashboards WHERE id = ?{extra}", (dashboard_id, *params)).fetchone())


def list_all() -> list[dict]:
    extra, params = _and_owner(_owner())
    with app_store() as conn:
        rows = conn.execute(f"SELECT * FROM dashboards WHERE 1=1{extra} ORDER BY updated_at DESC", params).fetchall()
    return [_out(r) for r in rows]


def update(dashboard_id: str, name: str | None = None, config: dict | None = None) -> bool:
    cur = get(dashboard_id)
    if not cur:
        return False
    extra, params = _and_owner(_owner())
    with app_store() as conn:
        conn.execute(f"UPDATE dashboards SET name=?, config_json=?, updated_at=? WHERE id=?{extra}",
                     (name or cur["name"], json.dumps(config if config is not None else cur["config"]), utcnow(), dashboard_id, *params))
    return True


def delete(dashboard_id: str) -> bool:
    extra, params = _and_owner(_owner())
    with app_store() as conn:
        n = conn.execute(f"DELETE FROM dashboards WHERE id = ?{extra}", (dashboard_id, *params)).rowcount
    return n > 0
