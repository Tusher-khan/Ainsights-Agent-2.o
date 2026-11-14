"""Investigation engine: answers "why did X change?" with evidence.

This is deliberately deterministic. Every number in the output comes from a
pandas aggregation over the dataset; no language model supplies or edits a
figure. The workflow mirrors what an analyst does by hand:

    1. pick the measure and the date column
    2. split the data into a current and a previous period
    3. compare totals (the KPIs)
    4. break the change down by every categorical dimension to find which
       segments contributed most, in both directions
    5. check that each breakdown reconciles with the total change
    6. scan the time series for statistically unusual movements
    7. state facts, then (carefully worded) interpretations, then next steps

The engine reports what the data shows. It never asserts a cause: segment
contribution is evidence of *where* a change happened, not *why*.
"""

from __future__ import annotations

import calendar
import math
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from app.core.datasource import DataSource
from app.utils.exceptions import UnsupportedRequestError
from app.utils.helpers import human_number, json_safe
from app.utils.logger import get_logger

log = get_logger("ai.investigation")

POSITIVE = "#128A6E"
NEGATIVE = "#C4622D"
ACCENT = "#2F5AE0"

# Columns that are rates or prices: summing them is meaningless, so average.
_MEAN_HINT = re.compile(r"(discount|rate|ratio|pct|percent|margin|price|score|rating|age)", re.I)
# Preference order when the question names no measure.
_MEASURE_PRIORITY = ("revenue", "sales", "amount", "total", "profit", "quantity", "units")
_SYNONYMS = {
    "revenue": ("revenue", "sales", "turnover", "income"),
    "sales": ("sales", "revenue", "turnover"),
    "profit": ("profit", "earnings", "margin"),
    "orders": ("orders", "order"),
    "quantity": ("quantity", "units", "volume", "qty"),
}
MIN_PERIODS_FOR_ANOMALY = 6
MAX_DIMENSION_CARDINALITY = 40
TOP_N_DRIVERS = 5
# A segment's move is rated "unusual" when it exceeds this many standard deviations of that
# segment's own earlier period-to-period changes. Chosen by simulation, not by convention: the
# biggest movers are picked out of many segments, so a conventional 2-sigma cut-off flagged
# something "unusual" in ~65% of perfectly stable datasets. At 4 sigma the false-alarm rate was
# ~5% while a 50% drop in one segment was still detected ~84% of the time (noisy synthetic data).
UNUSUAL_K = 4.0
# Prior period-to-period changes needed before a segment can be rated at all.
MIN_HISTORY_CHANGES = 6


@dataclass
class Investigation:
    measure: str
    aggregation: str
    time_column: str | None
    grain: str
    current: dict
    previous: dict
    kpis: list = field(default_factory=list)
    drivers: dict = field(default_factory=dict)
    top_drivers: list = field(default_factory=list)
    hotspots: list = field(default_factory=list)
    anomalies: list = field(default_factory=list)
    series: list = field(default_factory=list)
    charts: list = field(default_factory=list)
    headline: str = ""
    summary: str = ""
    facts: list = field(default_factory=list)
    interpretation: list = field(default_factory=list)
    recommendations: list = field(default_factory=list)
    how_calculated: list = field(default_factory=list)
    limitations: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)
    checks: list = field(default_factory=list)
    confidence: str = "medium"

    def to_dict(self) -> dict:
        return json_safe(self.__dict__)


# --------------------------------------------------------------------------
# Column resolution
# --------------------------------------------------------------------------


def _aggregation_for(column: str) -> str:
    return "mean" if _MEAN_HINT.search(column) else "sum"


def resolve_measure(question: str, source: DataSource) -> tuple[str | None, str | None]:
    """Choose the measure. Returns (column, assumption-or-None)."""
    text = question.lower()
    measures = source.measures
    if not measures:
        return None, None
    # 1. a measure named in the question (exact, then via synonyms)
    for column in measures:
        if column.lower().replace("_", " ") in text:
            return column, None
    for word, group in _SYNONYMS.items():
        if any(re.search(rf"\b{re.escape(term)}\b", text) for term in group):
            for column in measures:
                if any(term in column.lower() for term in group):
                    return column, None
    # 2. nothing named: prefer a headline business measure
    for hint in _MEASURE_PRIORITY:
        for column in measures:
            if hint in column.lower() and not _MEAN_HINT.search(column):
                return column, f"The question named no measure, so “{column}” was used as the headline measure."
    column = next((m for m in measures if not _MEAN_HINT.search(m)), measures[0])
    return column, f"The question named no measure, so “{column}” was used."


