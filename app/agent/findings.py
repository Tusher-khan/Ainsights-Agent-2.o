"""Turn tool results into validated findings, then findings into the final answer.

Every sentence here is composed from numbers that a tool computed. The three tiers are kept
apart on purpose:

  FACT            what the data directly shows (a computed number)
  INTERPRETATION  what that pattern suggests (worded as "suggests"/"is consistent with")
  RECOMMENDATION  what the business could consider, each tied to specific findings

An optional LLM pass may rephrase the executive summary, but it is only accepted when every
number it uses already appears in the facts (see `grounded`).
"""

from __future__ import annotations

import re
from typing import Any

from app.utils.helpers import human_number
from app.utils.logger import get_logger

log = get_logger("agent.findings")

EVIDENCE_NOT_ENOUGH = "The available data is not sufficient to determine the cause reliably."


def _r(name: str) -> str:
    return str(name).replace("_", " ").strip()


def _pct(p: float | None) -> str:
    return "n/a" if p is None else f"{p:+.1f}%"


def _verb(delta: float) -> str:
    return "increased" if delta > 0 else "decreased" if delta < 0 else "was unchanged"


def _passed(result: dict) -> bool:
    return all(c.get("passed", True) for c in (result.get("checks") or []))


def _add(findings: list, step: int, kind: str, text: str, numbers: dict, validated: bool, **extra: Any) -> dict:
    f = {"id": f"f{len(findings) + 1}", "kind": kind, "text": text, "numbers": numbers, "step": step, "validated": validated, **extra}
    findings.append(f)
    return f


def _where(filters: dict | None) -> str:
    return " in " + " / ".join(str(v) for v in filters.values()) if filters else ""


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def extract(tool: str, args: dict, result: dict, step: int, state) -> list[dict]:
    """Findings from one tool result. Also records working context the policy uses to decide the next step."""
    ok = _passed(result) if isinstance(result, dict) else True
    out: list[dict] = []
    ctx = state.context
    fn = _EXTRACTORS.get(tool)
    if fn and isinstance(result, dict) and result:
        try:
            fn(out, args, result, step, ok, ctx, state)
        except (KeyError, TypeError, IndexError, ValueError) as exc:  # a malformed result yields no findings, never a crash
            log.warning("Could not extract findings from %s: %s", tool, exc)
            out.clear()
    state.findings.extend(out)
    for f in out:  # keep ids unique across steps
        pass
    _renumber(state.findings)
    return out


def _renumber(findings: list[dict]) -> None:
    for i, f in enumerate(findings, 1):
        f["id"] = f"f{i}"


