"""Deterministic analysis primitives the agent can call.

Everything here computes numbers with pandas over the user's own data (never a language
model) and returns small, JSON-safe results with the checks that were run on them.
Authorization happens in the tool registry; these functions receive an already
owner-scoped DataSource.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from app.ai import investigation_engine as ie
from app.core.datasource import DataSource
from app.utils.exceptions import UnsupportedRequestError
from app.utils.helpers import human_number, json_safe

MAX_SEGMENTS = 40
MAX_FILTERS = 4


def _measure(source: DataSource, name: str) -> str:
    column = source.resolve_column(name)
    if not column or column not in source.measures:
        raise UnsupportedRequestError(
            f"“{name}” is not a numeric measure in this dataset. Available measures: {', '.join(source.measures[:12]) or 'none'}.",
            f"unknown measure {name!r}",
        )
    return column


def _dimension(source: DataSource, name: str) -> str:
    column = source.resolve_column(name)
    if not column or column in source.measures:
        raise UnsupportedRequestError(
            f"“{name}” is not a category column in this dataset. Available: {', '.join(source.dimensions[:12]) or 'none'}.",
            f"unknown dimension {name!r}",
        )
    return column


def apply_filters(frame: pd.DataFrame, source: DataSource, filters: dict | None) -> tuple[pd.DataFrame, dict]:
    """Equality filters on real columns. Values are compared as text, so no expression is ever evaluated."""
    clean: dict[str, str] = {}
    if not filters:
        return frame, clean
    if not isinstance(filters, dict) or len(filters) > MAX_FILTERS:
        raise UnsupportedRequestError(f"Use at most {MAX_FILTERS} simple column=value filters.")
    for name, value in filters.items():
        column = source.resolve_column(str(name))
        if not column:
            raise UnsupportedRequestError(f"“{name}” is not a column in this dataset.", f"filter column {name!r}")
        frame = frame[frame[column].astype(str) == str(value)]
        clean[column] = str(value)
    return frame, clean


def _periods(frame: pd.DataFrame, time_column: str, grain: str, current_label: str | None):
    """Anchor periods to the *whole* dataset so drill-down steps compare the same two periods."""
    dates = frame[time_column]
    if current_label:
        index = ie._period_index(dates, grain)
        wanted = [p for p in sorted(index.unique()) if ie._period_label(p, grain).lower() == str(current_label).lower()]
        if not wanted:
            raise UnsupportedRequestError(f"There is no {grain} called “{current_label}” in this data.")
        return wanted[0], wanted[0] - 1, [f"Compared {current_label} with the {grain} before it (as requested)."], []
    return ie.choose_periods(dates, grain, "")


def period_contribution(
    source: DataSource,
    measure: str,
    dimension: str | None = None,
    filters: dict | None = None,
    grain: str = "month",
    current_period: str | None = None,
) -> dict:
    """How a measure changed between two periods, optionally inside a filtered slice and split by a dimension."""
    if grain not in {"week", "month", "quarter", "year"}:
        raise UnsupportedRequestError("grain must be week, month, quarter or year.")
    measure = _measure(source, measure)
    how = ie._aggregation_for(measure)
    raw = source.frame()
    time_column = ie._pick_time_column(source, list(raw.columns))
    if not time_column:
        raise UnsupportedRequestError("This dataset has no date column, so periods cannot be compared.")
    frame = ie._prepare_frame(raw, measure, time_column)
    if frame.empty:
        raise UnsupportedRequestError("There are no usable rows in this measure and date column.")
    current, previous, assumptions, limitations = _periods(frame, time_column, grain, current_period)
    frame, applied = apply_filters(frame, source, filters)

    index = ie._period_index(frame[time_column], grain)
    cur, prev = frame[index == current], frame[index == previous]
    total_cur, total_prev = ie._agg(cur, measure, how), ie._agg(prev, measure, how)
    total_delta = total_cur - total_prev
    checks = [{
        "name": "periods_have_data", "passed": len(cur) > 0 and len(prev) > 0,
        "detail": f"{len(cur)} rows in {ie._period_label(current, grain)}, {len(prev)} in {ie._period_label(previous, grain)}",
    }]
    out: dict[str, Any] = {
        "measure": measure, "aggregation": how, "grain": grain, "time_column": time_column,
        "current_label": ie._period_label(current, grain), "previous_label": ie._period_label(previous, grain),
        "filters": applied, "dimension": None, "rows": [], "limitations": limitations, "assumptions": assumptions,
        "total": {"current": round(total_cur, 4), "previous": round(total_prev, 4), "delta": round(total_delta, 4),
                  "delta_pct": ie._pct(total_cur, total_prev), "current_rows": int(len(cur)), "previous_rows": int(len(prev))},
    }
    if dimension:
        dimension = _dimension(source, dimension)
        if dimension in applied:
            raise UnsupportedRequestError(f"“{dimension}” is already fixed by a filter; choose a different dimension.")
        if dimension not in frame.columns:
            raise UnsupportedRequestError(f"“{dimension}” is not available.")
        card = frame[dimension].nunique(dropna=True)
        if card > MAX_SEGMENTS:
            raise UnsupportedRequestError(f"“{dimension}” has {card} distinct values, too many to break down usefully.")
        agg = "mean" if how == "mean" else "sum"
        c = cur.groupby(dimension, observed=True)[measure].agg(agg)
        p = prev.groupby(dimension, observed=True)[measure].agg(agg)
        table = pd.concat([c.rename("current"), p.rename("previous")], axis=1).fillna(0.0)
        table["delta"] = table["current"] - table["previous"]
        rows = []
        for segment, row in table.sort_values("delta").iterrows():
            share = round(float(row["delta"]) / total_delta * 100, 1) if how == "sum" and total_delta else None
            rows.append({"segment": str(segment), "current": round(float(row["current"]), 2),
                         "previous": round(float(row["previous"]), 2), "delta": round(float(row["delta"]), 2),
                         "delta_pct": ie._pct(float(row["current"]), float(row["previous"])), "share_of_change_pct": share})
        out["dimension"], out["rows"] = dimension, rows
        if how == "sum":
            gap = abs(float(table["delta"].sum()) - total_delta)
            checks.append({"name": f"reconciles:{dimension}", "passed": gap <= max(0.01, 1e-9 * max(abs(total_cur), abs(total_prev), 1.0)),
                           "detail": f"segment changes sum to {table['delta'].sum():,.2f} vs total change {total_delta:,.2f}"})
        else:
            limitations.append(f"“{measure}” is averaged, so segment shares of the change are not meaningful and are not shown.")
        aligned = [r for r in rows if total_delta and r["delta"] * total_delta > 0]
        if aligned and how == "sum":
            lead = max(aligned, key=lambda r: abs(r["delta"]))
            out["lead"] = {"segment": lead["segment"], "delta": lead["delta"], "share_of_change_pct": lead["share_of_change_pct"],
                           "delta_pct": lead["delta_pct"]}
        # How many segments does it take to explain 80% of the movement? Small = concentrated.
        if how == "sum" and total_delta and aligned:
            running, need = 0.0, 0
            for r in sorted(aligned, key=lambda r: abs(r["delta"]), reverse=True):
                running += abs(r["delta"])
                need += 1
                if running >= 0.8 * sum(abs(x["delta"]) for x in aligned):
                    break
            out["segments_for_80pct"] = need
            out["segments_moving_with_total"] = len(aligned)
    out["checks"] = checks
    return json_safe(out)


def _series(source: DataSource, measure: str, grain: str) -> tuple[str, list[str], list[float]]:
    from app.agent.tools import trend_series

    return trend_series(source, measure, grain)


def compare_trends(source_a: DataSource, measure_a: str, source_b: DataSource, measure_b: str, grain: str = "month") -> dict:
    """Aggregate each measure inside its own source and relate the two by period."""
    if grain not in {"month", "year"}:
        raise UnsupportedRequestError("Cross-source comparison supports month or year.")
    name_a, labels_a, values_a = _series(source_a, measure_a, grain)
    name_b, labels_b, values_b = _series(source_b, measure_b, grain)
    a, b = dict(zip(labels_a, values_a)), dict(zip(labels_b, values_b))
    shared = sorted(set(a) & set(b))
    if len(shared) < 2:
        raise UnsupportedRequestError(
            "The two sources share fewer than two time periods, so they cannot be compared over time.",
            f"overlap={len(shared)}",
        )
    xa = np.array([a[p] for p in shared], dtype=float)
    xb = np.array([b[p] for p in shared], dtype=float)

    def move(x):
        return {"first": float(x[0]), "last": float(x[-1]), "change": float(x[-1] - x[0]),
                "change_pct": ie._pct(float(x[-1]), float(x[0]))}

    out: dict[str, Any] = {
        "grain": grain, "periods": shared,
        "a": {"source": source_a.name, "measure": name_a, **move(xa), "values": [float(v) for v in xa]},
        "b": {"source": source_b.name, "measure": name_b, **move(xb), "values": [float(v) for v in xb]},
        "dropped_periods": {"only_in_a": len(set(a) - set(b)), "only_in_b": len(set(b) - set(a))},
        "checks": [{"name": "aligned_periods", "passed": len(shared) >= 2, "detail": f"{len(shared)} shared {grain}s"}],
        "limitations": [],
    }
    if len(shared) >= 6 and np.std(np.diff(xa)) > 0 and np.std(np.diff(xb)) > 0:
        r = float(np.corrcoef(np.diff(xa), np.diff(xb))[0, 1])
        out["change_correlation"] = round(r, 3)
        out["relationship"] = "moves in opposite directions" if r <= -0.3 else "moves in the same direction" if r >= 0.3 else "shows no clear link"
    else:
        out["limitations"].append("Fewer than 6 aligned periods (or a flat series), so no correlation between the two was computed.")
    ca, cb = out["a"]["change"], out["b"]["change"]
    out["opposite_trends"] = bool(ca * cb < 0)
    out["direction_summary"] = (
        f"Over the whole period {out['a']['measure']} ({out['a']['source']}) "
        f"{'rose' if ca > 0 else 'fell' if ca < 0 else 'was flat'} while {out['b']['measure']} ({out['b']['source']}) "
        f"{'rose' if cb > 0 else 'fell' if cb < 0 else 'was flat'}.")
    out["limitations"].append("An association between two sources is not proof that one causes the other.")
    return json_safe(out)


def correlate(source: DataSource, x: str, y: str) -> dict:
    x, y = _measure(source, x), _measure(source, y)
    if x == y:
        raise UnsupportedRequestError("Choose two different measures.")
    frame = source.frame()[[x, y]].apply(pd.to_numeric, errors="coerce").dropna()
    n = len(frame)
    if n < 8:
        raise UnsupportedRequestError("There are too few complete rows to measure a relationship (need at least 8).", f"n={n}")
    if frame[x].std() == 0 or frame[y].std() == 0:
        raise UnsupportedRequestError("One of those columns never changes, so no relationship can be measured.")
    pearson = float(frame[x].corr(frame[y]))
    spearman = float(frame[x].corr(frame[y], method="spearman"))
    strength = "very weak" if abs(pearson) < 0.2 else "weak" if abs(pearson) < 0.4 else "moderate" if abs(pearson) < 0.6 else "strong" if abs(pearson) < 0.8 else "very strong"
    sample = frame.sample(min(n, 600), random_state=0)
    return json_safe({
        "x": x, "y": y, "n": n, "pearson": round(pearson, 3), "spearman": round(spearman, 3),
        "strength": strength, "direction": "positive" if pearson > 0 else "negative",
        "points": [{"x": float(a), "y": float(b)} for a, b in zip(sample[x], sample[y])],
        "checks": [{"name": "enough_rows", "passed": n >= 8, "detail": f"{n} complete rows"}],
        "limitations": ["Correlation measures association, not cause.",
                        *([f"Pearson ({pearson:.2f}) and Spearman ({spearman:.2f}) differ noticeably, so outliers or a non-linear shape may matter."]
                          if abs(pearson - spearman) > 0.25 else [])],
    })


def describe_distribution(source: DataSource, measure: str, bins: int = 12) -> dict:
    measure = _measure(source, measure)
    values = pd.to_numeric(source.frame()[measure], errors="coerce").dropna()
    if len(values) < 5:
        raise UnsupportedRequestError("There are too few values to describe a distribution.")
    counts, edges = np.histogram(values, bins=max(4, min(int(bins), 30)))
    q1, med, q3 = (float(values.quantile(q)) for q in (0.25, 0.5, 0.75))
    iqr = q3 - q1
    outliers = int(((values < q1 - 1.5 * iqr) | (values > q3 + 1.5 * iqr)).sum()) if iqr > 0 else 0
    skew = float(values.skew())
    return json_safe({
        "measure": measure, "n": int(len(values)), "mean": float(values.mean()), "median": med, "std": float(values.std()),
        "min": float(values.min()), "max": float(values.max()), "q1": q1, "q3": q3, "outliers": outliers, "skew": round(skew, 2),
        "histogram": [{"low": float(edges[i]), "high": float(edges[i + 1]), "count": int(counts[i])} for i in range(len(counts))],
        "checks": [{"name": "histogram_totals", "passed": int(counts.sum()) == len(values), "detail": f"{int(counts.sum())} of {len(values)} values binned"}],
        "limitations": [],
    })


def data_quality(source: DataSource) -> dict:
    frame = source.frame()
    n = len(frame)
    issues: list[dict] = []
    for column in frame.columns:
        missing = int(frame[column].isna().sum())
        if n and missing / n >= 0.05:
            issues.append({"kind": "missing_values", "column": str(column), "count": missing, "pct": round(missing / n * 100, 1)})
    duplicates = int(frame.duplicated().sum())
    if duplicates:
        issues.append({"kind": "duplicate_rows", "count": duplicates, "pct": round(duplicates / max(n, 1) * 100, 1)})
    for column in source.measures:
        values = pd.to_numeric(frame[column], errors="coerce").dropna()
        if len(values) >= 8 and values.std() > 0:
            q1, q3 = values.quantile(0.25), values.quantile(0.75)
            iqr = q3 - q1
            if iqr > 0:
                extreme = int(((values < q1 - 3 * iqr) | (values > q3 + 3 * iqr)).sum())
                if extreme:
                    issues.append({"kind": "extreme_values", "column": column, "count": extreme, "pct": round(extreme / len(values) * 100, 2)})
        if len(values) and (values < 0).any() and not any(w in column.lower() for w in ("profit", "loss", "change", "delta", "variance", "balance", "margin")):
            issues.append({"kind": "negative_values", "column": column, "count": int((values < 0).sum())})
    for column in source.date_columns:
        parsed = pd.to_datetime(frame[column], errors="coerce")
        bad = int(parsed.isna().sum() - frame[column].isna().sum())
        if bad:
            issues.append({"kind": "unparseable_dates", "column": column, "count": bad})
    return json_safe({"rows": n, "columns": len(frame.columns), "issues": issues,
                      "checks": [{"name": "scanned_all_rows", "passed": True, "detail": f"{n} rows scanned"}], "limitations": []})


def format_number(value: Any) -> str:
    return human_number(value)


def nan_safe(value: Any) -> Any:
    return None if isinstance(value, float) and (math.isnan(value) or math.isinf(value)) else value
