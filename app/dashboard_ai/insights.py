"""KPIs, AI-detected insights, What Changed, data quality, next analyses and the business story.

Everything here is derived from the same validated pandas numbers the charts use. No sentence contains a figure
that was not computed, and the word "significant" is never used (there is no statistical test behind these cards)."""

from __future__ import annotations

import urllib.parse

import pandas as pd

from app.dashboard_ai import analysis as A
from app.dashboard_ai.profiler import Dim, Measure, Profile

GOOD_WHEN_UP = {"revenue", "profit", "quantity"}


def chat_link(dataset_id: str, question: str) -> str:
    return f"/chat?dataset={dataset_id}&q=" + urllib.parse.quote(question)


def _exact(v: float, agg: str) -> str:
    return f"{v:,.2f}" if (agg == "mean" or v != int(v)) else f"{int(v):,}"


# ------------------------------------------------------------------------------------------------- KPIs
def build_kpis(ctx, limit: int = 4) -> list[dict]:
    prof, frame = ctx.prof, ctx.frame
    out: list[dict] = []
    for m in prof.measures[:limit]:
        value = A.reduce(frame[m.name], m.agg)
        if value is None:
            continue
        kpi = {"id": f"kpi-{m.name}", "label": m.label, "measure": m.name, "value": value, "display": A.fmt(value),
               "exact": _exact(value, m.agg), "aggregation": m.agg, "change": None, "sparkline": None,
               "source": ctx.source.name, "tone": "good-up" if m.role in GOOD_WHEN_UP else "neutral",
               "how": {"operation": ("AVG" if m.agg == "mean" else "SUM") + f"({m.name})", "rows": int(len(frame)), "engine": "Pandas analytics"},
               "open_q": f"What is the total {m.name}?" if m.agg == "sum" else f"What is the average {m.name}?"}
        if prof.date:
            s, dropped = A.period_series(frame, prof, m)
            if len(s) >= 2:
                kpi["sparkline"] = [round(float(v), 4) for v in s.tail(24).tolist()]
                c = A.change(s)
                if c:
                    small = abs(c["previous"]) < 0.05 * abs(float(s.mean()))
                    kpi["change"] = {"pct": c["pct"], "label": c["label"], "direction": c["direction"],
                                     "note": "Latest complete period vs the one before it." + (" The partial newest period is excluded." if dropped else "")
                                     + (" The earlier period is small, so the percentage looks large." if small else "")}
        out.append(kpi)
    out.append({"id": "kpi-records", "label": "Records", "measure": None, "value": int(len(frame)), "display": f"{len(frame):,}",
                "exact": f"{len(frame):,}", "aggregation": "count", "change": None, "sparkline": None, "source": ctx.source.name, "tone": "neutral",
                "how": {"operation": "COUNT(*)", "rows": int(len(frame)), "engine": "Pandas analytics"}, "open_q": "How many records are there?"})
    return out


# --------------------------------------------------------------------------------------- period helpers
def _period_mask(frame: pd.DataFrame, prof: Profile, period) -> pd.Series:
    d = pd.to_datetime(frame[prof.date.column], errors="coerce")
    return d.dt.to_period("M" if prof.date.grain == "month" else "D") == period


def _best_dim(prof: Profile) -> Dim | None:
    return next((d for d in prof.dims if 3 <= d.card <= 30), next((d for d in prof.dims if d.card >= 2), None))


def _pct(a: float, b: float) -> float | None:
    return None if not b else (a - b) / abs(b) * 100