def _investigation(out, args, r, step, ok, ctx, state):
    k = r["kpis"][0] if r.get("kpis") else None
    ctx["measure"] = r.get("measure")
    ctx["grain"] = r.get("grain") or "month"
    ctx["dataset_id"] = args.get("dataset_id")
    ctx["inv"] = {"drivers": r.get("drivers", {}), "top_drivers": r.get("top_drivers", []), "hotspots": r.get("hotspots", []),
                  "current": r["current"]["label"], "previous": r["previous"]["label"], "aggregation": r.get("aggregation")}
    ctx["total_delta"] = k["change"] if k else 0
    series = [p["value"] for p in r.get("series", [])]
    if len(series) >= 8:
        import numpy as np

        diffs = np.diff(np.array(series[:-1], dtype=float))  # history before the latest move
        base = float(np.mean(np.abs(series[:-1])) or 1.0)
        ctx["usual_swing_pct"] = round(float(np.std(diffs)) / base * 100, 2)
    if k:
        _add(out, step, "change",
             f"{_r(r['measure']).title()} {_verb(k['change'])} from {human_number(k['previous'])} in {r['previous']['label']} "
             f"to {human_number(k['current'])} in {r['current']['label']} ({_pct(k['change_pct'])}).",
             {"previous": k["previous"], "current": k["current"], "delta": k["change"], "delta_pct": k["change_pct"]}, ok,
             subject={"measure": r["measure"]})
    for row in (r.get("top_drivers") or [])[:3]:
        if row.get("share_of_change_pct") is None:
            continue
        swing = " (larger than its usual swing)" if row.get("unusual") is True else ""
        _add(out, step, "driver",
             f"{row['segment']} ({_r(row['dimension'])}) moved from {human_number(row['previous'])} to {human_number(row['current'])}, "
             f"{abs(row['share_of_change_pct']):.0f}% of the total change{swing}.",
             {"previous": row["previous"], "current": row["current"], "delta": row["delta"], "share_pct": row["share_of_change_pct"]},
             ok, subject={"dimension": row["dimension"], "segment": row["segment"]})
    for row in (r.get("hotspots") or [])[:1]:
        _add(out, step, "hotspot",
             f"The sharpest combination was {row['label']}: {human_number(row['previous'])} → {human_number(row['current'])} "
             f"({human_number(row['delta'])}" + (f", {abs(row['share_of_change_pct']):.0f}% of the total change" if row.get("share_of_change_pct") is not None else "") + ").",
             {"delta": row["delta"], "share_pct": row.get("share_of_change_pct")}, ok,
             subject={"dimensions": row["dimensions"], "segments": row["segments"]})
    for a in (r.get("anomalies") or [])[:2]:
        _add(out, step, "anomaly", f"{a['period']} was unusual: {_pct(a.get('change_pct'))} versus the period before.",
             {"change_pct": a.get("change_pct")}, ok, subject={"period": a["period"]})
    for lim in r.get("limitations", []):
        state.context.setdefault("limitations", []).append(lim)


def _small_change(state) -> dict | None:
    """The overall change, when it is too small (or within the series' own normal swing) to call a finding."""
    change = next((f for f in state.findings if f["kind"] == "change"), None)
    if not change:
        return None
    pct = change["numbers"].get("delta_pct")
    swing = state.context.get("usual_swing_pct")
    if pct is not None and abs(pct) < 2.0:
        return {"pct": pct, "swing": swing, "why": "below the 2% materiality threshold"}
    if pct is not None and swing and abs(pct) <= 1.5 * swing:
        return {"pct": pct, "swing": swing, "why": "within the series' normal period-to-period swing"}
    return None


def _contribution(out, args, r, step, ok, ctx, state):
    t = r["total"]
    filters = r.get("filters") or {}
    measure = r["measure"]
    where = _where(filters)
    if r.get("dimension") is None or filters:
        label = "Overall" if not filters else f"Within {' / '.join(filters.values())}"
        if filters or not any(f["kind"] == "change" for f in state.findings):
            _add(out, step, "change" if not filters else "slice_change",
                 f"{label}, {_r(measure)} {_verb(t['delta'])} from {human_number(t['previous'])} to {human_number(t['current'])} "
                 f"({_pct(t['delta_pct'])}) between {r['previous_label']} and {r['current_label']}.",
                 {"previous": t["previous"], "current": t["current"], "delta": t["delta"], "delta_pct": t["delta_pct"]}, ok,
                 subject={"measure": measure, "filters": filters})
    lead = r.get("lead")
    if r.get("dimension") and lead and lead.get("share_of_change_pct") is not None:
        row = next((x for x in r["rows"] if x["segment"] == lead["segment"]), {})
        _add(out, step, "drill",
             f"{where.strip() + ', ' if where else ''}{lead['segment']} ({_r(r['dimension'])}) accounts for "
             f"{abs(lead['share_of_change_pct']):.0f}% of the change ({human_number(row.get('previous', 0))} → {human_number(row.get('current', 0))}).",
             {"delta": lead["delta"], "share_pct": lead["share_of_change_pct"], "delta_pct": lead.get("delta_pct")}, ok,
             subject={"dimension": r["dimension"], "segment": lead["segment"], "filters": filters})
    if r.get("segments_for_80pct"):
        ctx.setdefault("concentration", []).append({"dimension": r.get("dimension"), "k": r["segments_for_80pct"],
                                                    "n": r.get("segments_moving_with_total"), "filters": filters})
    for lim in r.get("limitations", []):
        ctx.setdefault("limitations", []).append(lim)