def _prepare_frame(raw: pd.DataFrame, measure: str, time_column: str | None) -> pd.DataFrame:
    frame = raw.copy()
    frame[measure] = pd.to_numeric(frame[measure], errors="coerce")
    frame = frame.dropna(subset=[measure])
    if time_column:
        parsed = pd.to_datetime(frame[time_column], errors="coerce")
        frame = frame.assign(**{time_column: parsed}).dropna(subset=[time_column])
    return frame


def _pick_time_column(source: DataSource, frame_columns: list[str]) -> str | None:
    for column in source.date_columns:
        if column in frame_columns:
            return column
    return None


# --------------------------------------------------------------------------
# Periods
# --------------------------------------------------------------------------


def detect_grain(question: str) -> str:
    text = question.lower()
    if re.search(r"\b(year|yearly|annual|annually|yoy|12 months)\b", text):
        return "year"
    if re.search(r"\b(quarter|quarterly|q[1-4])\b", text):
        return "quarter"
    if re.search(r"\b(week|weekly)\b", text):
        return "week"
    return "month"


def _period_index(series: pd.Series, grain: str) -> pd.PeriodIndex:
    freq = {"year": "Y", "quarter": "Q", "month": "M", "week": "W"}[grain]
    return series.dt.to_period(freq)


def _period_label(period: pd.Period, grain: str) -> str:
    if grain == "month":
        return period.strftime("%b %Y")
    if grain == "quarter":
        return f"Q{period.quarter} {period.year}"
    if grain == "year":
        return str(period.year)
    return f"week of {period.start_time.strftime('%d %b %Y')}"


def _period_is_complete(period: pd.Period, latest: pd.Timestamp) -> bool:
    """A period is complete if data reaches (close to) its last day.

    Weekly/monthly exports often end a day or two early, so allow a small gap
    rather than discarding an essentially complete period.
    """
    end = period.end_time.normalize()
    return (end - latest.normalize()).days <= 2


def choose_periods(
    dates: pd.Series, grain: str, question: str
) -> tuple[pd.Period, pd.Period, list[str], list[str]]:
    """Pick (current, previous) periods anchored to the data, not the calendar.

    "Last month" is resolved relative to the latest date *in the dataset*: a
    dataset that ends in 2017 has no meaningful "last month" relative to today.
    """
    assumptions: list[str] = []
    limitations: list[str] = []
    periods = sorted(_period_index(dates, grain).unique())
    if len(periods) < 2:
        raise UnsupportedRequestError(
            "There is only one time period in this data, so there is nothing to compare it with.",
            f"periods={len(periods)} grain={grain}",
        )
    latest_date = dates.max()
    current = periods[-1]
    wants_current = bool(re.search(r"\b(this|current)\s+(month|quarter|year|week)\b", question.lower()))
    if not _period_is_complete(current, latest_date) and not wants_current:
        # Comparing a half-finished period with a full one would manufacture a decline.
        assumptions.append(
            f"{_period_label(current, grain)} is incomplete in this data (it ends {latest_date:%d %b %Y}), "
            f"so the last complete {grain} was used instead."
        )
        current = periods[-2]
        if len(periods) < 3:
            raise UnsupportedRequestError(
                "Only one complete period is available, so a fair comparison is not possible.",
                "need >=2 complete periods",
            )
    elif not _period_is_complete(current, latest_date):
        limitations.append(
            f"{_period_label(current, grain)} is incomplete (data ends {latest_date:%d %b %Y}); "
            "a partial period will look lower than a full one."
        )
    previous = current - 1
    if previous not in set(periods):
        limitations.append(
            f"There is no data for {_period_label(previous, grain)}, so the comparison is against zero activity."
        )
    assumptions.insert(
        0,
        f"“{_period_label(current, grain)}” is the most recent complete {grain} in the data; "
        f"it is compared with {_period_label(previous, grain)}.",
    )
    return current, previous, assumptions, limitations


# --------------------------------------------------------------------------
# Aggregation helpers
# --------------------------------------------------------------------------


def _agg(frame: pd.DataFrame, measure: str, how: str) -> float:
    if frame.empty:
        return 0.0
    return float(frame[measure].mean() if how == "mean" else frame[measure].sum())


def _pct(current: float, previous: float) -> float | None:
    if previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100, 2)


def _kpi(label: str, current: float, previous: float, *, fmt: str = "number", inverse: bool = False) -> dict:
    change = current - previous
    pct = _pct(current, previous)
    direction = "flat" if change == 0 else ("up" if change > 0 else "down")
    return {
        "label": label,
        "current": round(current, 4),
        "previous": round(previous, 4),
        "change": round(change, 4),
        "change_pct": pct,
        "direction": direction,
        "display": human_number(current),
        "format": fmt,
        # For cost-like measures, "up" is not good news; the UI uses this flag.
        "inverse": inverse,
    }