# ----------------------------------------------------------------------------------------- What changed
def what_changed(ctx) -> dict | None:
    prof, frame, p = ctx.prof, ctx.frame, ctx.primary
    series = ctx.trend_series
    if not prof.date or series is None or len(series) < 2:
        return None
    cur_p, prev_p = series.index[-1], series.index[-2]
    cur, prev = float(series.iloc[-1]), float(series.iloc[-2])
    items: list[dict] = []
    diffs = series.diff().dropna()
    up_i, dn_i = diffs.idxmax(), diffs.idxmin()
    if diffs[up_i] > 0:
        pct = _pct(float(series[up_i]), float(series.shift(1)[up_i]))
        items.append({"kind": "biggest_increase", "title": "Biggest increase", "text":
                      f"{up_i}: {A.fmt(series[up_i])}, up {A.fmt(diffs[up_i])}" + (f" ({pct:.1f}%)" if pct is not None else "") + " on the period before.",
                      "ask": f"Why did {p.name} rise in {up_i}?"})
    if diffs[dn_i] < 0:
        pct = _pct(float(series[dn_i]), float(series.shift(1)[dn_i]))
        items.append({"kind": "biggest_decrease", "title": "Biggest decrease", "text":
                      f"{dn_i}: {A.fmt(series[dn_i])}, down {A.fmt(abs(diffs[dn_i]))}" + (f" ({abs(pct):.1f}%)" if pct is not None else "") + " on the period before.",
                      "ask": f"Why did {p.name} fall in {dn_i}?"})
    hi, lo = series.idxmax(), series.idxmin()
    items.append({"kind": "highest_period", "title": "Highest period", "text": f"{hi} at {A.fmt(series[hi])}.", "ask": f"Why was {p.name} highest in {hi}?"})
    if lo != hi:
        items.append({"kind": "lowest_period", "title": "Lowest period", "text": f"{lo} at {A.fmt(series[lo])}.", "ask": f"Why was {p.name} lowest in {lo}?"})
    # by-category movement between the last two periods
    d = _best_dim(prof)
    movers = None
    if d is not None:
        a = A.by_dim(frame[_period_mask(frame, prof, cur_p)], d.name, p).set_index("label")["value"]
        b = A.by_dim(frame[_period_mask(frame, prof, prev_p)], d.name, p).set_index("label")["value"]
        both = pd.concat([a.rename("cur"), b.rename("prev")], axis=1).fillna(0.0)
        both["delta"] = both["cur"] - both["prev"]
        if len(both) >= 2:
            movers = both.sort_values("delta", ascending=False)
            top, bot = movers.index[0], movers.index[-1]
            if movers["delta"].iloc[0] > 0:
                items.append({"kind": "largest_gain", "title": f"Largest gain by {A.readable(d.name)}",
                              "text": f"{top}: {A.fmt(movers['cur'].iloc[0])} in {cur_p} vs {A.fmt(movers['prev'].iloc[0])} in {prev_p} (+{A.fmt(movers['delta'].iloc[0])}).",
                              "ask": f"Why did {p.name} change for {top}?", "filter": {d.name: top}})
            if movers["delta"].iloc[-1] < 0:
                items.append({"kind": "largest_loss", "title": f"Largest drop by {A.readable(d.name)}",
                              "text": f"{bot}: {A.fmt(movers['cur'].iloc[-1])} in {cur_p} vs {A.fmt(movers['prev'].iloc[-1])} in {prev_p} ({A.fmt(movers['delta'].iloc[-1])}).",
                              "ask": f"Why did {p.name} change for {bot}?", "filter": {d.name: bot}})
            if p.agg == "sum" and (cur - prev) != 0:
                lead = movers.index[0] if cur > prev else movers.index[-1]
                share = float(movers.loc[lead, "delta"]) / (cur - prev) * 100
                if share > 0:
                    how = (f"accounts for {share:.0f}% of the" if share <= 100 else f"moved {A.fmt(abs(float(movers.loc[lead, 'delta'])))}, more than the net")
                    items.append({"kind": "contribution", "title": "Largest contribution to the change",
                                  "text": f"{lead} {how} {A.fmt(abs(cur - prev))} {'increase' if cur > prev else 'decrease'} between {prev_p} and {cur_p}" + ("" if share <= 100 else " (other groups moved the opposite way)") + ".",
                                  "ask": f"How much did {lead} contribute to the {p.name} change?", "filter": {d.name: lead}})
            fast = both[both["prev"] > 0].copy()
            if len(fast) >= 2:
                fast["pct"] = (fast["cur"] - fast["prev"]) / fast["prev"].abs() * 100
                fg, fd = fast["pct"].idxmax(), fast["pct"].idxmin()
                if fast["pct"][fg] > 0:
                    items.append({"kind": "fastest_growing", "title": "Fastest growing", "text": f"{fg}: +{fast['pct'][fg]:.1f}% ({prev_p} → {cur_p}).",
                                  "ask": f"Why is {fg} growing fastest?", "filter": {d.name: fg}})
                if fast["pct"][fd] < 0:
                    items.append({"kind": "fastest_declining", "title": "Fastest declining", "text": f"{fd}: {fast['pct'][fd]:.1f}% ({prev_p} → {cur_p}).",
                                  "ask": f"Why is {fd} declining?", "filter": {d.name: fd}})
    pct = _pct(cur, prev)
    headline = (f"{p.label} was {A.fmt(cur)} in {cur_p}, " + ("flat" if pct is None or abs(pct) < 0.05 else f"{'up' if pct > 0 else 'down'} {abs(pct):.1f}%")
                + f" from {A.fmt(prev)} in {prev_p}.")
    return {"headline": headline, "current": str(cur_p), "previous": str(prev_p), "items": items, "dimension": d.name if d else None}


