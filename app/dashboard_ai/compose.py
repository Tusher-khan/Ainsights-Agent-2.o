"""Assemble the auto dashboard payload: filters -> profile -> decisions -> insights."""

from __future__ import annotations

import time

import pandas as pd

from app.dashboard_ai import analysis as A
from app.dashboard_ai import composer as C
from app.dashboard_ai import insights as I
from app.dashboard_ai import profiler
from app.utils.helpers import utcnow

TABS = [("overview", "Overview"), ("trends", "Trends"), ("breakdown", "Breakdown"), ("performance", "Performance"),
        ("distribution", "Distribution"), ("details", "Details"), ("insights", "AI Insights")]
MAX_DETAIL_ROWS = 200
MAX_FILTER_VALUES = 50


def apply_filters(frame: pd.DataFrame, prof: profiler.Profile, filters: dict[str, list[str]], date_from: str | None, date_to: str | None):
    """Filters are equality / membership on real columns only; nothing is ever evaluated."""
    applied: dict[str, list[str]] = {}
    allowed = {d.name for d in prof.dims}
    for name, values in (filters or {}).items():
        if name not in allowed or name not in frame.columns:
            continue
        vals = [str(v) for v in values if str(v) != ""]
        if vals:
            frame = frame[frame[name].astype(str).isin(vals)]
            applied[name] = vals
    applied_date = None
    if prof.date and (date_from or date_to):
        d = pd.to_datetime(frame[prof.date.column], errors="coerce")
        lo = pd.to_datetime(date_from, errors="coerce") if date_from else None
        hi = pd.to_datetime(date_to, errors="coerce") if date_to else None
        mask = pd.Series(True, index=frame.index)
        if lo is not None and not pd.isna(lo):
            mask &= d >= lo
        if hi is not None and not pd.isna(hi):
            mask &= d < hi + pd.Timedelta(days=1)
        frame = frame[mask]
        applied_date = {"from": date_from, "to": date_to}
    return frame, applied, applied_date


def filter_options(full: pd.DataFrame, prof: profiler.Profile) -> dict:
    dims = []
    for d in prof.dims:
        if not d.filterable or d.name not in full.columns:
            continue
        counts = full[d.name].dropna().astype(str).value_counts()
        dims.append({"name": d.name, "label": A.readable(d.name), "values": sorted(counts.index[:MAX_FILTER_VALUES].tolist())})
    date = None
    if prof.date:
        date = {"column": prof.date.column, "label": A.readable(prof.date.column), "min": str(prof.date.start.date()), "max": str(prof.date.end.date())}
    return {"dims": dims[:C.MAX_FILTER_DIMS], "date": date}


def compose(source, full: pd.DataFrame, filters=None, date_from=None, date_to=None, measure: str | None = None, detail_rows: int = MAX_DETAIL_ROWS) -> dict:
    started = time.time()
    prof_full = profiler.build(full, source)
    if not prof_full.measures:
        return {"ok": False, "reason": "This dataset has no numeric column with variation, so an automated dashboard cannot be built.", "dataset": {"id": source.id, "name": source.name}}
    frame, applied, applied_date = apply_filters(full, prof_full, filters or {}, date_from, date_to)
    options = filter_options(full, prof_full)
    if frame.empty:
        return {"ok": False, "empty": True, "reason": "No records match the current filters.", "dataset": {"id": source.id, "name": source.name},
                "filters": options, "applied_filters": applied, "applied_date": applied_date, "rows": 0, "rows_total": int(len(full))}
    prof = profiler.build(frame, source)
    # keep the measure/dimension roles chosen on the full data stable while filtering (a filter must not reshuffle the dashboard)
    prof.dims = [d for d in prof.dims if d.name not in applied or True]
    if len(prof.measures) < len(prof_full.measures):
        prof.measures = [next((m for m in prof.measures if m.name == f.name), f) for f in prof_full.measures]
    prim = prof.by_name(measure) if measure else None
    primary = prim or prof.measures[0]
    ctx = C.Ctx(frame=frame, prof=prof, source=source, primary=primary)
    C.decide(ctx)
    kpis = I.build_kpis(ctx)
    changed = I.what_changed(ctx)
    ins = I.build_insights(ctx)
    charts = ctx.charts
    counts = {}
    for c in charts:
        counts[c["tab"]] = counts.get(c["tab"], 0) + 1
    counts["details"], counts["insights"] = int(len(frame)), len(ins)
    tabs = [{"id": t, "label": l, "count": counts.get(t, 0)} for t, l in TABS if t in ("details", "insights") or counts.get(t)]
    cols = list(frame.columns)[:20]
    detail = frame[cols].head(detail_rows).astype(object).where(frame[cols].head(detail_rows).notna(), None)
    detail_rows_list = [{k: (v if isinstance(v, (int, float, str, type(None))) else str(v)) for k, v in r.items()} for r in detail.to_dict("records")]
    hero = next((c["id"] for c in charts if c["role"] == "hero"), None)
    return {"ok": True, "dataset": {"id": source.id, "name": source.name}, "primary": {"name": primary.name, "label": primary.label, "aggregation": primary.agg},
            "measures": [{"name": m.name, "label": m.label} for m in prof_full.measures[:8]],
            "rows": int(len(frame)), "rows_total": int(len(full)), "kpis": kpis, "hero": hero, "tabs": tabs, "charts": charts,
            "unavailable": ctx.unavailable, "insights": ins, "what_changed": changed, "quality": I.quality_panel(ctx, len(full)),
            "recommendations": I.recommendations(ctx), "story": I.business_story(ctx, ins, changed), "filters": options,
            "applied_filters": applied, "applied_date": applied_date,
            "details": {"columns": cols, "rows": detail_rows_list, "total": int(len(frame))},
            "generated_at": utcnow(), "compute_ms": int((time.time() - started) * 1000)}
