"""Data-access layer for the application store.

Routes and services never write SQL against the store directly; they call these
functions, which keeps the storage engine swappable.
"""

from __future__ import annotations

from typing import Any

from app.database.connection import app_store
from app.security.request_scope import owner_scope
from app.utils.exceptions import AInsightError
from app.utils.helpers import dumps, loads, new_id, utcnow

# Every function below that touches user-owned rows filters by owner. The owner
# comes from the signed-in request (see app/security/request_scope.py), so callers
# cannot forget to pass it; `owner_id=` exists only for account deletion, which acts
# on behalf of a specific user. When the owner is None (AUTH_REQUIRED off) there is
# no filtering, which is the legacy single-user behaviour.
_AUTO = object()


def _owner(owner_id: Any = _AUTO) -> str | None:
    return owner_scope() if owner_id is _AUTO else owner_id


def _and_owner(owner: str | None, column: str = "owner_id") -> tuple[str, tuple]:
    return (f" AND {column} = ?", (owner,)) if owner is not None else ("", ())


# --------------------------------------------------------------------------
# Datasets
# --------------------------------------------------------------------------


def create_dataset(record: dict) -> dict:
    now = utcnow()
    row = {
        "owner_id": _owner(),
        "id": record.get("id") or new_id("ds"),
        "name": record["name"],
        "slug": record["slug"],
        "source_type": record["source_type"],
        "file_path": record.get("file_path"),
        "sheet_name": record.get("sheet_name"),
        "table_name": record.get("table_name"),
        "connection_id": record.get("connection_id"),
        "row_count": int(record.get("row_count", 0)),
        "column_count": int(record.get("column_count", 0)),
        "size_bytes": int(record.get("size_bytes", 0)),
        "status": record.get("status", "ready"),
        "profile_json": dumps(record.get("profile", {})),
        "created_at": now,
        "updated_at": now,
    }
    with app_store() as conn:
        conn.execute(
            """INSERT INTO datasets (id,owner_id,name,slug,source_type,file_path,sheet_name,table_name,connection_id,
                 row_count,column_count,size_bytes,status,profile_json,created_at,updated_at)
               VALUES (:id,:owner_id,:name,:slug,:source_type,:file_path,:sheet_name,:table_name,:connection_id,
                 :row_count,:column_count,:size_bytes,:status,:profile_json,:created_at,:updated_at)""",
            row,
        )
    return get_dataset(row["id"], owner_id=row["owner_id"])


def update_dataset(dataset_id: str, **fields: Any) -> dict | None:
    if not fields:
        return get_dataset(dataset_id)
    if "profile" in fields:
        fields["profile_json"] = dumps(fields.pop("profile"))
    fields["updated_at"] = utcnow()
    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    fields["id"] = dataset_id
    owner = _owner()
    clause = ""
    if owner is not None:
        clause = " AND owner_id = :owner"
        fields["owner"] = owner
    with app_store() as conn:
        conn.execute(f"UPDATE datasets SET {assignments} WHERE id = :id{clause}", fields)
    return get_dataset(dataset_id)


def _dataset_row(row) -> dict:
    data = dict(row)
    data["profile"] = loads(data.pop("profile_json", None), {}) or {}
    return data


def get_dataset(dataset_id: str, owner_id: Any = _AUTO) -> dict | None:
    extra, params = _and_owner(_owner(owner_id))
    with app_store() as conn:
        row = conn.execute(f"SELECT * FROM datasets WHERE id = ?{extra}", (dataset_id, *params)).fetchone()
    return _dataset_row(row) if row else None


def find_dataset_by_name(name: str) -> dict | None:
    extra, params = _and_owner(_owner())
    with app_store() as conn:
        row = conn.execute(
            f"SELECT * FROM datasets WHERE (lower(name) = lower(?) OR slug = lower(?)){extra} LIMIT 1",
            (name, name, *params),
        ).fetchone()
    return _dataset_row(row) if row else None


def list_datasets(owner_id: Any = _AUTO) -> list[dict]:
    owner = _owner(owner_id)
    with app_store() as conn:
        if owner is None:
            rows = conn.execute("SELECT * FROM datasets ORDER BY created_at DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM datasets WHERE owner_id = ? ORDER BY created_at DESC", (owner,)
            ).fetchall()
    return [_dataset_row(r) for r in rows]


