"""Visualization decision engine + dashboard composition.

For every visualization the engine either *builds it from the data* or records, in plain language, why the data
does not support it (with what to try). Nothing is created merely because a chart type is available."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.dashboard_ai import analysis as A
from app.dashboard_ai import figures as F
from app.dashboard_ai.profiler import ADDITIVE_ROLES, Dim, Measure, Profile
from app.utils.helpers import human_number

MAX_DIM_CHARTS = 3
MAX_FILTER_DIMS = 6
FUNNEL_WORDS = ["lead", "prospect", "visit", "qualif", "signup", "sign_up", "proposal", "trial", "order", "purchase", "won", "closed"]


def slug(*parts: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-")


@dataclass
class Ctx:
    frame: pd.DataFrame
    prof: Profile
    source: object
    primary: Measure
    charts: list[dict] = field(default_factory=list)
    unavailable: list[dict] = field(default_factory=list)
    trend_series: pd.Series | None = None
    trend_unusual: list = field(default_factory=list)
    relationships: list = field(default_factory=list)


def _how(ctx: Ctx, operation: str, rows: int | None = None) -> dict:
    return {"operation": operation, "rows": int(len(ctx.frame) if rows is None else rows), "engine": "Pandas analytics",
            "dataset": ctx.source.name}


def _op(m: Measure, dim: str | None = None, extra: str = "") -> str:
    fn = "AVG" if m.agg == "mean" else "SUM"
    return f"{fn}({m.name})" + (f" GROUP BY {dim}" if dim else "") + extra


def _add(ctx: Ctx, **chart) -> dict:
    chart.setdefault("wide", False)
    chart.setdefault("variants", None)
    chart.setdefault("drill", None)
    chart.setdefault("explain", [])
    ctx.charts.append(chart)
    return chart


def _skip(ctx: Ctx, kind: str, title: str, why: str, tries: list[str], action: dict | None = None) -> None:
    ctx.unavailable.append({"kind": kind, "title": title, "why": why, "try": tries, "action": action})


def _table(columns: list[str], rows: list[list]) -> dict:
    return {"columns": columns, "rows": [dict(zip(columns, r)) for r in rows]}


# ----------------------------------------------------------------------------------------- explanations

def _explain_rank(df: pd.DataFrame, m: Measure, dim: str) -> list[str]:
    top, low = df.iloc[0], df.iloc[-1]
    out = [f"{top['label']} has the highest {m.label.lower()} at {A.fmt(top['value'])}"
           + (f" ({top['share']:.1f}% of the groups shown)." if pd.notna(top["share"]) else ".")]
    if len(df) > 1:
        out.append(f"{low['label']} has the lowest at {A.fmt(low['value'])}.")
        if low["value"] > 0 and top["value"] / low["value"] >= 1.5:
            out.append(f"The highest is {top['value'] / low['value']:.1f}× the lowest.")
    out.append("This shows how the figure is distributed across groups; it does not by itself explain why.")
    return out


def _explain_trend(s: pd.Series, m: Measure) -> list[str]:
    hi, lo = s.idxmax(), s.idxmin()
    out = [f"The highest period is {hi} at {A.fmt(s[hi])}; the lowest is {lo} at {A.fmt(s[lo])}."]
    first, last = float(s.iloc[0]), float(s.iloc[-1])
    if first:
        pct = (last - first) / abs(first) * 100
        word = "higher" if pct > 0 else "lower" if pct < 0 else "unchanged"
        out.append(f"From {s.index[0]} to {s.index[-1]} it moved from {A.fmt(first)} to {A.fmt(last)} ({abs(pct):.1f}% {word}).")
    out.append("A single high or low period is a prompt to look closer, not a trend on its own.")
    return out


# ----------------------------------------------------------------------------------------- chart builders

def build_trends(ctx: Ctx) -> None:
    prof, p = ctx.prof, ctx.primary
    if not prof.date:
        _skip(ctx, "trend", "Trend over time", "No usable date column was found (or it covers fewer than 3 months).",
              ["Upload data that includes an order or transaction date", "Check that the date column is formatted as a date"],
              {"label": "Explore dataset", "href": f"/datasets/{ctx.source.id}"})
        return
    series, dropped = A.period_series(ctx.frame, prof, p)
    if len(series) < 3:
        _skip(ctx, "trend", "Trend over time", f"Only {len(series)} complete period(s) are available after filtering; a trend needs at least 3.",
              ["Expand the date range", "Clear filters"], {"label": "Clear filters", "kind": "clear-filters"})
        return
    unusual = A.unusual_periods(series)
    labels = [A.label(i) for i in series.index]
    rev = prof.measure("revenue")
    prof_m = prof.measure("profit")
    multi = rev and prof_m and rev.agg == prof_m.agg and p.name in (rev.name, prof_m.name)
    note = "The newest period was left out because the data ends part-way through it." if dropped else None
    sub = note
    if multi:
        s2, _ = A.period_series(ctx.frame, prof, prof_m if p.name == rev.name else rev)
        s2 = s2.reindex(series.index)
        fig = F.line([(p.label, labels, series.tolist()), ((prof_m if p.name == rev.name else rev).label, labels, s2.tolist())],
                     f"{rev.label} and {prof_m.label} over time", subtitle=sub, unusual=unusual if p.name == rev.name else None)
        table = _table(["Period", rev.label, prof_m.label], [[l, F.f(a), F.f(b)] for l, a, b in zip(labels, series.tolist() if p.name == rev.name else s2.tolist(), s2.tolist() if p.name == rev.name else series.tolist())])
        kind, title = "multi_line", f"{rev.label} and {prof_m.label} over time"
    else:
        area = p.role in ("quantity",) or p.agg == "sum" and False
        fig = F.line([(p.label, labels, series.tolist())], f"{p.label} over time", area=area, unusual=unusual, subtitle=sub)
        table = _table(["Period", p.label], [[l, F.f(v)] for l, v in zip(labels, series.tolist())])
        kind, title = ("area" if area else "line"), f"{p.label} over time"
    grain = prof.date.grain
    _add(ctx, id=slug("trend", p.name), tab="overview", kind=kind, title=title, role="hero", figure=fig, table=table,
         explain=_explain_trend(series, p), wide=True,
         how=_how(ctx, _op(p, f"{grain} of {prof.date.column}"), len(ctx.frame)),
         open_q=f"Show {grain}ly {p.name} trend", ask_q=f"Analyze the {p.name} trend over time shown in this chart.",
         why_chosen=f"A date column ({prof.date.column}) covering {prof.date.months} months and a numeric measure exist.")
    ctx.trend_series = series  # type: ignore[attr-defined]
    ctx.trend_unusual = unusual  # type: ignore[attr-defined]
    # other measures get their own trend (up to 2), area for volume-like measures
    others = [m for m in prof.measures if m.name != p.name and not (multi and m.name in (rev.name, prof_m.name))][:2]
    for m in others:
        s, _ = A.period_series(ctx.frame, prof, m)
        if len(s) < 3:
            continue
        area = m.role == "quantity"
        lab = [A.label(i) for i in s.index]
        _add(ctx, id=slug("trend", m.name), tab="trends", kind="area" if area else "line", title=f"{m.label} over time", role="standard",
             figure=F.line([(m.label, lab, s.tolist())], f"{m.label} over time", area=area, unusual=A.unusual_periods(s)),
             table=_table(["Period", m.label], [[l, F.f(v)] for l, v in zip(lab, s.tolist())]), explain=_explain_trend(s, m),
             how=_how(ctx, _op(m, f"{grain} of {prof.date.column}")), open_q=f"Show {grain}ly {m.name} trend",
             ask_q=f"Analyze the {m.name} trend over time shown in this chart.",
             why_chosen=f"{m.label} has its own time series, shown as {'an area (volume)' if area else 'a line'}.")


def _rank_chart(ctx: Ctx, d: Dim, m: Measure, tab: str, role: str = "standard") -> pd.DataFrame | None:
    df = A.by_dim(ctx.frame, d.name, m)
    if len(df) < 2:
        return None
    big = len(df) > 12
    shown = df.head(10) if big else df
    shares = [None if pd.isna(s) else float(s) for s in shown["share"]]
    title = (f"Top 10 {A.readable(d.name)} by {m.label}" if big else f"{m.label} by {A.readable(d.name)}")
    fig = F.bars(shown["label"].tolist(), shown["value"].tolist(), title, m.label, horizontal=True, shares=shares if m.agg == "sum" else None)
    variants = None
    if big:
        variants = {}
        for key, part in A.topn_variants(df).items():
            t = {"top5": f"Top 5 {A.readable(d.name)} by {m.label}", "top10": title, "bottom5": f"Bottom 5 {A.readable(d.name)} by {m.label}",
                 "all": f"All {A.readable(d.name)} by {m.label}"}[key]
            variants[key] = F.bars(part["label"].tolist(), part["value"].tolist(), t, m.label, horizontal=True,
                                   shares=[None if pd.isna(s) else float(s) for s in part["share"]] if m.agg == "sum" else None)
    _add(ctx, id=slug("rank", m.name, d.name), tab=tab, kind="horizontal_bar", title=title, role=role, figure=fig, variants=variants,
         default_variant="top10" if big else None,
         table=_table([A.readable(d.name), m.label, "Records"], [[r.label, F.f(r.value), int(r.records)] for r in df.itertuples()]),
         explain=_explain_rank(df, m, d.name), how=_how(ctx, _op(m, d.name)),
         drill={"dimension": d.name, "values": [str(v) for v in df["label"].head(100)]},
         open_q=f"Show {m.name} by {d.name}", ask_q=f"Analyze {m.name} by {d.name} shown in this chart.",
         why_chosen=(f"{d.name} has {len(df)} groups, too many for one readable chart, so Top-N views are offered." if big
                     else f"One category column ({d.name}, {len(df)} groups) and one measure: ranked bars, sorted high to low."))
    return df


def build_breakdown(ctx: Ctx) -> None:
    prof, p = ctx.prof, ctx.primary
    dims = [d for d in prof.dims if d.card >= 2]
    if not dims:
        _skip(ctx, "breakdown", "Breakdown by category", "No category column with at least 2 groups was found.",
              ["Upload data with a category such as region, product or channel"], {"label": "Explore dataset", "href": f"/datasets/{ctx.source.id}"})
        _skip(ctx, "composition", "Composition", "This dataset does not contain enough categorical groups to create a meaningful composition chart.",
              ["Select another dimension", "Upload a more detailed dataset"])
        return
    ranked: dict[str, pd.DataFrame] = {}
    for i, d in enumerate(dims[:MAX_DIM_CHARTS]):
        df = _rank_chart(ctx, d, p, "overview" if i < 2 and not any(c["tab"] == "overview" and c["role"] != "hero" for c in ctx.charts) else "breakdown")
        if df is not None:
            ranked[d.name] = df
    # composition (only for additive measures)
    if p.agg != "sum" or p.role not in ADDITIVE_ROLES and p.role != "other":
        _skip(ctx, "composition", "Composition (share of total)",
              f"{p.label} is a rate or price, so shares of its total would be meaningless.",
              ["Choose an additive measure such as sales, revenue or quantity"])
    else:
        added = False
        for parent, child in prof.hierarchies[:1]:
            sub = ctx.frame.groupby([parent, child], observed=True)[p.name].sum(min_count=1).dropna()
            sub = sub[sub > 0]
            if len(sub) >= 3:
                par = sub.groupby(level=0).sum()
                labels = [str(x) for x in par.index] + [f"{c} ({pa})" for pa, c in sub.index]
                parents = [""] * len(par) + [str(pa) for pa, _ in sub.index]
                vals = par.tolist() + sub.tolist()
                _add(ctx, id=slug("sunburst", p.name, parent, child), tab="breakdown", kind="sunburst", title=f"{p.label} by {parent} → {child}",
                     role="standard", figure=F.treemap(labels, parents, vals, f"{p.label} by {A.readable(parent)} → {A.readable(child)}", sunburst=True),
                     table=_table([A.readable(parent), A.readable(child), p.label], [[pa, c, F.f(v)] for (pa, c), v in sub.items()]),
                     explain=[f"{A.readable(child)} nests inside {A.readable(parent)} in this data, so the hierarchy is shown as rings.",
                              f"The largest {A.readable(parent).lower()} is {par.idxmax()} at {A.fmt(par.max())} ({par.max() / par.sum() * 100:.1f}% of the total)."],
                     how=_how(ctx, _op(p, f"{parent}, {child}")), open_q=f"Show {p.name} by {parent}", ask_q=f"Analyze {p.name} by {parent} and {child} shown in this chart.",
                     why_chosen=f"Every {child} belongs to exactly one {parent}: a true hierarchy, so a sunburst is meaningful.")
                added = True
        small = [d for d in dims if 2 <= d.card <= 6]
        mid = [d for d in dims if 7 <= d.card <= 30]
        if small:
            d = small[0]
            df = ranked.get(d.name) if d.name in ranked else A.by_dim(ctx.frame, d.name, p)
            df = df[df["value"] > 0]
            if len(df) >= 2:
                _add(ctx, id=slug("donut", p.name, d.name), tab="breakdown", kind="donut", title=f"{p.label} share by {A.readable(d.name)}", role="standard",
                     figure=F.donut(df["label"].tolist(), df["value"].tolist(), f"{p.label} share by {A.readable(d.name)}"),
                     table=_table([A.readable(d.name), p.label, "Share %"], [[r.label, F.f(r.value), F.f(r.share)] for r in df.itertuples()]),
                     explain=_explain_rank(df, p, d.name), how=_how(ctx, _op(p, d.name)), drill={"dimension": d.name, "values": df["label"].astype(str).tolist()},
                     open_q=f"Show {p.name} by {d.name}", ask_q=f"Analyze the {p.name} composition by {d.name} shown in this chart.",
                     why_chosen=f"{d.name} has only {len(df)} groups, a readable number for a donut.")
                added = True
        if mid and not added or (mid and not any(c["kind"] == "treemap" for c in ctx.charts) and not small):
            d = mid[0]
            df = A.by_dim(ctx.frame, d.name, p)
            df = df[df["value"] > 0]
            if len(df) >= 7:
                labels = ["Total"] + df["label"].tolist()
                _add(ctx, id=slug("treemap", p.name, d.name), tab="breakdown", kind="treemap", title=f"{p.label} by {A.readable(d.name)} (treemap)", role="standard",
                     figure=F.treemap(df["label"].tolist() + [], [""] * len(df), df["value"].tolist(), f"{p.label} by {A.readable(d.name)} (treemap)"),
                     table=_table([A.readable(d.name), p.label, "Share %"], [[r.label, F.f(r.value), F.f(r.share)] for r in df.itertuples()]),
                     explain=_explain_rank(df, p, d.name), how=_how(ctx, _op(p, d.name)), drill={"dimension": d.name, "values": df["label"].astype(str).tolist()},
                     open_q=f"Show {p.name} by {d.name}", ask_q=f"Analyze the {p.name} composition by {d.name} shown in this chart.",
                     why_chosen=f"{d.name} has {len(df)} groups: too many for a pie, so area-proportional rectangles are used.")
                added = True
        if not added:
            _skip(ctx, "composition", "Composition (share of total)",
                  "No category column has a readable number of groups (2–30) for a composition chart"
                  + (f"; the closest has {dims[0].card}." if dims else "."),
                  ["Select another dimension", "Use the Top-N ranking charts instead"])


def build_performance(ctx: Ctx) -> None:
    prof, p = ctx.prof, ctx.primary
    dims = [d for d in prof.dims if 2 <= d.card <= 12]
    # multi-measure grouped comparison (same-unit, additive)
    money = [m for m in prof.measures if m.role in ("revenue", "profit", "cost", "spend") and m.agg == "sum"]
    if len(money) >= 2 and dims:
        d = dims[0]
        cols = money[:3]
        g = ctx.frame.groupby(ctx.frame[d.name].astype(str), observed=True)[[m.name for m in cols]].sum()
        g = g.sort_values(cols[0].name, ascending=False)
        _add(ctx, id=slug("grouped", d.name, *[m.name for m in cols]), tab="performance", kind="grouped_bar",
             title=f"{' vs '.join(m.label for m in cols)} by {A.readable(d.name)}", role="standard",
             figure=F.grouped(g.index.tolist(), [(m.label, g[m.name].tolist()) for m in cols], f"{' vs '.join(m.label for m in cols)} by {A.readable(d.name)}"),
             table=_table([A.readable(d.name)] + [m.label for m in cols], [[i] + [F.f(g.loc[i, m.name]) for m in cols] for i in g.index]),
             explain=[f"{g.index[0]} is highest on {cols[0].label.lower()} ({A.fmt(g.iloc[0][cols[0].name])})."] +
                     [f"Bars for {cols[0].label.lower()}, {', '.join(m.label.lower() for m in cols[1:])} share one axis because they are the same kind of amount."],
             how=_how(ctx, ", ".join(f"SUM({m.name})" for m in cols) + f" GROUP BY {d.name}"), drill={"dimension": d.name, "values": g.index.astype(str).tolist()},
             open_q=f"Compare {cols[0].name} and {cols[1].name} by {d.name}", ask_q=f"Compare {cols[0].name} and {cols[1].name} by {d.name} as shown in this chart.",
             why_chosen=f"{len(cols)} amounts in the same unit exist, so they are compared side by side.")
    # rate measures by dim (e.g. average discount by country)
    rates = [m for m in prof.measures if m.agg == "mean"][:2]
    for m in rates:
        for d in dims[:1]:
            df = A.by_dim(ctx.frame, d.name, m)
            if len(df) >= 2:
                ch = _rank_chart(ctx, d, m, "performance")
                if ch is not None:
                    pass
    # heatmap
    heat_done = False
    if prof.date and prof.date.grain == "month" and 3 <= prof.date.months <= 36:
        for d in [x for x in prof.dims if 3 <= x.card <= 12][:1]:
            d_ = pd.to_datetime(ctx.frame[prof.date.column], errors="coerce")
            pv = pd.to_numeric(ctx.frame[p.name], errors="coerce").groupby([d_.dt.to_period("M").astype(str), ctx.frame[d.name].astype(str)])
            pv = (pv.mean() if p.agg == "mean" else pv.sum()).unstack()
            if pv.shape[0] >= 3 and pv.shape[1] >= 3:
                z = [[F.f(pv.loc[mo, c]) for mo in pv.index] for c in pv.columns]
                _add(ctx, id=slug("heat", p.name, d.name), tab="performance", kind="heatmap", title=f"{p.label}: {A.readable(d.name)} × month", role="standard",
                     figure=F.heatmap(pv.index.tolist(), pv.columns.tolist(), z, f"{p.label}: {A.readable(d.name)} × month", p.label),
                     table=_table(["Month"] + pv.columns.tolist(), [[mo] + [F.f(pv.loc[mo, c]) for c in pv.columns] for mo in pv.index]),
                     explain=[f"Each cell is {p.label.lower()} for one {A.readable(d.name).lower()} in one month; darker is higher.",
                              f"The highest cell is {pv.stack().idxmax()[1]} in {pv.stack().idxmax()[0]} at {A.fmt(pv.stack().max())}."],
                     how=_how(ctx, _op(p, f"{d.name}, month of {prof.date.column}")), open_q=f"Show monthly {p.name} by {d.name}",
                     ask_q=f"Analyze {p.name} by month and {d.name} shown in this chart.",
                     why_chosen=f"{d.name} has {d.card} groups and the data spans {prof.date.months} months: both axes are readable.")
                heat_done = True
    if not heat_done:
        _skip(ctx, "heatmap", "Heatmap", "A heatmap needs a date column spanning at least 3 months and a category with 3–12 groups"
              + (", " + ("neither was found." if not prof.date and not [x for x in prof.dims if 3 <= x.card <= 12] else "one of them is missing.")),
              ["Upload data with dates and a category such as region or channel"])
    # relationship
    pairs = []
    ms = prof.measures[:6]
    for i in range(len(ms)):
        for j in range(i + 1, len(ms)):
            r = A.pearson(ctx.frame, ms[i].name, ms[j].name)
            if r:
                pairs.append(r)
    good = [r for r in pairs if abs(r["r"]) >= 0.3]
    pref = {("spend", "revenue"), ("quantity", "revenue"), ("price", "quantity"), ("discount", "profit"), ("cost", "revenue"), ("discount", "revenue")}

    def role(n):
        return prof.by_name(n).role

    good.sort(key=lambda r: (0 if (role(r["a"]), role(r["b"])) in pref or (role(r["b"]), role(r["a"])) in pref else 1, -abs(r["r"])))
    ctx.relationships = good  # type: ignore[attr-defined]
    for r in good[:2]:
        a, b = prof.by_name(r["a"]), prof.by_name(r["b"])
        x, y = (a, b) if (a.role in ("spend", "quantity", "price", "discount", "cost") or b.role == "revenue") else (b, a)
        xs, ys, sampled = A.sample_points(ctx.frame, x.name, y.name)
        sign = "higher" if r["r"] > 0 else "lower"
        _add(ctx, id=slug("scatter", x.name, y.name), tab="performance", kind="scatter", title=f"{y.label} vs {x.label}", role="standard",
             figure=F.scatter(xs, ys, x.name, y.name, r["r"], r["strength"], r["n"], sampled),
             table=_table([x.label, y.label], [[F.f(a_), F.f(b_)] for a_, b_ in zip(xs, ys)]),
             explain=[f"Across {r['n']:,} records, higher {x.label.lower()} is associated with {sign} {y.label.lower()} (r = {r['r']:.2f}, {r['strength']}).",
                      "This is an association in the data, not proof that one causes the other."],
             how=_how(ctx, f"CORR({x.name}, {y.name}) over {r['n']:,} records", r["n"]), open_q=f"What is the correlation between {x.name} and {y.name}?",
             ask_q=f"Analyze the relationship between {x.name} and {y.name} shown in this chart.",
             why_chosen=f"Two numeric measures with {r['n']:,} paired records and a {r['strength']} correlation (|r| ≥ 0.3).")
    if not good:
        best = max(pairs, key=lambda r: abs(r["r"]), default=None)
        _skip(ctx, "relationship", "Relationship (scatter)",
              (f"No pair of measures shows a meaningful relationship (strongest: {best['a']} vs {best['b']}, r = {best['r']:.2f})." if best
               else "Fewer than two numeric measures have enough paired records (30+) to test a relationship."),
              ["Choose two other measures in chat", "Filter to a narrower segment"],
              {"label": "Ask AInsight", "href": f"/chat?dataset={ctx.source.id}&q=" + "Which measures are related to each other?"})
    # waterfall
    rev, cost, prf = prof.measure("revenue"), (prof.measure("cost") or prof.measure("spend")), prof.measure("profit")
    wf = False
    if rev and cost and prf and rev.agg == cost.agg == prf.agg == "sum":
        R, C, P = (pd.to_numeric(ctx.frame[m.name], errors="coerce") for m in (rev, cost, prf))
        ok = (R - C - P).abs() <= 0.01 * R.abs().clip(lower=1e-9)
        if ok.mean() >= 0.98:
            tr, tc, tp = float(R.sum()), float(C.sum()), float(P.sum())
            _add(ctx, id="waterfall", tab="performance", kind="waterfall", title=f"{rev.label} → {cost.label} → {prf.label}", role="standard",
                 figure=F.waterfall([(rev.label, tr, "absolute"), (cost.label, -tc, "relative"), (prf.label, tp, "total")], f"{rev.label} → {cost.label} → {prf.label}"),
                 table=_table(["Step", "Value"], [[rev.label, tr], [cost.label, -tc], [prf.label, tp]]),
                 explain=[f"{rev.label} of {A.fmt(tr)} less {cost.label.lower()} of {A.fmt(tc)} leaves {prf.label.lower()} of {A.fmt(tp)}.",
                          f"This reconciles row by row in {ok.mean() * 100:.1f}% of records."],
                 how=_how(ctx, f"SUM({rev.name}), SUM({cost.name}), SUM({prf.name})"), open_q=f"Show {prf.name} and {cost.name} against {rev.name}",
                 ask_q="Explain how revenue turns into profit as shown in this chart.", why_chosen="Revenue, cost and profit exist and reconcile row by row.")
            wf = True
    if not wf:
        missing = [n for n, v in (("revenue", rev), ("cost", cost), ("profit", prf)) if not v]
        _skip(ctx, "waterfall", "Waterfall (revenue → cost → profit)",
              (f"The dataset has no {' or '.join(missing)} measure." if missing else "Revenue, cost and profit do not reconcile row by row, so a waterfall would mislead."),
              ["Upload data that includes revenue, cost and profit columns"])
    # gauge
    tg = [m for m in prof.targets]
    if tg:
        tname = tg[0]
        actual_m = prof.measure("revenue") or p
        _add(ctx, id="gauge", tab="performance", kind="gauge", title=f"{actual_m.label} vs {tname}", role="standard",
             figure=F.gauge(float(pd.to_numeric(ctx.frame[actual_m.name], errors="coerce").sum()), float(pd.to_numeric(ctx.frame[tname], errors="coerce").sum()),
                            f"{actual_m.label} vs {A.readable(tname)}", actual_m.name),
             table=_table(["Measure", "Value"], [[actual_m.label, F.f(pd.to_numeric(ctx.frame[actual_m.name], errors="coerce").sum())],
                                                [A.readable(tname), F.f(pd.to_numeric(ctx.frame[tname], errors="coerce").sum())]]),
             explain=["Actual compared with the target column in the data; AInsight does not set targets."], how=_how(ctx, f"SUM({actual_m.name}) vs SUM({tname})"),
             open_q=f"Show {actual_m.name} against {tname}", ask_q="Analyze target achievement shown in this chart.", why_chosen=f"The data contains a target column ({tname}).")
    else:
        _skip(ctx, "gauge", "Gauge / target tracking", "No target, goal, quota or budget column exists in this data. AInsight never invents a target.",
              ["Add a target column to your data", "Ask AInsight to compare against a figure you type in"])
    # funnel
    stage_cols = [(i, m) for i, w in enumerate(FUNNEL_WORDS) for m in prof.measures if re.search(w, m.name, re.I)]
    seen, stages = set(), []
    for i, m in sorted(stage_cols, key=lambda t: t[0]):
        if m.name not in seen:
            seen.add(m.name); stages.append(m)
    totals = [float(pd.to_numeric(ctx.frame[m.name], errors="coerce").sum()) for m in stages]
    if len(stages) >= 3 and all(totals[i] >= totals[i + 1] for i in range(len(totals) - 1)) and totals[0] > 0:
        _add(ctx, id="funnel", tab="performance", kind="funnel", title="Funnel", role="standard",
             figure=F.funnel([m.label for m in stages], totals, "Funnel"), table=_table(["Stage", "Total"], [[m.label, t] for m, t in zip(stages, totals)]),
             explain=[f"{stages[0].label} ({A.fmt(totals[0])}) falls to {stages[-1].label} ({A.fmt(totals[-1])})."], how=_how(ctx, "SUM of each stage column"),
             open_q="Show the funnel stages", ask_q="Analyze the funnel shown in this chart.", why_chosen="Several stage columns exist in sequence and never increase.")
    else:
        _skip(ctx, "funnel", "Funnel", "No sequential stage columns (such as Leads → Qualified → Won) were found. A funnel is never built from unrelated categories.",
              ["Upload data with one column per stage"])
    # sankey
    src = next((d for d in prof.dims if re.fullmatch(r"(source|origin|from)\w*", d.name, re.I)), None)
    dst = next((d for d in prof.dims if re.fullmatch(r"(destination|target|to)\w*", d.name, re.I)), None)
    if src and dst and src.name != dst.name and src.card * dst.card <= 200:
        g = ctx.frame.groupby([src.name, dst.name], observed=True)[p.name].sum().reset_index()
        g = g[g[p.name] > 0]
        _add(ctx, id="sankey", tab="breakdown", kind="sankey", title=f"{p.label} flow: {src.name} → {dst.name}", role="standard",
             figure=F.sankey(g[src.name].astype(str).tolist(), g[dst.name].astype(str).tolist(), g[p.name].tolist(), f"{p.label} flow"),
             table=_table([src.name, dst.name, p.label], g.values.tolist()), explain=["Link width is proportional to the amount flowing between the two groups."],
             how=_how(ctx, _op(p, f"{src.name}, {dst.name}")), open_q=f"Show {p.name} by {src.name} and {dst.name}", ask_q="Analyze the flow shown in this chart.",
             why_chosen="Explicit source and destination columns exist.")
    else:
        _skip(ctx, "sankey", "Sankey (flow)", "No source → destination columns exist. Two unrelated categories do not form a flow.", ["Upload data with source and destination columns"])


def build_distribution(ctx: Ctx) -> None:
    prof = ctx.prof
    pick = [m for m in prof.measures if m.role in ("price", "discount", "rate")] + [m for m in prof.measures if m.role in ("revenue", "quantity", "profit")]
    done = 0
    for m in pick:
        if done >= 2:
            break
        h = A.histogram(ctx.frame[m.name])
        if not h:
            continue
        edges, counts, med, q1, q3, lo, hi = h
        _add(ctx, id=slug("hist", m.name), tab="distribution", kind="histogram", title=f"Distribution of {A.readable(m.name)}", role="standard",
             figure=F.histogram(edges, counts, m.name, med),
             table=_table(["Range", "Records"], [[f"{human_number(edges[i])}–{human_number(edges[i + 1])}", c] for i, c in enumerate(counts)]),
             explain=[f"The middle half of records lies between {A.fmt(q1)} and {A.fmt(q3)}; the median is {A.fmt(med)}.",
                      f"Values range from {A.fmt(lo)} to {A.fmt(hi)}."],
             how=_how(ctx, f"HISTOGRAM({m.name}, {len(counts)} bins)"), open_q=f"Show the distribution of {m.name}", ask_q=f"Analyze the distribution of {m.name} shown in this chart.",
             why_chosen=f"{m.label} has {m.nunique:,} distinct values across enough records to show its spread.")
        done += 1
    # box by dim
    d = next((x for x in prof.dims if 2 <= x.card <= 10), None)
    bm = next((m for m in pick if A.histogram(ctx.frame[m.name])), None)
    if d and bm:
        groups, stats = A.box_stats(ctx.frame, d.name, bm.name)
        if len(groups) >= 2:
            _add(ctx, id=slug("box", bm.name, d.name), tab="distribution", kind="box", title=f"{bm.label} by {A.readable(d.name)}", role="standard",
                 figure=F.box(groups, stats, bm.name, d.name),
                 table=_table([A.readable(d.name), "Q1", "Median", "Q3", "Mean", "Records"], [[g, F.f(s["q1"]), F.f(s["median"]), F.f(s["q3"]), F.f(s["mean"]), s["n"]] for g, s in zip(groups, stats)]),
                 explain=[f"{groups[int(np.argmax([s['median'] for s in stats]))]} has the highest median {bm.label.lower()}.",
                          "Each box covers the middle 50% of that group's records."],
                 how=_how(ctx, f"QUARTILES({bm.name}) GROUP BY {d.name}"), open_q=f"Show {bm.name} by {d.name}", ask_q=f"Analyze {bm.name} by {d.name} shown in this chart.",
                 why_chosen=f"{d.name} has {len(groups)} groups with at least 5 records each, enough to compare spreads.")
            done += 1
    if done == 0:
        _skip(ctx, "distribution", "Distribution", "No numeric column has enough distinct values and records (30+) to show a meaningful distribution.",
              ["Upload more rows", "Remove filters"], {"label": "Clear filters", "kind": "clear-filters"})


def decide(ctx: Ctx) -> None:
    build_trends(ctx)
    build_breakdown(ctx)
    build_performance(ctx)
    build_distribution(ctx)
    if not any(c["role"] == "hero" for c in ctx.charts):
        # no time series: the strongest category ranking becomes the hero
        cand = next((c for c in ctx.charts if c["id"].startswith("rank-")), None)
        if cand:
            cand["role"], cand["tab"], cand["wide"] = "hero", "overview", True
