"""Plotly figure builders for the auto dashboard. Inputs are already-computed pandas results; values are
passed through, never altered. Styling reuses the analysis agent's layout so every chart looks like one system."""

from __future__ import annotations

from typing import Any

import numpy as np

from app.agent import viz
from app.agent.viz import AMBER, BLUE, GREEN, PALETTE, RED
from app.utils.helpers import human_number

readable = viz._readable


def f(x: Any):
    """JSON-safe float (NaN/inf -> None)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


def _wrap(data, layout, kind, purpose, **meta):
    return viz._fig(data, layout, kind, purpose, **meta)["figure"]


def line(series: list[tuple[str, list[str], list[float]]], title: str, *, area: bool = False, unusual: list[dict] | None = None,
         subtitle: str | None = None, y_title: str = "") -> dict:
    data = []
    for i, (name, xs, ys) in enumerate(series):
        color = PALETTE[i % len(PALETTE)]
        trace = {"type": "scatter", "mode": "lines+markers", "x": xs, "y": [f(v) for v in ys], "name": name,
                 "line": {"color": color, "width": 2.5}, "marker": {"size": 5},
                 "hovertemplate": f"%{{x}}<br>{name}: %{{y:,.2f}}<extra></extra>"}
        if area and len(series) == 1:
            trace.update({"fill": "tozeroy", "fillcolor": "rgba(47,90,224,0.12)"})
        data.append(trace)
    if unusual:
        data.append({"type": "scatter", "mode": "markers", "x": [u["label"] for u in unusual], "y": [u["value"] for u in unusual],
                     "name": "Unusual period", "marker": {"color": RED, "size": 11, "symbol": "diamond"},
                     "hovertemplate": "%{x}<br>%{y:,.2f}<br>unusual vs other periods<extra></extra>"})
    layout = viz._layout(title, subtitle, x="", y=y_title or (series[0][0] if len(series) == 1 else ""), legend=len(data) > 1, height=380)
    return _wrap(data, layout, "area" if area else "line", "trend")


def bars(labels: list[str], values: list[float], title: str, measure_label: str, *, horizontal: bool = True,
         shares: list[float | None] | None = None, subtitle: str | None = None, color: str = BLUE) -> dict:
    labels = [str(l) for l in labels]
    custom = [[s if s is not None else None] for s in (shares or [None] * len(values))]
    tmpl = "%{y}" if horizontal else "%{x}"
    hover = f"{tmpl}<br>{measure_label}: %{{value:,.2f}}" if False else None  # kept simple below
    if horizontal:
        data = [{"type": "bar", "orientation": "h", "x": [f(v) for v in values][::-1], "y": labels[::-1], "marker": {"color": color},
                 "customdata": custom[::-1], "text": [human_number(v) for v in values][::-1], "textposition": "auto",
                 "hovertemplate": ("%{y}<br>" + measure_label + ": %{x:,.2f}" + ("<br>%{customdata[0]:.1f}% of shown<extra></extra>" if shares else "<extra></extra>"))}]
        layout = viz._layout(title, subtitle, x=measure_label, y="", height=max(300, 120 + 30 * len(labels)))
        layout["yaxis"] = {"automargin": True}
        layout["xaxis"]["tickformat"] = "~s"
    else:
        data = [{"type": "bar", "x": labels, "y": [f(v) for v in values], "marker": {"color": color}, "customdata": custom,
                 "hovertemplate": ("%{x}<br>" + measure_label + ": %{y:,.2f}" + ("<br>%{customdata[0]:.1f}% of shown<extra></extra>" if shares else "<extra></extra>"))}]
        layout = viz._layout(title, subtitle, x="", y=measure_label, height=360)
    return _wrap(data, layout, "horizontal_bar" if horizontal else "column", "ranking")


def grouped(labels: list[str], series: list[tuple[str, list[float]]], title: str) -> dict:
    data = [{"type": "bar", "name": n, "x": [str(l) for l in labels], "y": [f(v) for v in vs], "marker": {"color": PALETTE[i % len(PALETTE)]},
             "hovertemplate": f"%{{x}}<br>{n}: %{{y:,.2f}}<extra></extra>"} for i, (n, vs) in enumerate(series)]
    layout = viz._layout(title, None, x="", y="", legend=True, height=380)
    layout["barmode"] = "group"
    return _wrap(data, layout, "grouped_bar", "comparison")


def donut(labels: list[str], values: list[float], title: str) -> dict:
    data = [{"type": "pie", "labels": [str(l) for l in labels], "values": [f(v) for v in values], "hole": 0.55, "sort": False,
             "marker": {"colors": PALETTE}, "textinfo": "label+percent",
             "hovertemplate": "%{label}<br>%{value:,.2f} (%{percent})<extra></extra>"}]
    return _wrap(data, viz._layout(title, None, legend=True, height=360), "donut", "composition")


def treemap(labels: list[str], parents: list[str], values: list[float], title: str, *, sunburst: bool = False) -> dict:
    data = [{"type": "sunburst" if sunburst else "treemap", "labels": labels, "parents": parents, "values": [f(v) for v in values],
             "branchvalues": "total", "marker": {"colors": None}, "textinfo": "label+percent root",
             "hovertemplate": "%{label}<br>%{value:,.2f}<br>%{percentRoot:.1%} of total<extra></extra>"}]
    layout = viz._layout(title, None, height=420)
    layout["margin"] = {"l": 8, "r": 8, "t": 52, "b": 8}
    layout.pop("xaxis", None); layout.pop("yaxis", None)
    return _wrap(data, layout, "sunburst" if sunburst else "treemap", "composition")


def heatmap(xs: list[str], ys: list[str], z: list[list[float | None]], title: str, measure_label: str) -> dict:
    data = [{"type": "heatmap", "x": xs, "y": ys, "z": z, "colorscale": [[0, "#EAF0FE"], [1, "#2F5AE0"]],
             "hovertemplate": "%{y} · %{x}<br>" + measure_label + ": %{z:,.2f}<extra></extra>", "colorbar": {"thickness": 12}}]
    layout = viz._layout(title, None, height=max(320, 120 + 32 * len(ys)))
    layout["yaxis"] = {"automargin": True}
    return _wrap(data, layout, "heatmap", "performance")


def scatter(xs: list[float], ys: list[float], xname: str, yname: str, r: float, strength: str, n: int, sampled: bool) -> dict:
    x, y = np.asarray(xs, float), np.asarray(ys, float)
    slope, intercept = np.polyfit(x, y, 1)
    lo, hi = float(x.min()), float(x.max())
    data = [{"type": "scatter", "mode": "markers", "x": x.tolist(), "y": y.tolist(), "name": "Records",
             "marker": {"color": BLUE, "size": 6, "opacity": 0.55},
             "hovertemplate": f"{readable(xname)} %{{x:,.2f}}<br>{readable(yname)} %{{y:,.2f}}<extra></extra>"},
            {"type": "scatter", "mode": "lines", "x": [lo, hi], "y": [slope * lo + intercept, slope * hi + intercept], "name": "Linear fit",
             "line": {"color": RED, "width": 2, "dash": "dash"}, "hoverinfo": "skip"}]
    sub = f"{strength.capitalize()} association (r = {r:.2f}, {n:,} records{', sample plotted' if sampled else ''})"
    layout = viz._layout(f"{readable(yname)} vs {readable(xname)}", sub, x=readable(xname), y=readable(yname), legend=True, height=380)
    return _wrap(data, layout, "scatter", "relationship")


def histogram(edges: list[float], counts: list[int], measure: str, median: float) -> dict:
    labels = [f"{human_number(edges[i])}–{human_number(edges[i + 1])}" for i in range(len(counts))]
    data = [{"type": "bar", "x": labels, "y": counts, "marker": {"color": BLUE}, "hovertemplate": "%{x}<br>%{y:,} records<extra></extra>"}]
    layout = viz._layout(f"Distribution of {readable(measure)}", f"Median {human_number(median)}", x=readable(measure), y="Records", height=340)
    layout["yaxis"]["tickformat"] = ","
    layout["bargap"] = 0.04
    return _wrap(data, layout, "histogram", "distribution")


def box(groups: list[str], stats: list[dict], measure: str, dim: str) -> dict:
    data = [{"type": "box", "name": str(g), "q1": [s["q1"]], "median": [s["median"]], "q3": [s["q3"]], "lowerfence": [s["lo"]],
             "upperfence": [s["hi"]], "mean": [s["mean"]], "marker": {"color": PALETTE[i % len(PALETTE)]}, "boxmean": True}
            for i, (g, s) in enumerate(zip(groups, stats))]
    layout = viz._layout(f"{readable(measure)} by {readable(dim)}", "Box = middle 50% of records; line = median; whiskers = typical range",
                         x="", y=readable(measure), legend=False, height=380)
    return _wrap(data, layout, "box", "distribution")


def waterfall(steps: list[tuple[str, float, str]], title: str) -> dict:
    data = [{"type": "waterfall", "x": [s[0] for s in steps], "y": [f(s[1]) for s in steps], "measure": [s[2] for s in steps],
             "connector": {"line": {"color": "#8A94A6"}}, "increasing": {"marker": {"color": GREEN}}, "decreasing": {"marker": {"color": RED}},
             "totals": {"marker": {"color": BLUE}}, "hovertemplate": "%{x}<br>%{y:,.2f}<extra></extra>"}]
    return _wrap(data, viz._layout(title, None, height=360), "waterfall", "performance")


def gauge(actual: float, target: float, title: str, measure: str) -> dict:
    data = [{"type": "indicator", "mode": "gauge+number+delta", "value": f(actual), "delta": {"reference": f(target), "relative": False},
             "gauge": {"axis": {"range": [0, max(f(target) or 0, f(actual) or 0) * 1.15]}, "bar": {"color": BLUE},
                       "threshold": {"line": {"color": RED, "width": 3}, "thickness": 0.8, "value": f(target)}}}]
    layout = viz._layout(title, f"Actual {readable(measure)} against the target column in the data", height=320)
    layout.pop("xaxis", None); layout.pop("yaxis", None)
    return _wrap(data, layout, "gauge", "performance")


def funnel(stages: list[str], values: list[float], title: str) -> dict:
    data = [{"type": "funnel", "y": stages, "x": [f(v) for v in values], "marker": {"color": BLUE}, "textinfo": "value+percent initial"}]
    layout = viz._layout(title, None, height=360)
    layout.pop("xaxis", None); layout.pop("yaxis", None)
    return _wrap(data, layout, "funnel", "performance")


def sankey(sources: list[str], targets: list[str], values: list[float], title: str) -> dict:
    nodes = list(dict.fromkeys(sources + targets))
    idx = {n: i for i, n in enumerate(nodes)}
    data = [{"type": "sankey", "node": {"label": nodes, "pad": 14, "thickness": 14},
             "link": {"source": [idx[s] for s in sources], "target": [idx[t] for t in targets], "value": [f(v) for v in values]}}]
    layout = viz._layout(title, None, height=420)
    layout.pop("xaxis", None); layout.pop("yaxis", None)
    return _wrap(data, layout, "sankey", "composition")