def delete_dataset(dataset_id: str, owner_id: Any = _AUTO) -> bool:
    extra, params = _and_owner(_owner(owner_id))
    with app_store() as conn:
        cur = conn.execute(f"DELETE FROM datasets WHERE id = ?{extra}", (dataset_id, *params))
    return cur.rowcount > 0


# --------------------------------------------------------------------------
# Conversations and messages
# --------------------------------------------------------------------------


def create_conversation(dataset_id: str | None = None, title: str = "New analysis") -> dict:
    now = utcnow()
    cid = new_id("conv")
    with app_store() as conn:
        conn.execute(
            "INSERT INTO conversations (id,owner_id,title,dataset_id,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (cid, _owner(), title, dataset_id, now, now),
        )
    return {"id": cid, "title": title, "dataset_id": dataset_id, "created_at": now}


def conversation_owned(conversation_id: str) -> bool:
    """True when the conversation exists and belongs to the current owner."""
    extra, params = _and_owner(_owner())
    with app_store() as conn:
        row = conn.execute(
            f"SELECT 1 FROM conversations WHERE id = ?{extra}", (conversation_id, *params)
        ).fetchone()
    return row is not None


def ensure_conversation(conversation_id: str | None, dataset_id: str | None = None) -> str:
    """Return a conversation id the current owner may write to.

    An id that belongs to somebody else is never reused or revealed: the caller
    simply gets a fresh conversation of their own.
    """
    if conversation_id:
        if conversation_owned(conversation_id):
            return conversation_id
        with app_store() as conn:
            taken = conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if not taken:
            now = utcnow()
            with app_store() as conn:
                conn.execute(
                    "INSERT INTO conversations (id,owner_id,title,dataset_id,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                    (conversation_id, _owner(), "New analysis", dataset_id, now, now),
                )
            return conversation_id
    return create_conversation(dataset_id)["id"]


def add_message(conversation_id: str, role: str, content: str, payload: dict | None = None) -> str:
    if not conversation_owned(conversation_id):
        raise AInsightError("That conversation is not available.")
    mid = new_id("msg")
    now = utcnow()
    with app_store() as conn:
        conn.execute(
            "INSERT INTO messages (id,conversation_id,role,content,payload_json,created_at) VALUES (?,?,?,?,?,?)",
            (mid, conversation_id, role, content, dumps(payload or {}), now),
        )
        conn.execute(
            "UPDATE conversations SET updated_at = ?, dataset_id = COALESCE(?, dataset_id) WHERE id = ?",
            (now, (payload or {}).get("dataset_id"), conversation_id),
        )
    return mid


def get_messages(conversation_id: str, limit: int = 20) -> list[dict]:
    if not conversation_owned(conversation_id):
        return []
    with app_store() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (conversation_id, limit),
        ).fetchall()
    messages = []
    for row in reversed(rows):
        item = dict(row)
        item["payload"] = loads(item.pop("payload_json", None), {}) or {}
        messages.append(item)
    return messages


def clear_conversation(conversation_id: str) -> None:
    if not conversation_owned(conversation_id):
        return
    with app_store() as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))


# --------------------------------------------------------------------------
# Query history
# --------------------------------------------------------------------------


_MAX_RESPONSE_BYTES = 1_500_000
_SECRETISH = ("password", "secret", "api_key", "apikey", "authorization", "private_key", "access_token", "refresh_token", "bearer")


def _scrub(obj, depth: int = 0):
    """Defence in depth: nothing that looks like a credential is ever written into saved responses."""
    if depth > 12:
        return obj
    if isinstance(obj, dict):
        return {k: ("(removed)" if (str(k).lower() == "token" or any(x in str(k).lower() for x in _SECRETISH)) else _scrub(v, depth + 1))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v, depth + 1) for v in obj]
    return obj


def _response_json(response) -> str | None:
    """The saved analysis shown when the user reopens it. Capped so history cannot grow without bound."""
    if not response:
        return None
    text = dumps(_scrub(response))
    if len(text.encode("utf-8")) <= _MAX_RESPONSE_BYTES:
        return text
    slim = dict(response)
    slim.pop("extra_visualizations", None)
    slim.pop("visualization", None)
    slim["truncated_for_storage"] = True
    return dumps(_scrub(slim))


