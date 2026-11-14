"""AI auto-analytics dashboard: owner-scoped service around app/dashboard_ai.

Stored config only holds personalisation (hidden / order / favourites / sizes / saved filters). Numbers are recomputed
from the data with the validated pandas engine; results are cached briefly per (owner, dataset, filters)."""

from __future__ import annotations

import io
import threading
import time
from collections import OrderedDict

import pandas as pd

from app.dashboard_ai import compose as compose_mod
from app.database import dashboard_repo, repository
from app.files.dataframe_manager import frame_manager
from app.services.dashboard_service import _source
from app.utils.exceptions import AInsightError
from app.utils.helpers import utcnow

CACHE_TTL = 120
CACHE_MAX = 40
MAX_SAVED_FILTERS = 12
LAYOUT_KEYS = ("hidden", "order", "favorites", "sizes", "saved_filters", "measure")
_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
_lock = threading.Lock()


def _dash(dashboard_id: str) -> dict:
    dash = dashboard_repo.get(dashboard_id)  # owner-scoped: another user's dashboard looks like it does not exist
    if not dash:
        raise AInsightError("That dashboard does not exist.")
    return dash


def _norm(filters: dict | None) -> dict[str, list[str]]:
    out = {}
    for k, v in (filters or {}).items():
        vals = v if isinstance(v, list) else [v]
        vals = [str(x) for x in vals if str(x) != ""]
        if vals:
            out[str(k)] = sorted(vals)
    return out


def _key(dash: dict, filters, date_from, date_to, measure) -> tuple:
    owner = dash.get("owner_id")
    return (owner, dash["dataset_id"], tuple(sorted((k, tuple(v)) for k, v in filters.items())), date_from or "", date_to or "", measure or "")


def layout_of(dash: dict) -> dict:
    auto = dash["config"].get("auto") or {}
    return {"hidden": auto.get("hidden", []), "order": auto.get("order", []), "favorites": auto.get("favorites", []),
            "sizes": auto.get("sizes", {}), "saved_filters": auto.get("saved_filters", []), "measure": auto.get("measure")}


def _clear(dataset_id: str) -> None:
    with _lock:
        for k in [k for k in _cache if k[1] == dataset_id]:
            _cache.pop(k, None)


def render(dashboard_id: str, filters=None, date_from=None, date_to=None, measure=None, part: str = "all") -> dict:
    dash = _dash(dashboard_id)
    layout = layout_of(dash)
    filters = _norm(filters)
    measure = measure or layout.get("measure")
    key = _key(dash, filters, date_from, date_to, measure)
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            _cache.move_to_end(key)
            data, cached = hit[1], True
        else:
            data, cached = None, False
    if data is None:
        source = _source(dash["dataset_id"])
        data = compose_mod.compose(source, source.frame(), filters, date_from, date_to, measure)
        with _lock:
            _cache[key] = (time.time(), data)
            while len(_cache) > CACHE_MAX:
                _cache.popitem(last=False)
    out = {k: v for k, v in data.items()}
    out.update({"id": dash["id"], "name": dash["name"], "layout": layout, "cached": cached, "updated_at": dash["updated_at"],
                "last_refreshed": dash["config"].get("auto", {}).get("last_refreshed") or dash["updated_at"]})
    if part == "core" and out.get("ok"):
        keep = {c["id"] for c in out["charts"] if c["role"] == "hero"}
        out["charts"] = [c for c in out["charts"] if c["id"] in keep]
        out.pop("details", None)
        out["partial"] = True
    elif part == "rest" and out.get("ok"):
        out = {"ok": True, "charts": [c for c in out["charts"] if c["role"] != "hero"], "unavailable": out["unavailable"],
               "details": out["details"], "story": out["story"], "partial": True}
    return out


