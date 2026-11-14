"""Visualization agent for the analysis loop.

Builds Plotly figures from *computed tool results only*; no value is ever typed in by a
model. Charts are chosen from the shape of the result (time series, ranking, contribution,
composition, relationship, distribution) and styled for a product UI: formatted numbers,
tooltips, sensible sorting, no 3D, a restrained palette, transparent backgrounds so the
front end can apply light/dark themes (static/js/charts.js), responsive sizing.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from app.utils.helpers import human_number

BLUE, GREEN, RED, AMBER, GREY = "#2F5AE0", "#128A6E", "#C4622D", "#9A7B12", "#8A94A6"
PALETTE = [BLUE, GREEN, RED, "#6D4AC4", "#0E7490", "#B2295C", "#4B5563", AMBER]
MAX_BARS = 12


def _title(text: str, subtitle: str | None = None) -> dict:
    full = f"{text}<br><span style='font-size:11px;opacity:.7'>{subtitle}</span>" if subtitle else text
    return {"text": full, "font": {"size": 15}, "x": 0.01, "xanchor": "left"}


def _layout(title: str, subtitle: str | None = None, *, x: str | None = None, y: str | None = None,
            height: int = 360, legend: bool = False, **extra: Any) -> dict:
    layout = {
        "title": _title(title, subtitle),
        "height": height,
        "margin": {"l": 56, "r": 20, "t": 64 if subtitle else 52, "b": 56},
        "paper_bgcolor": "rgba(0,0,0,0)", "plot_bgcolor": "rgba(0,0,0,0)",
        "font": {"family": "Inter, system-ui, sans-serif", "size": 12},
        "showlegend": legend,
        "legend": {"orientation": "h", "y": -0.2},
        "hoverlabel": {"font": {"size": 12}},
        "xaxis": {"title": {"text": x or ""}, "automargin": True},
        "yaxis": {"title": {"text": y or ""}, "automargin": True, "tickformat": "~s"},
    }
    layout.update(extra)
    return layout


def _fig(data: list, layout: dict, kind: str, purpose: str, **meta: Any) -> dict:
    return {
        "figure": {"data": data, "layout": layout, "config": {"displayModeBar": False, "responsive": True}},
        "spec": {"chart_type": kind, "purpose": purpose, **meta},
        "point_count": sum(len(t.get("x", t.get("values", []))) for t in data),
    }


def _readable(name: str) -> str:
    return str(name).replace("_", " ").strip().title()


def trend(labels: list[str], values: list[float], measure: str, *, anomalies: list[dict] | None = None,
          highlight: tuple[str, str] | None = None, subtitle: str | None = None) -> dict | None:
    if len(labels) < 2:
        return None
    data = [{
        "type": "scatter", "mode": "lines+markers", "x": labels, "y": values, "name": _readable(measure),
        "line": {"color": BLUE, "width": 2.5, "shape": "spline", "smoothing": 0.4}, "marker": {"size": 6},
        "hovertemplate": "%{x}<br>%{y:,.2f}<extra></extra>",
    }]
    flagged = {a["period"] for a in (anomalies or [])}
    if flagged:
        pts = [(l, v) for l, v in zip(labels, values) if l in flagged]
        data.append({"type": "scatter", "mode": "markers", "x": [p[0] for p in pts], "y": [p[1] for p in pts],
                     "name": "Unusual period", "marker": {"color": RED, "size": 11, "symbol": "diamond"},
                     "hovertemplate": "%{x}: unusual<br>%{y:,.2f}<extra></extra>"})
    layout = _layout(f"{_readable(measure)} over time", subtitle, x="Period", y=_readable(measure), legend=bool(flagged))
    if highlight:
        layout["shapes"] = [{"type": "rect", "xref": "x", "yref": "paper", "x0": highlight[0], "x1": highlight[1],
                             "y0": 0, "y1": 1, "fillcolor": "rgba(47,90,224,0.08)", "line": {"width": 0}, "layer": "below"}]
    return _fig(data, layout, "line", "trend", series=len(data))


def contribution(rows: list[dict], measure: str, dimension: str, current_label: str, previous_label: str,
                 filters: dict | None = None) -> dict | None:
    """Diverging horizontal bars: which segments moved the measure, and in which direction."""
    rows = [r for r in rows if r.get("delta")]
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: abs(r["delta"]), reverse=True)[:MAX_BARS]
    rows = sorted(rows, key=lambda r: r["delta"])  # most negative at the bottom → reads top-down as biggest gain first
    where = (" in " + ", ".join(f"{v}" for v in (filters or {}).values())) if filters else ""
    data = [{
        "type": "bar", "orientation": "h", "x": [r["delta"] for r in rows], "y": [r["segment"] for r in rows],
        "marker": {"color": [GREEN if r["delta"] > 0 else RED for r in rows]},
        "text": [human_number(r["delta"]) for r in rows], "textposition": "auto",
        "customdata": [[r["previous"], r["current"], r.get("delta_pct")] for r in rows],
        "hovertemplate": "%{y}<br>%{customdata[0]:,.2f} → %{customdata[1]:,.2f}<br>change %{x:,.2f}<extra></extra>",
    }]
    layout = _layout(f"What moved {_readable(measure)} by {_readable(dimension)}{where}",
                     f"Change from {previous_label} to {current_label}", x=f"Change in {_readable(measure)}", y="",
                     height=max(300, 90 + 34 * len(rows)))
    layout["yaxis"] = {"automargin": True}
    layout["xaxis"]["tickformat"] = "~s"
    layout["xaxis"]["zeroline"] = True
    return _fig(data, layout, "horizontal_bar", "contribution", dimension=dimension)


def comparison(periods: list[str], a: dict, b: dict) -> dict | None:
    """Two measures from two sources over the same periods (own axes: the scales differ)."""
    if len(periods) < 2:
        return None
    data = [
        {"type": "scatter", "mode": "lines+markers", "x": periods, "y": a["values"], "name": f"{_readable(a['measure'])} ({a['source']})",
         "line": {"color": BLUE, "width": 2.5}, "hovertemplate": "%{x}<br>%{y:,.2f}<extra></extra>"},
        {"type": "scatter", "mode": "lines+markers", "x": periods, "y": b["values"], "yaxis": "y2",
         "name": f"{_readable(b['measure'])} ({b['source']})", "line": {"color": AMBER, "width": 2.5, "dash": "dot"},
         "hovertemplate": "%{x}<br>%{y:,.2f}<extra></extra>"},
    ]
    layout = _layout("Two sources over time", "Each series is aggregated inside its own source; separate axes", x="Period",
                     y=_readable(a["measure"]), legend=True)
    layout["yaxis2"] = {"overlaying": "y", "side": "right", "tickformat": "~s", "showgrid": False,
                        "title": {"text": _readable(b["measure"])}}
    layout["margin"]["r"] = 56
    return _fig(data, layout, "dual_line", "cross_source")


def relationship(points: list[dict], x: str, y: str, pearson: float, strength: str) -> dict | None:
    if len(points) < 8:
        return None
    xs = np.array([p["x"] for p in points], dtype=float)
    ys = np.array([p["y"] for p in points], dtype=float)
    slope, intercept = np.polyfit(xs, ys, 1)
    lo, hi = float(xs.min()), float(xs.max())
    data = [
        {"type": "scatter", "mode": "markers", "x": xs.tolist(), "y": ys.tolist(), "name": "Records",
         "marker": {"color": BLUE, "size": 7, "opacity": 0.6}, "hovertemplate": f"{_readable(x)} %{{x:,.2f}}<br>{_readable(y)} %{{y:,.2f}}<extra></extra>"},
        {"type": "scatter", "mode": "lines", "x": [lo, hi], "y": [slope * lo + intercept, slope * hi + intercept],
         "name": "Linear fit", "line": {"color": RED, "width": 2, "dash": "dash"}, "hoverinfo": "skip"},
    ]
    layout = _layout(f"{_readable(y)} vs {_readable(x)}", f"{strength.capitalize()} relationship (r = {pearson:.2f})",
                     x=_readable(x), y=_readable(y), legend=True)
    return _fig(data, layout, "scatter", "relationship")


def distribution(hist: list[dict], measure: str, median: float) -> dict | None:
    if not hist:
        return None
    labels = [f"{human_number(h['low'])}–{human_number(h['high'])}" for h in hist]
    data = [{"type": "bar", "x": labels, "y": [h["count"] for h in hist], "marker": {"color": BLUE},
             "hovertemplate": "%{x}<br>%{y} records<extra></extra>"}]
    layout = _layout(f"Distribution of {_readable(measure)}", f"Median {human_number(median)}", x=_readable(measure), y="Records")
    layout["yaxis"]["tickformat"] = ","
    layout["bargap"] = 0.04
    return _fig(data, layout, "histogram", "distribution")


def composition(labels: list[str], values: list[float], measure: str) -> dict | None:
    pairs = [(l, v) for l, v in zip(labels, values) if v and v > 0]
    if len(pairs) < 2:
        return None
    pairs.sort(key=lambda p: p[1], reverse=True)
    if len(pairs) <= 6:
        data = [{"type": "pie", "labels": [p[0] for p in pairs], "values": [p[1] for p in pairs], "hole": 0.55,
                 "marker": {"colors": PALETTE}, "textinfo": "label+percent", "sort": False,
                 "hovertemplate": "%{label}<br>%{value:,.2f} (%{percent})<extra></extra>"}]
        return _fig(data, _layout(f"{_readable(measure)} composition", legend=True), "donut", "composition")
    pairs = pairs[:MAX_BARS][::-1]
    data = [{"type": "bar", "orientation": "h", "x": [p[1] for p in pairs], "y": [p[0] for p in pairs],
             "marker": {"color": BLUE}, "text": [human_number(p[1]) for p in pairs], "textposition": "auto"}]
    layout = _layout(f"{_readable(measure)} by group", x=_readable(measure), y="", height=max(300, 90 + 30 * len(pairs)))
    layout["yaxis"] = {"automargin": True}
    return _fig(data, layout, "horizontal_bar", "ranking")


def issues_bar(issues: list[dict]) -> dict | None:
    rows = [(f"{i.get('column', 'all columns')} · {i['kind'].replace('_', ' ')}", i.get("pct") or i.get("count") or 0) for i in issues]
    rows = [r for r in rows if r[1]][:MAX_BARS][::-1]
    if not rows:
        return None
    data = [{"type": "bar", "orientation": "h", "x": [r[1] for r in rows], "y": [r[0] for r in rows],
             "marker": {"color": AMBER}, "hovertemplate": "%{y}: %{x}<extra></extra>"}]
    layout = _layout("Data quality findings", "Share of rows affected (%) where available", x="Affected", y="",
                     height=max(280, 80 + 32 * len(rows)))
    layout["yaxis"] = {"automargin": True}
    return _fig(data, layout, "horizontal_bar", "quality")


def dedupe(figures: list[dict]) -> list[dict]:
    """Drop charts that would repeat each other (same purpose + title)."""
    seen, out = set(), []
    for f in figures:
        key = (f["spec"].get("purpose"), (f["figure"]["layout"].get("title") or {}).get("text"))
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out