def add_history(entry: dict) -> str:
    hid = entry.get("id") or new_id("q")
    row = {
        "id": hid,
        "owner_id": _owner(),
        "conversation_id": entry.get("conversation_id"),
        "question": entry.get("question", ""),
        "dataset_id": entry.get("dataset_id"),
        "dataset_name": entry.get("dataset_name"),
        "intent": entry.get("intent"),
        "engine": entry.get("engine"),
        "sql": entry.get("sql"),
        "plan_json": dumps(entry.get("plan") or {}),
        "status": entry.get("status", "success"),
        "error": entry.get("error"),
        "row_count": int(entry.get("row_count") or 0),
        "execution_ms": float(entry.get("execution_ms") or 0),
        "total_ms": float(entry.get("total_ms") or 0),
        "answer": entry.get("answer"),
        "result_json": dumps(entry.get("result") or {}),
        "response_json": _response_json(entry.get("response")),
        "analysis_id": entry.get("analysis_id"),
        "title": entry.get("title"),
        "created_at": utcnow(),
    }
    with app_store() as conn:
        conn.execute(
            """INSERT INTO query_history (id,owner_id,conversation_id,question,dataset_id,dataset_name,intent,
                 engine,sql,plan_json,status,error,row_count,execution_ms,total_ms,answer,result_json,
                 response_json,analysis_id,title,created_at)
               VALUES (:id,:owner_id,:conversation_id,:question,:dataset_id,:dataset_name,:intent,:engine,:sql,
                 :plan_json,:status,:error,:row_count,:execution_ms,:total_ms,:answer,:result_json,
                 :response_json,:analysis_id,:title,:created_at)""",
            row,
        )
    return hid