def _anomalies(out, args, r, step, ok, ctx, state):
    ctx["measure"] = r.get("measure")
    ctx["grain"] = r.get("grain")
    ctx["dataset_id"] = args.get("dataset_id")
    ctx["series"] = r.get("series", [])
    ctx["anomalies"] = r.get("anomalies", [])
    if not r.get("anomalies"):
        _add(out, step, "anomaly_none", f"No unusual {r['grain']}-to-{r['grain']} movements were found in {_r(r['measure'])} "
             f"across {r['periods_checked']} periods.", {"periods": r["periods_checked"]}, ok)
    for a in r.get("anomalies", [])[:3]:
        _add(out, step, "anomaly", f"{_r(r['measure']).title()} {a['direction']}d {abs(a['change_pct']):.1f}% in {a['period']}, "
             "well outside its recent pattern.", {"change_pct": a["change_pct"]}, ok, subject={"period": a["period"]})
    ctx.setdefault("limitations", []).extend(r.get("limitations", []))


def _trends(out, args, r, step, ok, ctx, state):
    ctx["trends"] = r
    for key in ("a", "b"):
        s = r[key]
        _add(out, step, "trend", f"{_r(s['measure']).title()} ({s['source']}) went from {human_number(s['first'])} to "
             f"{human_number(s['last'])} ({_pct(s['change_pct'])}) between {r['periods'][0]} and {r['periods'][-1]}.",
             {"first": s["first"], "last": s["last"], "change_pct": s["change_pct"]}, ok, subject={"source": s["source"]})
    if r.get("direction_summary"):
        _add(out, step, "relationship", r["direction_summary"], {"opposite": r["opposite_trends"]}, ok)
    if r.get("change_correlation") is not None:
        _add(out, step, "relationship", f"Across {len(r['periods'])} {r['grain']}s, period-to-period changes in the two series "
             f"correlate at {r['change_correlation']:+.2f}; they {r['relationship']}.", {"r": r["change_correlation"]}, ok)
    ctx.setdefault("limitations", []).extend(r.get("limitations", []))


def _correlate(out, args, r, step, ok, ctx, state):
    _add(out, step, "relationship", f"{_r(r['x']).title()} and {_r(r['y'])} have a {r['strength']} {r['direction']} relationship "
         f"(Pearson r = {r['pearson']:.2f}, Spearman {r['spearman']:.2f}, n = {r['n']:,}).",
         {"pearson": r["pearson"], "spearman": r["spearman"], "n": r["n"]}, ok)
    ctx.setdefault("limitations", []).extend(r.get("limitations", []))
    ctx["correlation"] = r


def _distribution(out, args, r, step, ok, ctx, state):
    _add(out, step, "distribution", f"{_r(r['measure']).title()}: median {human_number(r['median'])}, mean {human_number(r['mean'])}, "
         f"range {human_number(r['min'])} to {human_number(r['max'])} across {r['n']:,} values.",
         {"median": r["median"], "mean": r["mean"], "min": r["min"], "max": r["max"], "n": r["n"]}, ok)
    if r["outliers"]:
        _add(out, step, "outliers", f"{r['outliers']:,} value(s) fall outside the usual range (1.5 × IQR rule).", {"outliers": r["outliers"]}, ok)
    ctx["distribution"] = r


def _quality(out, args, r, step, ok, ctx, state):
    if not r["issues"]:
        _add(out, step, "quality", f"No data-quality problems were found in {r['rows']:,} rows.", {"rows": r["rows"]}, ok)
    for i in r["issues"][:8]:
        col = f" in “{i['column']}”" if i.get("column") else ""
        pct = f" ({i['pct']}%)" if i.get("pct") is not None else ""
        _add(out, step, "quality", f"{i['count']:,} {i['kind'].replace('_', ' ')}{col}{pct}.", {"count": i["count"]}, ok,
             subject={"issue": i["kind"], "column": i.get("column")})
    ctx["quality"] = r