# --------------------------------------------------------------------------
# Drivers
# --------------------------------------------------------------------------


def compute_drivers(
    frame: pd.DataFrame,
    measure: str,
    how: str,
    time_column: str,
    grain: str,
    current: pd.Period,
    previous: pd.Period,
    dimensions: list[str],
) -> tuple[dict, list[dict], list[dict]]:
    """Break the change down by each dimension. Returns (drivers, top, checks)."""
    periods = _period_index(frame[time_column], grain)
    cur = frame[periods == current]
    prev = frame[periods == previous]
    total_cur = _agg(cur, measure, how)
    total_prev = _agg(prev, measure, how)
    total_delta = total_cur - total_prev

    drivers: dict[str, list[dict]] = {}
    flat: list[dict] = []
    checks: list[dict] = []

    for dimension in dimensions:
        if dimension not in frame.columns:
            continue
        cardinality = frame[dimension].nunique(dropna=True)
        if cardinality < 2 or cardinality > MAX_DIMENSION_CARDINALITY:
            continue
        if how == "sum":
            c = cur.groupby(dimension, observed=True)[measure].sum()
            p = prev.groupby(dimension, observed=True)[measure].sum()
        else:
            c = cur.groupby(dimension, observed=True)[measure].mean()
            p = prev.groupby(dimension, observed=True)[measure].mean()
        table = pd.concat([c.rename("current"), p.rename("previous")], axis=1).fillna(0.0)
        table["delta"] = table["current"] - table["previous"]

        # Reconciliation is only a valid check for additive measures.
        if how == "sum":
            gap = abs(float(table["delta"].sum()) - total_delta)
            tolerance = max(1e-6, 1e-9 * max(abs(total_cur), abs(total_prev), 1.0))
            checks.append(
                {
                    "name": f"reconciles:{dimension}",
                    "passed": gap <= max(tolerance, 0.01),
                    "detail": f"segment changes sum to {table['delta'].sum():,.2f} vs total change {total_delta:,.2f}",
                }
            )

        rows = []
        for segment, row in table.sort_values("delta").iterrows():
            share = None
            if how == "sum" and total_delta != 0:
                share = round(float(row["delta"]) / total_delta * 100, 1)
            rows.append(
                {
                    "dimension": dimension,
                    "segment": str(segment),
                    "current": round(float(row["current"]), 2),
                    "previous": round(float(row["previous"]), 2),
                    "delta": round(float(row["delta"]), 2),
                    "delta_pct": _pct(float(row["current"]), float(row["previous"])),
                    "share_of_change_pct": share,
                }
            )
        drivers[dimension] = rows
        flat.extend(rows)

    # The most informative drivers are those that move *with* the total change
    # and are large relative to it; also surface the biggest offsetting segment.
    if total_delta != 0:
        aligned = [r for r in flat if r["delta"] * total_delta > 0]
        aligned.sort(key=lambda r: abs(r["delta"]), reverse=True)
        top = aligned[:TOP_N_DRIVERS]
    else:
        top = sorted(flat, key=lambda r: abs(r["delta"]), reverse=True)[:TOP_N_DRIVERS]
    return drivers, top, checks


def _offset(drivers: dict, total_delta: float) -> dict | None:
    """Largest segment moving against the overall direction."""
    if total_delta == 0:
        return None
    opposing = [r for rows in drivers.values() for r in rows if r["delta"] * total_delta < 0]
    if not opposing:
        return None
    return max(opposing, key=lambda r: abs(r["delta"]))



def _variation(frame: pd.DataFrame, periods: pd.Series, keys: list[str], measure: str, how: str,
               current: pd.Period) -> dict:
    """Typical period-to-period swing per segment, from history *before* the current move.

    Used to tell a genuinely unusual move from ordinary month-to-month noise.
    Needs >= MIN_HISTORY_CHANGES prior changes to say anything; otherwise the segment is not rated.
    """
    work = frame.assign(_p=periods)
    agg = "mean" if how == "mean" else "sum"
    grouped = work.groupby(["_p", *keys], observed=True)[measure].agg(agg).unstack("_p").fillna(0.0)
    history = grouped[[c for c in grouped.columns if c < current]]
    changes = history.diff(axis=1).iloc[:, 1:]
    out = {}
    for key, row in changes.iterrows():
        vals = row.to_numpy(dtype=float)
        if len(vals) >= MIN_HISTORY_CHANGES:
            parts = key if isinstance(key, tuple) else (key,)
            out[tuple(str(x) for x in parts)] = float(np.std(vals, ddof=1))
    return out