def list_history(limit: int = 100, q: str | None = None, favorite: bool = False, intent: str | None = None,
                 dataset_id: str | None = None, status: str | None = None) -> list[dict]:
    """The signed-in user's saved analyses, newest first, optionally searched/filtered."""
    owner = _owner()
    clauses, params = [], []
    if owner is not None:
        clauses.append("owner_id = ?")
        params.append(owner)
    if q:
        like = "%" + q.strip().replace("%", "").replace("_", "")[:100] + "%"
        clauses.append("(question LIKE ? OR COALESCE(title,'') LIKE ? OR COALESCE(answer,'') LIKE ? OR COALESCE(dataset_name,'') LIKE ?)")
        params += [like] * 4
    if favorite:
        clauses.append("favorite = 1")
    if intent:
        clauses.append("intent = ?")
        params.append(intent)
    if dataset_id:
        clauses.append("dataset_id = ?")
        params.append(dataset_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with app_store() as conn:
        rows = conn.execute(
            f"SELECT * FROM query_history {where} ORDER BY favorite DESC, created_at DESC, rowid DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["plan"] = loads(item.pop("plan_json", None), {})
        item["has_saved_analysis"] = bool(item.pop("response_json", None))
        item["favorite"] = bool(item["favorite"])
        item.pop("result_json", None)
        out.append(item)
    return out


def update_history(history_id: str, **fields) -> bool:
    """Rename / favorite / store a refreshed analysis. Owner-scoped: another user's row is never touched."""
    allowed = {"title": fields.get("title"), "favorite": None if fields.get("favorite") is None else int(bool(fields["favorite"]))}
    sets, params = [], []
    for key, value in allowed.items():
        if value is not None:
            sets.append(f"{key} = ?")
            params.append(value if key != "title" else str(value).strip()[:200])
    if "response" in fields:
        sets.append("response_json = ?")
        params.append(_response_json(fields["response"]))
        sets.append("refreshed_at = ?")
        params.append(utcnow())
        if fields.get("answer") is not None:
            sets.append("answer = ?")
            params.append(str(fields["answer"])[:300])
        if fields.get("analysis_id"):
            sets.append("analysis_id = ?")
            params.append(fields["analysis_id"])
    if not sets:
        return False
    extra, owner_params = _and_owner(_owner())
    with app_store() as conn:
        cur = conn.execute(f"UPDATE query_history SET {', '.join(sets)} WHERE id = ?{extra}", (*params, history_id, *owner_params))
    return cur.rowcount > 0


def get_saved_response(history_id: str) -> dict | None:
    extra, params = _and_owner(_owner())
    with app_store() as conn:
        row = conn.execute(f"SELECT * FROM query_history WHERE id = ?{extra}", (history_id, *params)).fetchone()
    if not row:
        return None
    item = dict(row)
    return {"id": item["id"], "question": item["question"], "title": item.get("title"), "favorite": bool(item.get("favorite")),
            "dataset_id": item.get("dataset_id"), "dataset_name": item.get("dataset_name"), "created_at": item["created_at"],
            "refreshed_at": item.get("refreshed_at"), "status": item.get("status"), "intent": item.get("intent"),
            "response": loads(item.get("response_json"), None), "result": loads(item.get("result_json"), {}),
            "answer": item.get("answer")}


def get_history_entry(history_id: str) -> dict | None:
    extra, params = _and_owner(_owner())
    with app_store() as conn:
        row = conn.execute(
            f"SELECT * FROM query_history WHERE id = ?{extra}", (history_id, *params)
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["plan"] = loads(item.pop("plan_json", None), {})
    item["result"] = loads(item.pop("result_json", None), {})
    item.pop("response_json", None)
    return item


def delete_history(history_id: str | None = None) -> int:
    extra, params = _and_owner(_owner())
    with app_store() as conn:
        if history_id:
            cur = conn.execute(f"DELETE FROM query_history WHERE id = ?{extra}", (history_id, *params))
        else:
            cur = conn.execute(f"DELETE FROM query_history WHERE 1 = 1{extra}", params)
    return cur.rowcount


def add_feedback(history_id: str | None, rating: str, comment: str = "") -> str:
    if history_id and get_history_entry(history_id) is None:
        raise AInsightError("That query is no longer in your history.")
    fid = new_id("fb")
    with app_store() as conn:
        conn.execute(
            "INSERT INTO feedback (id,owner_id,history_id,rating,comment,created_at) VALUES (?,?,?,?,?,?)",
            (fid, _owner(), history_id, rating, comment[:1000], utcnow()),
        )
    return fid


def feedback_summary() -> dict:
    where, params = _where_owner(_owner())
    with app_store() as conn:
        rows = conn.execute(f"SELECT rating, COUNT(*) c FROM feedback {where} GROUP BY rating", params).fetchall()
    counts = {r["rating"]: r["c"] for r in rows}
    helpful = counts.get("helpful", 0)
    total = sum(counts.values())
    return {
        "helpful": helpful,
        "not_helpful": counts.get("not_helpful", 0),
        "total": total,
        "helpful_rate": round(helpful / total * 100, 1) if total else None,
    }


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def _where_owner(owner: str | None) -> tuple[str, tuple]:
    return ("WHERE owner_id = ?", (owner,)) if owner is not None else ("", ())


def metrics_summary() -> dict:
    where, params = _where_owner(_owner())
    with app_store() as conn:
        datasets = conn.execute(
            f"SELECT COUNT(*) c, COALESCE(SUM(row_count),0) rows FROM datasets {where}", params
        ).fetchone()
        hist = conn.execute(
            f"""SELECT COUNT(*) total,
                      SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) ok,
                      AVG(total_ms) avg_ms
               FROM query_history {where}""",
            params,
        ).fetchone()
        by_intent = conn.execute(
            f"SELECT intent, COUNT(*) c FROM query_history {where} GROUP BY intent ORDER BY c DESC", params
        ).fetchall()
    total = hist["total"] or 0
    ok = hist["ok"] or 0
    return {
        "datasets": datasets["c"] or 0,
        "total_rows": datasets["rows"] or 0,
        "queries": total,
        "successful_queries": ok,
        "failed_queries": total - ok,
        "success_rate": round(ok / total * 100, 1) if total else None,
        "avg_query_ms": round(hist["avg_ms"], 1) if hist["avg_ms"] else None,
        "intents": [{"intent": r["intent"] or "unknown", "count": r["c"]} for r in by_intent],
        "feedback": feedback_summary(),
    }