def _analysis(out, args, r, step, ok, ctx, state):
    res = r.get("result") or {}
    val = r.get("validation") or {}
    passed = val.get("valid", True)
    cols, rows = res.get("columns", []), res.get("rows", [])
    ctx["analysis"] = r
    if res.get("row_count") == 1 and len(cols) == 1:
        _add(out, step, "value", f"{_r(cols[0]).title()} is {human_number(rows[0][cols[0]])}.", {"value": rows[0][cols[0]]}, passed)
    elif rows and len(cols) >= 2:
        label, value = cols[0], cols[-1]
        top = rows[0]
        _add(out, step, "ranking", f"{top.get(label)} leads on {_r(value)} with {human_number(top.get(value))} "
             f"(of {res.get('row_count')} rows).", {"value": top.get(value), "rows": res.get("row_count")}, passed)
    elif res.get("row_count") == 0:
        _add(out, step, "empty", "The analysis returned no rows.", {"rows": 0}, passed)


def _metric(out, args, r, step, ok, ctx, state):
    cols, rows = r.get("columns", []), r.get("rows", [])
    if rows and len(cols) == 1:
        _add(out, step, "value", f"{r['metric']} = {r['definition']} is {human_number(rows[0][cols[0]])}.", {"value": rows[0][cols[0]]}, True)
    elif rows:
        _add(out, step, "ranking", f"{r['metric']} computed for {len(rows)} groups.", {"rows": len(rows)}, True)
    ctx["metric"] = r


_EXTRACTORS = {
    "compare_periods": _investigation, "period_contribution": _contribution, "detect_anomalies": _anomalies,
    "compare_trends": _trends, "correlate": _correlate, "describe_distribution": _distribution, "data_quality": _quality,
    "analyze_dataframe": _analysis, "calculate_metric": _metric,
}


# --------------------------------------------------------------------------
# interpretation, recommendations, evidence
# --------------------------------------------------------------------------


def chain_of(state) -> list[dict]:
    """The drill-down path, outermost first: the top driver, then each drill step inside it."""
    steps = []
    root_taken = False
    for f in state.findings:
        if not f["validated"]:
            continue
        if f["kind"] == "driver" and not root_taken:
            root_taken = True
            s = f["subject"]
            steps.append({"dimension": s["dimension"], "segment": s["segment"], "share_pct": f["numbers"].get("share_pct"),
                          "delta_pct": None, "delta": f["numbers"].get("delta"), "filters": {}})
        elif f["kind"] == "drill" and root_taken:
            s = f["subject"]
            steps.append({"dimension": s["dimension"], "segment": s["segment"], "share_pct": f["numbers"].get("share_pct"),
                          "delta_pct": f["numbers"].get("delta_pct"), "delta": f["numbers"].get("delta"), "filters": s.get("filters") or {}})
    return steps