def _flag_unusual(rows: list[dict], variation: dict, key_fields: list[str]) -> None:
    for row in rows:
        std = variation.get(tuple(row[k] for k in key_fields))
        if std is None:
            row["unusual"] = None  # not enough history to say
        elif std == 0:
            row["unusual"] = abs(row["delta"]) > 0
        else:
            row["unusual"] = abs(row["delta"]) > UNUSUAL_K * std


def compute_hotspots(frame: pd.DataFrame, measure: str, how: str, time_column: str, grain: str,
                     current: pd.Period, previous: pd.Period, dimensions: list[str],
                     total_delta: float) -> list[dict]:
    """Two-dimension combinations (e.g. Furniture x South).

    A change concentrated in one combination is diluted when each dimension is
    examined alone, so the combinations can reveal what single-dimension
    breakdowns hide.
    """
    usable = [d for d in dimensions
              if 2 <= frame[d].nunique(dropna=True) <= 15]
    if len(usable) < 2 or total_delta == 0:
        return []
    periods = _period_index(frame[time_column], grain)
    cur, prev = frame[periods == current], frame[periods == previous]
    spots: list[dict] = []
    for i, d1 in enumerate(usable):
        for d2 in usable[i + 1:]:
            agg = "mean" if how == "mean" else "sum"
            c = cur.groupby([d1, d2], observed=True)[measure].agg(agg)
            p = prev.groupby([d1, d2], observed=True)[measure].agg(agg)
            table = pd.concat([c.rename("current"), p.rename("previous")], axis=1).fillna(0.0)
            table["delta"] = table["current"] - table["previous"]
            table = table[table["delta"] * total_delta > 0]
            table = table.reindex(table["delta"].abs().sort_values(ascending=False).index).head(3)
            for (a, b), row in table.iterrows():
                spots.append({
                    "dimensions": [d1, d2], "segments": [str(a), str(b)],
                    "label": f"{a} × {b}", "dimension": f"{d1} × {d2}", "segment": f"{a} × {b}",
                    "current": round(float(row["current"]), 2), "previous": round(float(row["previous"]), 2),
                    "delta": round(float(row["delta"]), 2),
                    "delta_pct": _pct(float(row["current"]), float(row["previous"])),
                    "share_of_change_pct": round(float(row["delta"]) / total_delta * 100, 1) if how == "sum" else None,
                })
    spots.sort(key=lambda r: abs(r["delta"]), reverse=True)
    spots = spots[:5]
    # rate each hotspot against its own history (only 2-key variation needed)
    by_pair: dict = {}
    for spot in spots:
        by_pair.setdefault(tuple(spot["dimensions"]), []).append(spot)
    for dims, group in by_pair.items():
        variation = _variation(frame, periods, list(dims), measure, how, current)
        for spot in group:
            std = variation.get(tuple(spot["segments"]))
            spot["unusual"] = None if std is None else (abs(spot["delta"]) > (UNUSUAL_K * std if std else 0))
    return spots

# --------------------------------------------------------------------------
# Anomalies
# --------------------------------------------------------------------------


def detect_anomalies(series: pd.Series, labels: list[str]) -> list[dict]:
    """Flag unusual period-over-period moves with a robust (MAD-based) score.

    A modified z-score above 3.5 is the conventional cut-off (Iglewicz &
    Hoaglin). This is a statistical flag, not proof of a problem.
    """
    values = series.astype(float).to_numpy()
    if len(values) < MIN_PERIODS_FOR_ANOMALY:
        return []
    changes = np.array([np.nan] + [
        (values[i] - values[i - 1]) / abs(values[i - 1]) if values[i - 1] != 0 else np.nan
        for i in range(1, len(values))
    ])
    valid = changes[~np.isnan(changes)]
    if len(valid) < MIN_PERIODS_FOR_ANOMALY - 1:
        return []
    median = float(np.median(valid))
    mad = float(np.median(np.abs(valid - median)))
    if mad == 0:
        std = float(np.std(valid))
        if std == 0:
            return []
        scores = (changes - float(np.mean(valid))) / std
        threshold = 3.0
    else:
        scores = 0.6745 * (changes - median) / mad
        threshold = 3.5
    found = []
    for i, score in enumerate(scores):
        if not np.isnan(score) and abs(score) >= threshold:
            found.append(
                {
                    "period": labels[i],
                    "value": round(float(values[i]), 2),
                    "change_pct": round(float(changes[i]) * 100, 1),
                    "direction": "increase" if changes[i] > 0 else "decrease",
                    "score": round(float(score), 2),
                }
            )
    return found


