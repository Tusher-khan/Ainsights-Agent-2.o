"""Data-access layer for connected data sources.

Same isolation rule as the rest of the store: every query is filtered by the
signed-in owner (fail-closed through `owner_scope()`), so a source id supplied by a
client can never reach another user's connection. Secrets are stored as an encrypted
blob and are only returned by `get_with_secrets`, which exists for the connector layer.
"""

from __future__ import annotations

from typing import Any

from app.database.connection import app_store
from app.database.repository import _AUTO, _and_owner, _owner
from app.utils.helpers import dumps, loads, new_id, utcnow


def _row(row, with_secrets: bool = False) -> dict:
    data = dict(row)
    data["config"] = loads(data.pop("config_json", None), {}) or {}
    data["warnings"] = loads(data.pop("warnings_json", None), []) or []
    data["enabled"] = bool(data["enabled"])
    secrets = data.pop("secrets_enc", None)
    data["secret_keys"] = []  # filled by the service (needs decryption); names only
    if with_secrets:
        data["secrets_enc"] = secrets
    else:
        data["has_secrets"] = bool(secrets)
    return data


def create(name: str, kind: str, config: dict, secrets_enc: str | None) -> dict:
    now = utcnow()
    row = {
        "id": new_id("src"), "owner_id": _owner(), "name": name, "kind": kind,
        "config_json": dumps(config), "secrets_enc": secrets_enc, "status": "untested",
        "created_at": now, "updated_at": now,
    }
    with app_store() as conn:
        conn.execute(
            """INSERT INTO data_sources (id,owner_id,name,kind,config_json,secrets_enc,status,enabled,created_at,updated_at)
               VALUES (:id,:owner_id,:name,:kind,:config_json,:secrets_enc,:status,1,:created_at,:updated_at)""",
            row,
        )
    return get(row["id"], owner_id=row["owner_id"])


def get(source_id: str, owner_id: Any = _AUTO, with_secrets: bool = False) -> dict | None:
    extra, params = _and_owner(_owner(owner_id))
    with app_store() as conn:
        row = conn.execute(f"SELECT * FROM data_sources WHERE id = ?{extra}", (source_id, *params)).fetchone()
    return _row(row, with_secrets) if row else None


def list_all(owner_id: Any = _AUTO) -> list[dict]:
    owner = _owner(owner_id)
    with app_store() as conn:
        if owner is None:
            rows = conn.execute("SELECT * FROM data_sources ORDER BY created_at DESC").fetchall()
        else:
            rows = conn.execute("SELECT * FROM data_sources WHERE owner_id = ? ORDER BY created_at DESC", (owner,)).fetchall()
    return [_row(r) for r in rows]


def count(owner_id: Any = _AUTO) -> int:
    return len(list_all(owner_id))


def update(source_id: str, owner_id: Any = _AUTO, **fields: Any) -> dict | None:
    if not fields:
        return get(source_id, owner_id)
    if "config" in fields:
        fields["config_json"] = dumps(fields.pop("config"))
    if "warnings" in fields:
        fields["warnings_json"] = dumps(fields.pop("warnings"))
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    fields["updated_at"] = utcnow()
    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    params = dict(fields, id=source_id)
    owner = _owner(owner_id)
    clause = ""
    if owner is not None:
        clause = " AND owner_id = :owner"
        params["owner"] = owner
    with app_store() as conn:
        conn.execute(f"UPDATE data_sources SET {assignments} WHERE id = :id{clause}", params)
    return get(source_id, owner_id)


def delete(source_id: str, owner_id: Any = _AUTO) -> bool:
    extra, params = _and_owner(_owner(owner_id))
    with app_store() as conn:
        cur = conn.execute(f"DELETE FROM data_sources WHERE id = ?{extra}", (source_id, *params))
    return cur.rowcount > 0


def datasets_for(source_id: str, owner_id: Any = _AUTO) -> list[dict]:
    owner = _owner(owner_id)
    extra, params = _and_owner(owner)
    with app_store() as conn:
        rows = conn.execute(f"SELECT * FROM datasets WHERE connection_id = ?{extra} ORDER BY name", (source_id, *params)).fetchall()
    from app.database.repository import _dataset_row

    return [_dataset_row(r) for r in rows]