def refresh(dashboard_id: str) -> dict:
    dash = _dash(dashboard_id)
    frame_manager.invalidate(dash["dataset_id"])
    _clear(dash["dataset_id"])
    cfg = dash["config"]
    cfg.setdefault("auto", {})["last_refreshed"] = utcnow()
    dashboard_repo.update(dashboard_id, config=cfg)
    return render(dashboard_id)


def save_layout(dashboard_id: str, body: dict) -> dict:
    dash = _dash(dashboard_id)
    cfg = dash["config"]
    auto = cfg.setdefault("auto", {})
    for key in ("hidden", "order", "favorites"):
        if key in body:
            vals = body[key]
            if not isinstance(vals, list) or len(vals) > 100 or not all(isinstance(v, str) and len(v) <= 120 for v in vals):
                raise AInsightError(f"“{key}” must be a short list of chart ids.")
            auto[key] = vals
    if "sizes" in body:
        sizes = body["sizes"]
        if not isinstance(sizes, dict) or len(sizes) > 100 or any(v not in ("small", "medium", "large") for v in sizes.values()):
            raise AInsightError("Chart sizes must be small, medium or large.")
        auto["sizes"] = {str(k)[:120]: v for k, v in sizes.items()}
    if "measure" in body:
        m = body["measure"]
        if m not in (None, "") and (m not in _source(dash["dataset_id"]).measures):
            raise AInsightError("That measure is not a column of this dataset.")
        auto["measure"] = m or None
    if "saved_filters" in body:
        sf = body["saved_filters"]
        if not isinstance(sf, list) or len(sf) > MAX_SAVED_FILTERS:
            raise AInsightError(f"You can save up to {MAX_SAVED_FILTERS} filter sets.")
        clean = []
        for s in sf:
            if not isinstance(s, dict) or not 1 <= len(str(s.get("name", "")).strip()) <= 60:
                raise AInsightError("Name each saved filter set (up to 60 characters).")
            clean.append({"name": str(s["name"]).strip(), "filters": _norm(s.get("filters")), "date_from": s.get("date_from") or None, "date_to": s.get("date_to") or None})
        auto["saved_filters"] = clean
    dashboard_repo.update(dashboard_id, config=cfg)
    _clear(dash["dataset_id"])
    return layout_of(_dash(dashboard_id))


def duplicate(dashboard_id: str) -> dict:
    from app.services.dashboard_service import MAX_DASHBOARDS
    dash = _dash(dashboard_id)
    if len(dashboard_repo.list_all()) >= MAX_DASHBOARDS:
        raise AInsightError(f"You can keep up to {MAX_DASHBOARDS} dashboards.")
    new = dashboard_repo.create(f"{dash['name']} (copy)"[:80], dash["dataset_id"], dash["config"])
    return {"id": new["id"], "name": new["name"], "dataset_id": new["dataset_id"]}


def _safe_cell(v):
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v  # stops spreadsheet formula injection from data values
    return v


def export_data(dashboard_id: str, fmt: str, filters=None, date_from=None, date_to=None) -> tuple[bytes, str, str]:
    """The underlying (filtered) records, exactly what the dashboard is computed from."""
    dash = _dash(dashboard_id)
    source = _source(dash["dataset_id"])
    full = source.frame()
    prof = compose_mod.profiler.build(full, source)
    frame, _, _ = compose_mod.apply_filters(full, prof, _norm(filters), date_from, date_to)
    if len(frame) > 200_000:
        raise AInsightError("That selection is too large to export; apply a filter first.")
    frame = frame.copy()
    for c in frame.columns:
        if frame[c].dtype == object or str(frame[c].dtype).startswith("string"):
            frame[c] = frame[c].map(_safe_cell)
    base = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in dash["name"])[:60] or "dashboard"
    if fmt == "csv":
        return frame.to_csv(index=False).encode("utf-8"), "text/csv", f"{base}.csv"
    if fmt in ("xlsx", "excel"):
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as w:
            frame.to_excel(w, index=False, sheet_name="Data")
        return buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"{base}.xlsx"
    raise AInsightError("Exports are available as CSV or Excel.")
