"""Dashboard builder.

A dashboard is stored as *configuration* (which measures, which breakdowns, which filters); the numbers and
charts are computed from the data every time it is opened, with the same chart builders the analysis agent
uses (app/agent/viz.py). No second chart system, and nothing a model typed ever becomes a number.
"""

from __future__ import annotations

import re

import pandas as pd

from app.agent import analysis_tools as AT
from app.agent import viz
from app.core.datasource import from_dataset_record
from app.database import dashboard_repo, repository
from app.utils.exceptions import AInsightError, DatasetNotFoundError
from app.utils.helpers import human_number

MAX_DASHBOARDS = 50
MAX_KPIS, MAX_BREAKDOWNS, MAX_FILTER_DIMS, MAX_FILTER_VALUES = 4, 2, 3, 50


def _source(dataset_id: str):
    record = repository.get_dataset(dataset_id)  # owner-scoped
    if not record:
        raise DatasetNotFoundError()
    return from_dataset_record(record)


def _groupable(source, frame: pd.DataFrame) -> list[str]:
    """Category columns with a readable number of distinct values (2..30)."""
    out = []
    for d in source.dimensions:
        if d in frame.columns and 2 <= frame[d].nunique(dropna=True) <= 30:
            out.append(d)
    return sorted(out, key=lambda d: frame[d].nunique())


def suggest_config(dataset_id: str, title: str | None = None, measure: str | None = None) -> dict:
    """Choose KPIs, charts and filters from the dataset's own metadata."""
    source = _source(dataset_id)
    if not source.measures:
        raise AInsightError("This dataset has no numeric column, so a dashboard cannot be built from it.")
    frame = source.frame()
    measures = list(source.measures)
    if measure:
        column = source.resolve_column(measure)
        if column in measures:
            measures.remove(column)
            measures.insert(0, column)
    primary = measures[0]
    dims = _groupable(source, frame)
    charts = []
    if source.date_columns:
        charts.append({"type": "trend", "measure": primary, "grain": "month"})
    for d in dims[:MAX_BREAKDOWNS]:
        charts.append({"type": "composition", "measure": primary, "dimension": d})
    return {"title": title or f"{source.name} overview", "kpis": measures[:MAX_KPIS], "charts": charts,
            "filters": dims[:MAX_FILTER_DIMS], "date_column": (source.date_columns or [None])[0]}


def create(dataset_id: str, title: str | None = None, measure: str | None = None, from_history: str | None = None) -> dict:
    if len(dashboard_repo.list_all()) >= MAX_DASHBOARDS:
        raise AInsightError(f"You can keep up to {MAX_DASHBOARDS} dashboards.")
    if from_history:
        entry = repository.get_history_entry(from_history)
        if not entry:
            raise AInsightError("That analysis is no longer in your history.")
        dataset_id = dataset_id or entry.get("dataset_id")
        title = title or f"Dashboard: {entry.get('title') or entry['question']}"[:80]
        if not measure:  # reuse the measure the analysis was about, if the question names one
            src = _source(dataset_id)
            q = entry["question"].lower()
            measure = next((m for m in src.measures if m.lower().replace("_", " ") in q.replace("_", " ")), None)
    if not dataset_id:
        raise AInsightError("Choose a dataset for the dashboard.")
    config = suggest_config(dataset_id, title, measure)
    return dashboard_repo.create(config["title"], dataset_id, config)


_RATE_NAME = re.compile(r"discount|rate|ratio|pct|percent|margin|score|price|avg|average|temperature|age", re.I)


def aggregation_for(frame: pd.DataFrame, measure: str) -> str:
    """Amounts are summed; rates, ratios and prices are averaged (their sum has no meaning)."""
    values = pd.to_numeric(frame[measure], errors="coerce").dropna()
    in_unit_range = len(values) > 0 and values.min() >= 0 and values.max() <= 1.0
    return "mean" if (_RATE_NAME.search(measure) or in_unit_range) else "sum"