def interpret(state) -> list[str]:
    out: list[str] = []
    ctx = state.context
    small = _small_change(state) if state.intent in {"diagnostic", "root_cause"} else None
    if small:
        extra = f" (the series normally moves about ±{small['swing']:.1f}% between periods)" if small.get("swing") else ""
        return [f"The change of {small['pct']:+.1f}% is {small['why']}{extra}, so it looks like ordinary fluctuation rather than something that needs explaining."]
    chain = chain_of(state)
    if chain:
        path = " → ".join(f"{c['segment']}" for c in chain)
        first = chain[0]
        last = chain[-1]
        if first.get("share_pct") is not None and abs(first["share_pct"]) >= 50:
            out.append(f"The change is concentrated rather than broad-based: {first['segment']} ({_r(first['dimension'])}) alone accounts for "
                       f"{abs(first['share_pct']):.0f}% of it" + (f", and inside it the largest piece is {last['segment']}" if len(chain) > 1 else "") + ".")
        elif first.get("share_pct") is not None:
            out.append(f"No single {_r(first['dimension'])} explains most of the change (the largest, {first['segment']}, accounts for "
                       f"{abs(first['share_pct']):.0f}%), so the movement looks spread across several segments.")
        if len(chain) > 1:
            out.append(f"Following the largest contributor step by step leads to {path}; this path is where to look first.")
    for c in ctx.get("concentration", [])[:1]:
        if c["n"] and c["k"] and c["n"] > 3:
            out.append(f"{c['k']} of {c['n']} {_r(c['dimension'])} segments account for 80% of the movement"
                       + (f" within {' / '.join(c['filters'].values())}" if c.get("filters") else "") + ".")
    for f in state.findings:
        if f["kind"] == "slice_change" and f["subject"].get("measure") != ctx.get("measure"):
            n = f["numbers"]
            out.append(f"{_r(f['subject']['measure']).title()} within the same slice moved {_pct(n['delta_pct'])} over the same periods. "
                       "This is consistent with the pattern above but does not prove it is the cause.")
    for f in state.findings:
        if f["kind"] == "relationship" and "opposite" in f["numbers"]:
            out.append("The two sources " + ("moved in opposite directions" if f["numbers"]["opposite"] else "moved in the same direction")
                       + " over the period. An association between two systems is not evidence that one causes the other.")
        if f["kind"] == "relationship" and "r" in f["numbers"]:
            out.append("The two sources move " + ("against each other" if f["numbers"]["r"] <= -0.3 else "together" if f["numbers"]["r"] >= 0.3
                       else "with no clear link") + " over time; an association between systems is not evidence that one causes the other.")
        if f["kind"] == "relationship" and "pearson" in f["numbers"]:
            out.append("Correlation shows that the two move together (or apart); it does not show that one drives the other.")
    for f in state.findings:
        if f["kind"] == "anomaly" and state.intent == "anomaly":
            out.append(f"{f['subject']['period']} is a statistical outlier; whether it reflects a real event or a data issue needs a business check.")
            break
    if not out and state.findings:
        out.append("The computed figures above describe what happened; the data alone does not say why.")
    return out


def recommend(state) -> list[str]:
    recs: list[str] = []
    chain = chain_of(state)
    ctx = state.context
    measure = _r(ctx.get("measure", "the measure"))
    if chain and any(f["kind"] == "change" and f["numbers"].get("delta", 0) < 0 for f in state.findings):
        last = chain[-1]
        scope = " / ".join(dict.fromkeys(c["segment"] for c in chain))
        pct = last.get("delta_pct")
        pct_txt = f" ({_pct(pct)})" if pct is not None else ""
        related = [f for f in state.findings if f["kind"] == "slice_change" and f["subject"].get("measure") != ctx.get("measure")]
        extra = ""
        if related:
            r0 = related[0]
            extra = f" Compare {_r(r0['subject']['measure'])} (which moved {_pct(r0['numbers'].get('delta_pct'))} in this slice)"
        recs.append(f"Investigate the decline in {scope}{pct_txt}: compare pricing, discounts, stock availability and demand for that slice against the previous period.{extra}".strip())
    elif chain:
        recs.append(f"Review what is driving the growth in {' / '.join(dict.fromkeys(c['segment'] for c in chain))} and whether it can be repeated elsewhere.")
    for f in state.findings:
        if f["kind"] == "anomaly" and f["subject"].get("period"):
            recs.append(f"Check whether {f['subject']['period']} reflects a one-off event (promotion, outage, stock-out) or a data problem before using it as a baseline.")
            break
    quality = [f for f in state.findings if f["kind"] == "quality" and f["subject"].get("column")]
    if quality:
        cols = ", ".join(dict.fromkeys(f["subject"]["column"] for f in quality[:3]))
        recs.append(f"Fix the data issues in {cols} at the source before relying on analyses that use those columns.")
    for f in state.findings:
        if f["kind"] == "relationship" and (f["numbers"].get("opposite") or ("r" in f["numbers"] and f["numbers"]["r"] <= -0.3)):
            recs.append("Review how the two areas are planned together (for example demand versus replenishment), by looking at the same segments in each source.")
            break
    return recs[:4]