# ------------------------------------------------------------------------------------------- insights
def _first_rank(ctx):
    for c in ctx.charts:
        if c["id"].startswith("rank-") and c.get("drill"):
            return c
    return None


def build_insights(ctx) -> list[dict]:
    prof, frame, p = ctx.prof, ctx.frame, ctx.primary
    out: list[dict] = []
    d = _best_dim(prof)
    df = A.by_dim(frame, d.name, p) if d else None
    if df is not None and len(df) >= 2:
        top, bot = df.iloc[0], df.iloc[-1]
        word = "highest average" if p.agg == "mean" else "largest"
        share = f", {top['share']:.1f}% of the total" if p.agg == "sum" and pd.notna(top["share"]) else ""
        out.append({"kind": "top", "title": "Top finding", "text": f"{top['label']} has the {word} {p.label.lower()} among {A.readable(d.name).lower()} groups: {A.fmt(top['value'])}{share}.",
                    "ask": f"Why is {top['label']} the top {d.name} for {p.name}?", "filter": {d.name: top["label"]}})
        gap = float(top["value"] - bot["value"])
        out.append({"kind": "attention", "title": "Needs attention", "text":
                    f"{bot['label']} is lowest on {p.label.lower()} at {A.fmt(bot['value'])}, {A.fmt(gap)} below {top['label']}.",
                    "ask": f"Why is {bot['label']} the lowest {d.name} for {p.name}?", "filter": {d.name: bot["label"]}})
    s = ctx.trend_series
    if s is not None and len(s) >= 2:
        c = A.change(s)
        if c:
            verb = "grew" if c["pct"] > 0.05 else "fell" if c["pct"] < -0.05 else "was flat"
            txt = f"{p.label} {verb}" + ("" if verb == "was flat" else f" {abs(c['pct']):.1f}%") + f" ({c['label']}): {A.fmt(c['previous'])} → {A.fmt(c['current'])}."
            out.append({"kind": "growth", "title": "Growth", "text": txt, "ask": f"Why did {p.name} change in {s.index[-1]}?"})
        if len(s) >= 4 and all(s.diff().iloc[-3:] < 0):
            out.append({"kind": "attention", "title": "Needs attention", "text": f"{p.label} has fallen in each of the last 3 periods ({A.fmt(s.iloc[-4])} → {A.fmt(s.iloc[-1])}).",
                        "ask": f"Why has {p.name} declined for three periods?"})
    for u in (ctx.trend_unusual or [])[:1]:
        out.append({"kind": "attention", "title": "Unusual period", "text": f"{u['label']} was unusually {u['direction']} ({A.fmt(u['value'])} vs a typical {A.fmt(u['typical'])}).",
                    "ask": f"What happened with {p.name} in {u['label']}?"})
    # opportunity: margin gap (needs revenue and profit and a dimension)
    rev, prf = prof.measure("revenue"), prof.measure("profit")
    if rev and prf and rev.agg == prf.agg == "sum" and d:
        g = frame.groupby(frame[d.name].astype(str), observed=True)[[rev.name, prf.name]].sum()
        g = g[g[rev.name] > 0]
        if len(g) >= 2:
            g["margin"] = g[prf.name] / g[rev.name] * 100
            hi, lo = g["margin"].idxmax(), g["margin"].idxmin()
            if hi != lo and g.loc[hi, 'margin'] - g.loc[lo, 'margin'] >= 1.0:
                out.append({"kind": "opportunity", "title": "Opportunity", "text":
                            f"{hi} converts {rev.label.lower()} to {prf.label.lower()} best ({g.loc[hi, 'margin']:.1f}% margin) while {lo} is lowest ({g.loc[lo, 'margin']:.1f}%).",
                            "ask": f"Why is the profit margin different for {hi} and {lo}?", "filter": {d.name: lo}})
    elif df is not None and len(df) >= 3 and p.agg == "sum":
        med = float(df["value"].median())
        low = df.iloc[-1]
        out.append({"kind": "opportunity", "title": "Opportunity", "text":
                    f"If {low['label']} matched the median {A.readable(d.name).lower()} ({A.fmt(med)}), {p.label.lower()} would be {A.fmt(med - low['value'])} higher (arithmetic on current data, not a forecast).",
                    "ask": f"How can {low['label']} improve {p.name}?", "filter": {d.name: low["label"]}})
    for r in (ctx.relationships or [])[:1]:
        out.append({"kind": "relationship", "title": "Relationship", "text": f"{r['a']} and {r['b']} move together ({r['strength']}, r = {r['r']:.2f}, {r['n']:,} records). This is an association, not proof of cause.",
                    "ask": f"What is the relationship between {r['a']} and {r['b']}?"})
    return out