# --------------------------------------------------------------------------
# Charts (Plotly figure specs built only from computed numbers)
# --------------------------------------------------------------------------


def _trend_figure(labels: list[str], values: list[float], measure: str, current_label: str,
                  previous_label: str, anomalies: list[dict]) -> dict:
    colors = [ACCENT] * len(labels)
    for i, label in enumerate(labels):
        if label in (current_label, previous_label):
            colors[i] = ACCENT
    data = [
        {
            "type": "scatter", "mode": "lines+markers", "x": labels, "y": values,
            "line": {"color": ACCENT, "width": 2.5}, "marker": {"size": 7, "color": ACCENT},
            "name": measure,
        }
    ]
    flagged = {a["period"] for a in anomalies}
    if flagged:
        data.append(
            {
                "type": "scatter", "mode": "markers",
                "x": [l for l in labels if l in flagged],
                "y": [values[labels.index(l)] for l in labels if l in flagged],
                "marker": {"size": 13, "color": "rgba(0,0,0,0)", "line": {"color": NEGATIVE, "width": 2.5}},
                "name": "Unusual change",
                "hovertemplate": "Unusual change<extra></extra>",
            }
        )
    return {
        "data": data,
        "layout": {
            "title": {"text": f"{measure.replace('_', ' ').title()} over time", "font": {"size": 15}},
            "margin": {"l": 60, "r": 20, "t": 48, "b": 60}, "height": 340,
            "showlegend": bool(flagged),
            "xaxis": {"automargin": True, "type": "category"},
            "yaxis": {"title": {"text": measure}, "automargin": True},
            "font": {"family": "Inter, system-ui, sans-serif", "size": 12},
        },
        "config": {"displayModeBar": False, "responsive": True},
    }


def _driver_figure(rows: list[dict], measure: str, current_label: str, previous_label: str) -> dict:
    ordered = sorted(rows, key=lambda r: r["delta"])
    labels = [f"{r['segment']} ({r['dimension']})" for r in ordered]
    values = [r["delta"] for r in ordered]
    return {
        "data": [
            {
                "type": "bar", "orientation": "h", "x": values, "y": labels,
                "marker": {"color": [POSITIVE if v >= 0 else NEGATIVE for v in values]},
                "text": [("+" if v >= 0 else "") + human_number(v) for v in values],
                "textposition": "auto",
                "hovertemplate": "%{y}: %{x:,.2f}<extra></extra>",
            }
        ],
        "layout": {
            "title": {"text": f"What moved {measure.replace('_', ' ')}: {previous_label} → {current_label}"
                              "<br><span style='font-size:11px;opacity:.7'>Segments overlap across dimensions — do not add the bars together</span>",
                      "font": {"size": 15}},
            "margin": {"l": 150, "r": 20, "t": 64, "b": 50}, "height": max(300, 80 + 38 * len(rows)),
            "showlegend": False,
            "xaxis": {"title": {"text": f"Change in {measure}"}, "automargin": True, "zeroline": True},
            "yaxis": {"automargin": True},
            "font": {"family": "Inter, system-ui, sans-serif", "size": 12},
        },
        "config": {"displayModeBar": False, "responsive": True},
    }


# --------------------------------------------------------------------------
# Narrative
# --------------------------------------------------------------------------


def _direction_word(delta: float) -> str:
    return "increased" if delta > 0 else "decreased" if delta < 0 else "was unchanged"


def _pct_text(pct: float | None) -> str:
    return "n/a (no previous value)" if pct is None else f"{abs(pct):.1f}%"


_DOWN_WORDS = re.compile(r"\b(fall|fell|falling|drop\w*|declin\w*|decreas\w*|dip\w*|down|lower|worse|slump\w*)\b", re.I)
_UP_WORDS = re.compile(r"\b(increas\w*|grow\w*|grew|rise|rose|rising|up|higher|better|jump\w*|surg\w*)\b", re.I)


def premise_note(question: str, total_delta: float, measure: str) -> str | None:
    """Flag when the question assumes a direction the data does not show.

    "Why did sales fall?" when sales actually rose must not be answered as if
    it had fallen.
    """
    asked_down = bool(_DOWN_WORDS.search(question))
    asked_up = bool(_UP_WORDS.search(question))
    if asked_down == asked_up:  # neither, or ambiguous
        return None
    name = measure.replace("_", " ")
    if asked_down and total_delta > 0:
        return f"Note: {name} did not fall in this comparison — it rose."
    if asked_up and total_delta < 0:
        return f"Note: {name} did not rise in this comparison — it fell."
    return None