def _periods_kpi(frame: pd.DataFrame, date_col: str | None, measure: str) -> dict:
    agg = aggregation_for(frame, measure)
    vals = pd.to_numeric(frame[measure], errors="coerce")
    total = float(vals.mean() if agg == "mean" else vals.sum())
    label = ("Average " if agg == "mean" else "") + measure.replace("_", " ").title()
    kpi = {"label": label, "value": total, "display": human_number(total), "aggregation": agg}
    if date_col and date_col in frame.columns:
        d = pd.to_datetime(frame[date_col], errors="coerce")
        g = vals.groupby(d.dt.to_period("M"))
        s = (g.mean() if agg == "mean" else g.sum()).sort_index()
        if len(s) >= 2 and s.iloc[-2]:
            kpi["change_pct"] = round(float((s.iloc[-1] - s.iloc[-2]) / abs(s.iloc[-2]) * 100), 1)
            kpi["change_label"] = f"{s.index[-1]} vs {s.index[-2]}"
    return kpi


def render(dashboard_id: str, filters: dict | None = None) -> dict:
    dash = dashboard_repo.get(dashboard_id)
    if not dash:
        raise AInsightError("That dashboard does not exist.")
    cfg = dash["config"]
    source = _source(dash["dataset_id"])
    frame = source.frame()
    allowed = set(cfg.get("filters", []))
    filters = {k: v for k, v in (filters or {}).items() if k in allowed and v not in (None, "")}
    frame, applied = AT.apply_filters(frame, source, filters)
    date_col = cfg.get("date_column")
    kpis = [_periods_kpi(frame, date_col, m) for m in cfg.get("kpis", []) if m in frame.columns]
    figures, skipped = [], []
    for ch in cfg.get("charts", []):
        m, kind = ch.get("measure"), ch["type"]
        if m not in frame.columns:
            skipped.append(f"{kind}: column {m} no longer exists")
            continue
        vals = pd.to_numeric(frame[m], errors="coerce")
        fig = None
        if kind == "trend" and date_col in frame.columns:
            d = pd.to_datetime(frame[date_col], errors="coerce")
            g = vals.groupby(d.dt.to_period("M"))
            s = (g.mean() if aggregation_for(frame, m) == "mean" else g.sum()).sort_index()
            fig = viz.trend([str(i) for i in s.index], [float(v) for v in s.values], m)
        elif kind == "composition" and ch.get("dimension") in frame.columns:
            if aggregation_for(frame, m) == "mean":
                skipped.append(f"{kind}: {m} is a rate, so shares of its total would be meaningless")
                continue
            else:
                g = vals.groupby(frame[ch["dimension"]].astype(str)).sum()
                fig = viz.composition(list(g.index), [float(v) for v in g.values], m)
                if fig:
                    t = fig["figure"]["layout"]["title"]
                    t["text"] = f"{m.replace('_', ' ').title()} by {ch['dimension'].replace('_', ' ').title()}"
        if fig:
            figures.append(fig["figure"])
        else:
            skipped.append(f"{kind}: not enough data to chart")
    options = {}
    full = source.frame() if applied else frame
    for d in cfg.get("filters", []):
        if d in full.columns:
            options[d] = sorted(full[d].dropna().astype(str).unique())[:MAX_FILTER_VALUES]
    return {"id": dash["id"], "name": dash["name"], "dataset_id": dash["dataset_id"], "dataset": source.name,
            "kpis": kpis, "figures": figures, "filters": options, "applied_filters": applied,
            "rows": int(len(frame)), "skipped": skipped, "updated_at": dash["updated_at"]}


def list_all() -> list[dict]:
    return [{k: d[k] for k in ("id", "name", "dataset_id", "created_at", "updated_at")} for d in dashboard_repo.list_all()]


def rename(dashboard_id: str, name: str) -> dict:
    name = (name or "").strip()
    if not 1 <= len(name) <= 80:
        raise AInsightError("Give the dashboard a name of up to 80 characters.")
    if not dashboard_repo.update(dashboard_id, name=name):
        raise AInsightError("That dashboard does not exist.")
    return {"id": dashboard_id, "name": name}


def delete(dashboard_id: str) -> None:
    if not dashboard_repo.delete(dashboard_id):
        raise AInsightError("That dashboard does not exist.")