# ----------------------------------------------------------------------------------------------- quality
def quality_panel(ctx, rows_total: int) -> dict:
    q = dict(ctx.prof.quality)
    q["rows_analyzed"] = int(len(ctx.frame))
    q["rows_total"] = int(rows_total)
    return q


# -------------------------------------------------------------------------------------------- next steps
def recommendations(ctx) -> list[dict]:
    prof, p, ds = ctx.prof, ctx.primary, ctx.source.id
    qs: list[str] = []
    ranked = {c["drill"]["dimension"] for c in ctx.charts if c.get("drill")}
    for d in prof.dims[:6]:
        if d.name not in ranked:
            qs.append(f"Show {p.name} by {d.name}")
    if prof.date and ctx.trend_series is not None and len(ctx.trend_series) >= 2:
        qs.append(f"Why did {p.name} change in {ctx.trend_series.index[-1]}?")
    d = _best_dim(prof)
    if d:
        qs.append(f"Which {d.name} has the lowest {p.name}?")
        if len(prof.dims) >= 2:
            qs.append(f"Show {p.name} by {d.name} and {next(x for x in prof.dims if x.name != d.name).name}")
    if len(prof.measures) >= 2:
        qs.append(f"Compare {p.name} and {next(m for m in prof.measures if m.name != p.name).name}")
    seen, out = set(), []
    for q in qs:
        if q not in seen:
            seen.add(q)
            out.append({"label": q, "href": chat_link(ds, q)})
    return out[:6]


# ----------------------------------------------------------------------------------------------- story
def business_story(ctx, insights: list[dict], changed: dict | None) -> dict:
    by = {}
    for i in insights:
        by.setdefault(i["kind"], []).append(i["text"])
    p = ctx.primary
    total = A.reduce(ctx.frame[p.name], p.agg)
    overview = [f"{ctx.source.name}: {len(ctx.frame):,} records analysed. {p.label} is {A.fmt(total)}" + (
        f" between {ctx.prof.date.start.date()} and {ctx.prof.date.end.date()}." if ctx.prof.date else ".")]
    sections = [{"title": "Overview", "points": overview + by.get("top", [])},
                {"title": "Growth", "points": ([changed["headline"]] if changed else []) + by.get("growth", [])},
                {"title": "Drivers", "points": [i["text"] for i in (changed or {}).get("items", []) if i["kind"] in ("largest_gain", "contribution", "fastest_growing")] + by.get("relationship", [])},
                {"title": "Risks", "points": by.get("attention", []) + [i["text"] for i in (changed or {}).get("items", []) if i["kind"] in ("largest_loss", "fastest_declining", "biggest_decrease")]},
                {"title": "Opportunities", "points": by.get("opportunity", [])}]
    actions = []
    for i in insights:
        if i["kind"] in ("attention", "opportunity") and i.get("ask"):
            actions.append(f"Investigate: {i['ask']}")
    sections.append({"title": "Recommended Actions", "points": actions[:4] or ["Explore the recommended analyses to dig further."]})
    for s in sections:
        s["points"] = s["points"] or ["Not enough data to say anything grounded here."]
    return {"title": f"Business story: {ctx.source.name}", "sections": sections,
            "note": "Every figure above is calculated from your data; the wording is deterministic, not generated by a model."}