def evidence_level(state) -> str:
    """Internal evidence quality (not a model-reported confidence number)."""
    if not state.findings:
        return "insufficient"
    if state.intent in {"diagnostic", "root_cause"}:
        small = _small_change(state)
        if small:
            return "insufficient"
    if any(not f["validated"] for f in state.findings):
        return "limited"
    chain = chain_of(state)
    if state.intent in {"diagnostic", "root_cause"}:
        change = next((f for f in state.findings if f["kind"] == "change"), None)
        if change is None or (change["numbers"].get("delta") or 0) == 0:
            return "insufficient"
        if chain and chain[0].get("share_pct") is not None and abs(chain[0]["share_pct"]) >= 60:
            return "strong"
        return "moderate" if chain or any(f["kind"] in {"driver", "hotspot"} for f in state.findings) else "limited"
    if state.intent == "cross_source":
        rel = next((f for f in state.findings if f["kind"] == "relationship"), None)
        return "moderate" if rel else "limited"
    return "strong" if state.findings else "limited"


def executive_summary(state) -> str:
    facts = [f for f in state.findings if f["kind"] in {"change", "value", "trend", "relationship", "distribution", "quality", "anomaly", "ranking"}]
    parts = [f["text"] for f in facts[:2]]
    drill = [f for f in state.findings if f["kind"] in {"drill", "driver"}]
    if drill and state.evidence != "insufficient":  # a drill into noise would present a segment share as if it explained something
        parts.append(drill[0]["text"] if len(chain_of(state)) < 2 else
                     f"Drilling in step by step points to {' → '.join(c['segment'] for c in chain_of(state))}.")
    if state.evidence == "insufficient":
        parts.append(EVIDENCE_NOT_ENOUGH)
    return " ".join(parts) or EVIDENCE_NOT_ENOUGH


def build_insights(state) -> dict:
    facts = [f["text"] for f in state.findings if f["validated"]]
    small = _small_change(state) if state.intent in {"diagnostic", "root_cause"} else None
    if small:  # do not dress a rounding-level change up with segment "shares" of it
        facts = [f["text"] for f in state.findings if f["kind"] == "change" and f["validated"]][:1]
    unvalidated = [f["text"] for f in state.findings if not f["validated"]]
    state.evidence = evidence_level(state)
    insights = {
        "summary": executive_summary(state),
        "facts": facts[:10],
        "interpretation": interpret(state) if (state.evidence != "insufficient" or small) else [EVIDENCE_NOT_ENOUGH],
        "recommendations": recommend(state) if state.evidence in {"strong", "moderate"} else [],
        "evidence": state.evidence,
        "withheld": unvalidated,  # findings that failed validation are never presented as facts
        "limitations": list(dict.fromkeys(state.context.get("limitations", [])))[:6],
    }
    if state.evidence in {"limited", "insufficient"} and not insights["recommendations"]:
        insights["recommendations"] = []
    state.final_insights = insights
    state.recommendations = insights["recommendations"]
    return insights


# --------------------------------------------------------------------------
# LLM wording guard
# --------------------------------------------------------------------------

_NUM = re.compile(r"\d[\d,]*\.?\d*")


def _norm(token: str) -> str:
    token = token.replace(",", "").rstrip(".")
    if "." in token:
        token = token.rstrip("0").rstrip(".")
    return token


def numbers_in(text: str) -> set[str]:
    return {_norm(t) for t in _NUM.findall(text or "")}


def grounded(candidate: str, facts: list[str], extra: str = "") -> bool:
    """True when every number in `candidate` appears in the facts (K/M/B-suffixed figures are compared as written)."""
    allowed = numbers_in(" ".join(facts) + " " + extra)
    return numbers_in(candidate) <= allowed