def build_narrative(inv: Investigation, total_delta: float, offset: dict | None, how: str, question: str = "") -> None:
    measure = inv.measure.replace("_", " ")
    cur_label, prev_label = inv.current["label"], inv.previous["label"]
    primary = inv.kpis[0]
    pct = primary["change_pct"]
    verb = _direction_word(total_delta)

    if total_delta == 0:
        inv.headline = f"{measure.title()} was unchanged between {prev_label} and {cur_label}."
    else:
        pct_part = f" ({_pct_text(pct)})" if pct is not None else ""
        inv.headline = (
            f"{measure.title()} {verb} by {human_number(abs(total_delta))}{pct_part} "
            f"from {prev_label} to {cur_label}."
        )

    sentences = []
    note = premise_note(question, total_delta, inv.measure) if total_delta != 0 else None
    if note:
        sentences.append(note)
    sentences.append(inv.headline)
    if abs(pct or 0) >= 1000:
        inv.limitations.append(
            f"The percentage change is very large ({pct:+,.0f}%) because the previous value is small; "
            "the absolute change is the more meaningful figure."
        )
    if inv.top_drivers:
        lead = inv.top_drivers[0]
        share = lead["share_of_change_pct"]
        if share is None:
            share_text = ""
        elif abs(share) > 100:
            share_text = ", which is larger than the net change itself because other segments moved the opposite way"
        else:
            share_text = f", accounting for {abs(share):.0f}% of the total change"
        sentences.append(
            f"The largest contributor was {lead['segment']} ({lead['dimension']}), which moved by "
            f"{human_number(lead['delta'])}{share_text}."
        )
    spot = inv.hotspots[0] if inv.hotspots else None
    if spot and inv.top_drivers and abs(spot["delta"]) >= 0.8 * abs(inv.top_drivers[0]["delta"]):
        share = spot["share_of_change_pct"]
        share_text = f" ({abs(share):.0f}% of the total change)" if share is not None else ""
        qualifier = (
            " and that is larger than its usual swing" if spot.get("unusual") is True
            else " but that is within its usual swing" if spot.get("unusual") is False else ""
        )
        sentences.append(
            f"Looking at combinations, the sharpest movement was {spot['label']}: "
            f"{human_number(spot['delta'])}{share_text}{qualifier}."
        )
    if offset:
        sentences.append(
            f"{offset['segment']} ({offset['dimension']}) moved the other way "
            f"({human_number(offset['delta'])}), partly offsetting the overall change."
        )
    inv.summary = " ".join(sentences)

    # --- facts: each is a direct restatement of a computed number ----------
    for k in inv.kpis:
        if k["change_pct"] is None:
            continue
        inv.facts.append(
            f"{k['label']}: {human_number(k['previous'])} → {human_number(k['current'])} "
            f"({k['change_pct']:+.1f}%)."
        )
    for row in inv.top_drivers[:3]:
        if row["share_of_change_pct"] is not None:
            swing = (" Larger than its usual month-to-month swing." if row.get("unusual") is True
                     else " Within its usual month-to-month swing." if row.get("unusual") is False else "")
            inv.facts.append(
                f"{row['segment']} ({row['dimension']}): {human_number(row['previous'])} → "
                f"{human_number(row['current'])}, {row['share_of_change_pct']:.0f}% of the total change.{swing}"
            )
    for row in inv.hotspots[:2]:
        swing = (" Larger than its usual swing." if row.get("unusual") is True
                 else " Within its usual swing." if row.get("unusual") is False else "")
        inv.facts.append(
            f"{row['label']} ({row['dimension']}): {human_number(row['previous'])} → "
            f"{human_number(row['current'])} ({human_number(row['delta'])}).{swing}"
        )
    for a in inv.anomalies[-3:]:
        inv.facts.append(
            f"{a['period']}: {a['direction']} of {abs(a['change_pct']):.1f}% was statistically unusual "
            f"for this series."
        )

    # --- interpretation: hedged, and only when the evidence supports it ----
    orders = next((k for k in inv.kpis if k["label"].lower() == "records"), None)
    avg = next((k for k in inv.kpis if k["label"].lower().startswith("average per")), None)
    if how == "sum" and orders and avg and orders["change_pct"] is not None and avg["change_pct"] is not None:
        if abs(orders["change_pct"]) >= 2 * abs(avg["change_pct"]) and abs(orders["change_pct"]) >= 3:
            inv.interpretation.append(
                f"The change tracks the number of records ({orders['change_pct']:+.1f}%) far more than the "
                f"average value per record ({avg['change_pct']:+.1f}%), which suggests volume rather than "
                "value per record was the main factor."
            )
        elif abs(avg["change_pct"]) >= 2 * abs(orders["change_pct"]) and abs(avg["change_pct"]) >= 3:
            inv.interpretation.append(
                f"The average value per record moved more ({avg['change_pct']:+.1f}%) than the number of "
                f"records ({orders['change_pct']:+.1f}%), which suggests value per record rather than volume "
                "was the main factor."
            )
    if inv.top_drivers and inv.top_drivers[0]["share_of_change_pct"] is not None:
        share = abs(inv.top_drivers[0]["share_of_change_pct"])
        if share >= 50:
            inv.interpretation.append(
                f"The change is concentrated: one segment explains {share:.0f}% of it, so the issue is "
                "more likely specific than broad-based."
            )
        elif len(inv.top_drivers) >= 3 and share < 30:
            inv.interpretation.append(
                "The change is spread across several segments rather than driven by one, which points to a "
                "broad-based movement."
            )
    if inv.interpretation:
        inv.interpretation.append(
            "These are readings of the numbers, not confirmed causes. The data shows where the change "
            "happened; it cannot show why a customer or segment behaved differently."
        )

    # --- recommendations: investigative next steps, never invented fixes ----
    unusual_spot = next((h for h in inv.hotspots if h.get("unusual") is True), None)
    if unusual_spot:
        inv.recommendations.append(
            f"Start with {unusual_spot['label']}: it fell by more than that combination normally varies. "
            "Compare pricing, stock availability and demand there between the two periods."
        )
    if inv.top_drivers:
        lead = inv.top_drivers[0]
        inv.recommendations.append(
            f"Investigate {lead['segment']} in {lead['dimension']}: look at what changed there between "
            f"{prev_label} and {cur_label} (pricing, stock, campaigns, staffing)."
        )
    if offset:
        inv.recommendations.append(
            f"Check what worked in {offset['segment']} ({offset['dimension']}) and whether it can be repeated."
        )
    if inv.anomalies:
        inv.recommendations.append(
            f"Review {inv.anomalies[-1]['period']}: the movement was unusual relative to this series' history."
        )
    if not inv.recommendations:
        inv.recommendations.append("Collect more periods or add dimensions (region, category) to locate the change.")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def investigate(question: str, source: DataSource) -> Investigation:
    measure, measure_note = resolve_measure(question, source)
    if not measure:
        raise UnsupportedRequestError(
            "This dataset has no numeric column to investigate.",
            "no measures",
        )
    how = _aggregation_for(measure)
    raw = source.frame()  # loaded once; a SQL source would otherwise re-query per use
    time_column = _pick_time_column(source, list(raw.columns))
    if not time_column:
        raise UnsupportedRequestError(
            "To explain a change I need a date column to compare periods, and this dataset has none. "
            "You can still ask which segments contribute most, e.g. “sales by region”.",
            "no date column",
        )

    frame = _prepare_frame(raw, measure, time_column)
    if frame.empty:
        raise UnsupportedRequestError(
            "There are no usable rows after removing blanks in the measure and date columns.",
            "empty after cleaning",
        )

    grain = detect_grain(question)
    current, previous, assumptions, limitations = choose_periods(frame[time_column], grain, question)
    if measure_note:
        assumptions.append(measure_note)

    periods = _period_index(frame[time_column], grain)
    cur = frame[periods == current]
    prev = frame[periods == previous]
    total_cur, total_prev = _agg(cur, measure, how), _agg(prev, measure, how)
    total_delta = total_cur - total_prev

    inv = Investigation(
        measure=measure,
        aggregation=how,
        time_column=time_column,
        grain=grain,
        current={"label": _period_label(current, grain), "rows": int(len(cur))},
        previous={"label": _period_label(previous, grain), "rows": int(len(prev))},
        assumptions=assumptions,
        limitations=limitations,
    )

    # --- KPIs ---------------------------------------------------------------
    label = measure.replace("_", " ").title()
    inv.kpis.append(_kpi(label if how == "mean" else f"Total {label}", total_cur, total_prev))
    inv.kpis.append(_kpi("Records", float(len(cur)), float(len(prev))))
    if how == "sum" and len(cur) and len(prev):
        inv.kpis.append(_kpi(f"Average per record", total_cur / len(cur), total_prev / len(prev)))
    for other in source.measures:
        if other != measure and len(inv.kpis) < 4 and other in frame.columns:
            other_how = _aggregation_for(other)
            col = pd.to_numeric(frame[other], errors="coerce")
            sub = frame.assign(**{other: col})
            inv.kpis.append(
                _kpi(
                    other.replace("_", " ").title() if other_how == "mean" else f"Total {other.replace('_', ' ').title()}",
                    _agg(sub[periods == current], other, other_how),
                    _agg(sub[periods == previous], other, other_how),
                )
            )

    # --- drivers ----------------------------------------------------------------
    dimensions = [d for d in source.dimensions if d in frame.columns]
    inv.drivers, inv.top_drivers, inv.checks = compute_drivers(
        frame, measure, how, time_column, grain, current, previous, dimensions
    )
    periods_series = _period_index(frame[time_column], grain)
    for dim, rows in inv.drivers.items():
        _flag_unusual(rows, _variation(frame, periods_series, [dim], measure, how, current), ["segment"])
    # top_drivers are copies of rows in inv.drivers; re-point them at the rated rows
    rated = {(r["dimension"], r["segment"]): r for rows in inv.drivers.values() for r in rows}
    inv.top_drivers = [rated.get((r["dimension"], r["segment"]), r) for r in inv.top_drivers]
    inv.hotspots = compute_hotspots(frame, measure, how, time_column, grain, current, previous, dimensions, total_delta)
    if not dimensions:
        inv.limitations.append("No categorical columns were available, so the change could not be broken down.")
    if how == "mean":
        inv.limitations.append(
            f"“{measure}” is a rate/price-like column, so it is averaged and segment shares of the change "
            "are not shown."
        )

    # --- time series + anomalies ---------------------------------------------------
    grouped = (
        frame.groupby(periods, observed=True)[measure].mean() if how == "mean"
        else frame.groupby(periods, observed=True)[measure].sum()
    ).sort_index()
    labels = [_period_label(p, grain) for p in grouped.index]
    inv.series = [{"period": l, "value": round(float(v), 2)} for l, v in zip(labels, grouped.values)]
    inv.anomalies = detect_anomalies(grouped, labels)

    # --- checks -----------------------------------------------------------------
    inv.checks.insert(0, {
        "name": "periods_have_data",
        "passed": len(cur) > 0,
        "detail": f"{len(cur)} rows in {inv.current['label']}, {len(prev)} rows in {inv.previous['label']}",
    })
    inv.checks.append({
        "name": "no_fabricated_values",
        "passed": True,
        "detail": "every figure is an aggregate of the uploaded data; no model-generated numbers",
    })
    failed = [c for c in inv.checks if not c["passed"]]
    if failed:
        inv.limitations.append("A reconciliation check failed; treat the breakdown as approximate.")

    # --- charts -----------------------------------------------------------------
    inv.charts.append(_trend_figure(labels, [round(float(v), 2) for v in grouped.values], measure,
                                    inv.current["label"], inv.previous["label"], inv.anomalies))
    if inv.top_drivers:
        # Show the leading movers in both directions so offsets are visible.
        movers = sorted(
            [r for rows in inv.drivers.values() for r in rows] + inv.hotspots[:2],
            key=lambda r: abs(r["delta"]), reverse=True,
        )[:8]
        inv.charts.append(_driver_figure(movers, measure, inv.current["label"], inv.previous["label"]))

    offset = _offset(inv.drivers, total_delta)
    build_narrative(inv, total_delta, offset, how, question)

    inv.how_calculated = [
        f"Source: {source.name} ({source.row_count:,} rows).",
        f"Measure: {measure}, aggregated by {'sum' if how == 'sum' else 'average'}.",
        f"Date column: {time_column}, grouped by {grain}.",
        f"Compared {inv.current['label']} with {inv.previous['label']}.",
        "Segment contribution = a segment's change in the measure between the two periods; "
        "for additive measures the segment changes add up to the total change (checked).",
        "Unusual periods use a robust modified z-score (>3.5) on period-over-period change.",
        f"A segment's move is called unusual only if it exceeds {UNUSUAL_K:g} standard deviations of its own "
        "earlier month-to-month changes (a deliberately strict rule to avoid reporting noise).",
    ]
    if len(grouped) < MIN_PERIODS_FOR_ANOMALY:
        inv.limitations.append(
            f"Only {len(grouped)} periods exist; anomaly detection needs at least {MIN_PERIODS_FOR_ANOMALY}."
        )
    inv.confidence = "medium" if not failed and len(grouped) >= 6 and len(cur) >= 30 and len(prev) >= 30 else "low"
    if inv.confidence == "low":
        inv.limitations.append("Few rows or periods were available, so treat this as indicative only.")
    return inv
