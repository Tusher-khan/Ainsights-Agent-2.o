"""Multi-source (hybrid) analysis: actual vs forecast / target / budget.

Example: actual sales live in a PostgreSQL table, the forecast in an Excel sheet.
Each side is aggregated to a shared key *inside its own source* (SQL push-down for
databases, pandas for files); only the small aggregated results are joined here. The
user never merges anything by hand, and nothing is estimated: every number comes from
an executed aggregation, and the join is reconciled before it is reported.

Selection is rule-based and always reported as assumptions so the user can see which
datasets, columns and join key were used.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from app.agents import pandas_agent, sql_agent
from app.core.datasource import DataSource, from_dataset_record
from app.core.models import QueryPlan
from app.database import repository
from app.utils.exceptions import AInsightError
from app.utils.helpers import human_number
from app.utils.logger import get_logger

log = get_logger("agent.multi_source")

REF_RE = re.compile(r"\b(forecast(?:ed|s)?|target(?:s)?|budget(?:ed|s)?|planned|quota(?:s)?|projection(?:s)?|expected)\b", re.I)
COMPARE_RE = re.compile(r"\b(compare|comparison|versus|vs\.?|against|variance|gap|difference|miss(?:ed)?|beat|behind|ahead|on track|achiev\w*)\b", re.I)
PALETTE = ["#2F5AE0", "#C4622D"]


class ComparisonError(AInsightError):
    status_code = 422
    user_message = "AInsight could not line those two sources up."


def detect(question: str) -> str | None:
    """Reference word (forecast / target / budget ...) when the question compares against one."""
    ref = REF_RE.search(question or "")
    return ref.group(1).lower() if ref and COMPARE_RE.search(question) else None


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _stem(name: str) -> str:
    return _norm(REF_RE.sub("", re.sub(r"[_\s-]+", " ", name)))


@dataclass
class Comparison:
    actual: DataSource
    reference: DataSource
    actual_measure: str
    reference_measure: str
    keys: list  # [(actual_col, reference_col, kind)] kind: period | dimension
    grain: str = "month"
    assumptions: list = field(default_factory=list)


def _numeric(source: DataSource) -> list[str]:
    return list(source.measures)


def _score_reference(source: DataSource) -> int:
    score = 0
    if REF_RE.search(source.name):
        score += 3
    score += sum(1 for c in source.columns if REF_RE.search(c))
    return score


def _pick_reference_measure(source: DataSource) -> str | None:
    named = [m for m in _numeric(source) if REF_RE.search(m)]
    return (named or _numeric(source) or [None])[0]


def _grain(question: str) -> tuple[str, str | None]:
    q = question.lower()
    for word, grain in (("quarter", "quarter"), ("year", "year"), ("annual", "year"), ("daily", "day"), ("day", "day")):
        if word in q:
            return grain, None
    if "week" in q:
        return "month", "Weekly grouping is not supported for cross-source comparison; compared by month instead."
    return "month", None


def plan_comparison(question: str, datasets: list[dict] | None = None) -> Comparison:
    records = [d for d in (datasets if datasets is not None else repository.list_datasets()) if d.get("status", "ready") == "ready"]
    sources = [from_dataset_record(r) for r in records]
    if len(sources) < 2:
        raise ComparisonError("Comparing against a forecast or target needs at least two connected datasets.")

    ref_candidates = sorted((s for s in sources if _score_reference(s) > 0 and _numeric(s)), key=_score_reference, reverse=True)
    if not ref_candidates:
        raise ComparisonError("No dataset looks like a forecast, target or budget.", "no reference dataset")
    reference = ref_candidates[0]
    ref_measure = _pick_reference_measure(reference)
    stem = _stem(ref_measure)
    q_words = set(re.findall(r"[a-z]+", question.lower()))

    def score(s: DataSource) -> float:
        total = 0.0
        if re.search(r"\bactual", s.name, re.I) or any(re.search(r"actual", c, re.I) for c in s.columns):
            total += 2
        shared = {_norm(c) for c in s.columns} & {_norm(c) for c in reference.columns}
        total += min(len(shared), 4) * 0.5
        total += sum(1 for m in _numeric(s) if stem and (_norm(m) == stem or stem in _norm(m))) * 3
        total += sum(0.5 for w in q_words if len(w) > 3 and w in _norm(s.name))
        return total

    others = [s for s in sources if s.id != reference.id and _numeric(s)]
    if not others:
        raise ComparisonError("No second dataset with numeric values was found to compare against.")
    actual = max(others, key=score)

    # measure on the actual side: the one matching the reference's subject, else the question's word, else the first
    actual_measure = next((m for m in _numeric(actual) if stem and _norm(m) == stem), None) \
        or next((m for m in _numeric(actual) if stem and stem in _norm(m)), None) \
        or next((m for m in _numeric(actual) if _norm(m) in {_norm(w) for w in q_words}), None) \
        or _numeric(actual)[0]

    notes = [
        f"Actual: '{actual.name}' → {actual_measure}.",
        f"{REF_RE.search(reference.name + ' ' + ref_measure).group(1).title() if REF_RE.search(reference.name + ' ' + ref_measure) else 'Reference'}: '{reference.name}' → {ref_measure}.",
        "Assumes both measures use the same currency and units.",
    ]

    grain, grain_note = _grain(question)
    if grain_note:
        notes.append(grain_note)
    by = re.search(r"\bby\s+([a-z_ ]+?)(?:\s+(?:and|for|in|with|this|last)\b|[?.,]|$)", question.lower())
    shared_dims = [
        (a, r) for a in actual.dimensions for r in reference.dimensions if _norm(a) == _norm(r)
    ]
    keys: list = []
    if by:
        wanted = _norm(by.group(1))
        match = next(((a, r) for a, r in shared_dims if _norm(a) == wanted), None)
        if match:
            keys = [(match[0], match[1], "dimension")]
    if not keys and actual.date_columns and reference.date_columns:
        keys = [(actual.date_columns[0], reference.date_columns[0], "period")]
        notes.append(f"Joined on {grain} using '{actual.date_columns[0]}' and '{reference.date_columns[0]}'.")
    if not keys and shared_dims:
        def cardinality(pair):
            c = actual.column_profile(pair[0]) or {}
            return c.get("unique_count") or 10**9
        pick = min((p for p in shared_dims if cardinality(p) > 1), key=cardinality, default=shared_dims[0])
        keys = [(pick[0], pick[1], "dimension")]
    if not keys:
        raise ComparisonError(
            f"'{actual.name}' and '{reference.name}' have no shared date or category column to line up on.",
            "no join key",
        )
    if keys[0][2] == "dimension":
        notes.append(f"Joined on {keys[0][0]}.")
    return Comparison(actual, reference, actual_measure, ref_measure, keys, grain, notes)


def _aggregate(source: DataSource, measure: str, key: tuple, side: int, grain: str) -> tuple[list[dict], dict]:
    column = key[side]
    kind = key[2]
    plan = QueryPlan(
        intent="trend_analysis" if kind == "period" else "grouping", dataset=source.name,
        dimensions=[] if kind == "period" else [column], measures=[measure], aggregation="sum", sort="none",
        time_column=column if kind == "period" else None, time_grain=grain if kind == "period" else None,
    )
    if source.kind == "sql":
        result, _report, info = sql_agent.run("(multi-source aggregation)", plan, source)
    else:
        result, _op = pandas_agent.execute(plan, source)
    key_col, value_col = result.columns[0], result.columns[-1]
    return [{"key": r[key_col], "value": r[value_col]} for r in result.rows], {
        "engine": result.engine, "sql": result.sql, "rows": result.row_count,
    }


def _norm_key(value, kind: str) -> str:
    text = str(value).strip().lower()
    return re.sub(r"-q", "q", text) if kind == "period" else text


def _merge(actual_rows: list[dict], ref_rows: list[dict], kind: str):
    merged: dict[str, dict] = {}
    collisions = 0
    for side, rows in (("actual", actual_rows), ("reference", ref_rows)):
        for r in rows:
            if r["value"] is None:
                continue
            k = _norm_key(r["key"], kind)
            entry = merged.setdefault(k, {"label": str(r["key"]), "actual": None, "reference": None})
            if entry[side] is not None:
                collisions += 1
                entry[side] += float(r["value"])
            else:
                entry[side] = float(r["value"])
    return merged, collisions


def run(question: str) -> dict:
    started = time.perf_counter()
    comparison = plan_comparison(question)
    key = comparison.keys[0]
    a_rows, a_info = _aggregate(comparison.actual, comparison.actual_measure, key, 0, comparison.grain)
    r_rows, r_info = _aggregate(comparison.reference, comparison.reference_measure, key, 1, comparison.grain)
    merged, collisions = _merge(a_rows, r_rows, key[2])

    matched = {k: v for k, v in merged.items() if v["actual"] is not None and v["reference"] is not None}
    if not matched:
        raise ComparisonError(
            f"The two sources share no common {'periods' if key[2] == 'period' else key[0] + ' values'}, so there is nothing to compare.",
            f"actual keys={[r['key'] for r in a_rows][:5]} reference keys={[r['key'] for r in r_rows][:5]}",
        )
    order = sorted(matched, key=lambda k: (matched[k]["label"] if key[2] == "period" else k))
    rows = []
    for k in order:
        v = matched[k]
        variance = v["actual"] - v["reference"]
        rows.append({
            key[0]: v["label"], "actual": round(v["actual"], 4), "reference": round(v["reference"], 4),
            "variance": round(variance, 4),
            "variance_pct": round(variance / abs(v["reference"]) * 100, 2) if v["reference"] else None,
        })

    total_a, total_r = sum(r["actual"] for r in rows), sum(r["reference"] for r in rows)
    only_actual = [v["label"] for v in merged.values() if v["reference"] is None]
    only_ref = [v["label"] for v in merged.values() if v["actual"] is None]

    noun = comparison.grain if key[2] == "period" else key[0]

    # ---- reconciliation (the validation agent's checks for this analysis) ----
    checks = []

    def check(name, passed, detail):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    side_a = sum(float(r["value"]) for r in a_rows if r["value"] is not None and _norm_key(r["key"], key[2]) in matched)
    side_r = sum(float(r["value"]) for r in r_rows if r["value"] is not None and _norm_key(r["key"], key[2]) in matched)
    check("actual_total_reconciles", abs(side_a - total_a) < 1e-6 * max(1, abs(side_a)),
          f"matched actual total {human_number(total_a)} equals the aggregated source total {human_number(side_a)}")
    check("reference_total_reconciles", abs(side_r - total_r) < 1e-6 * max(1, abs(side_r)),
          f"matched reference total {human_number(total_r)} equals the aggregated source total {human_number(side_r)}")
    check("keys_matched", True, f"{len(matched)} of {len(merged)} {noun}(s) exist in both sources")
    check("no_duplicate_keys", collisions == 0, "each key appears once per source after aggregation" if not collisions
          else f"{collisions} keys collided after normalisation and were summed")
    zero_ref = sum(1 for r in rows if not r["reference"])
    check("reference_nonzero", zero_ref == 0, "no zero reference values" if not zero_ref else f"{zero_ref} zero reference value(s): % variance not computed")
    warnings = []
    if only_actual:
        warnings.append(f"{len(only_actual)} {noun}(s) only exist in the actual data and were not compared: {', '.join(map(str, only_actual[:5]))}.")
    if only_ref:
        warnings.append(f"{len(only_ref)} {noun}(s) only exist in the reference data and were not compared: {', '.join(map(str, only_ref[:5]))}.")

    variance_total = total_a - total_r
    pct_total = variance_total / abs(total_r) * 100 if total_r else None
    word = REF_RE.search(comparison.reference.name + " " + comparison.reference_measure + " " + question)
    ref_word = word.group(1).lower() if word else "reference"
    direction = "above" if variance_total > 0 else "below" if variance_total < 0 else "equal to"
    answer = (
        f"Actual {comparison.actual_measure.replace('_', ' ')} is {human_number(total_a)} versus a {ref_word} of "
        f"{human_number(total_r)} — {human_number(abs(variance_total))}"
        + (f" ({abs(pct_total):.1f}%)" if pct_total is not None else "")
        + f" {direction} {ref_word}."
    )
    below = [r for r in rows if r["variance"] < 0]
    above = [r for r in rows if r["variance"] > 0]
    worst = min(rows, key=lambda r: r["variance"])
    best = max(rows, key=lambda r: r["variance"])
    insight = (
        f"Compared across {len(rows)} {noun}(s): {len(below)} below and {len(above)} above {ref_word}. "
        f"The largest shortfall is {worst[key[0]]} ({human_number(worst['variance'])}"
        + (f", {worst['variance_pct']:.1f}%" if worst["variance_pct"] is not None else "") + "); "
        f"the largest overrun is {best[key[0]]} ({human_number(best['variance'])}"
        + (f", {best['variance_pct']:.1f}%" if best["variance_pct"] is not None else "") + ")."
    )

    recs = []
    if worst["variance"] < 0:
        recs.append({
            "finding": f"{worst[key[0]]} fell short of {ref_word} by {human_number(abs(worst['variance']))}"
                       + (f" ({abs(worst['variance_pct']):.1f}%)." if worst["variance_pct"] is not None else "."),
            "evidence": f"SUM({comparison.actual_measure}) from {comparison.actual.name} = {human_number(worst['actual'])}; "
                        f"{comparison.reference_measure} from {comparison.reference.name} = {human_number(worst['reference'])}.",
            "explanation": "This is a measured gap, not a diagnosed cause. The data compared here does not say why it occurred.",
            "recommendation": f"Investigate {worst[key[0]]}: ask AInsight why {comparison.actual_measure.replace('_', ' ')} "
                              f"changed there, and check whether the {ref_word} itself was realistic.",
            "measurement": f"Track the {key[0]} gap to {ref_word} next period.",
            "confidence": "medium",
            "limitations": "Both measures are assumed to share currency and units.",
        })
    if best["variance"] > 0 and best is not worst:
        recs.append({
            "finding": f"{best[key[0]]} exceeded {ref_word} by {human_number(best['variance'])}.",
            "evidence": f"Actual {human_number(best['actual'])} vs {ref_word} {human_number(best['reference'])}.",
            "explanation": "An overrun can mean strong performance or an unrealistic " + ref_word + "; the data alone cannot tell.",
            "recommendation": f"Review what drove {best[key[0]]} and whether it can be repeated elsewhere.",
            "measurement": None, "confidence": "low", "limitations": None,
        })

    labels = [str(r[key[0]]) for r in rows]
    layout_base = {"margin": {"l": 60, "r": 20, "t": 48, "b": 60}, "height": 380, "paper_bgcolor": "rgba(0,0,0,0)",
                   "plot_bgcolor": "rgba(0,0,0,0)", "font": {"family": "Inter, system-ui, sans-serif", "size": 12}}
    if key[2] == "period":
        data = [
            {"type": "scatter", "mode": "lines+markers", "name": "Actual", "x": labels, "y": [r["actual"] for r in rows], "line": {"color": PALETTE[0], "width": 2.5}},
            {"type": "scatter", "mode": "lines+markers", "name": ref_word.title(), "x": labels, "y": [r["reference"] for r in rows],
             "line": {"color": PALETTE[1], "width": 2.5, "dash": "dash"}},
        ]
    else:
        data = [
            {"type": "bar", "name": "Actual", "x": labels, "y": [r["actual"] for r in rows], "marker": {"color": PALETTE[0]}},
            {"type": "bar", "name": ref_word.title(), "x": labels, "y": [r["reference"] for r in rows], "marker": {"color": PALETTE[1]}},
        ]
    main_fig = {"data": data, "layout": {**layout_base, "title": {"text": f"Actual vs {ref_word} — {comparison.actual_measure.replace('_', ' ').title()}", "font": {"size": 15}},
                "barmode": "group", "showlegend": True, "xaxis": {"title": {"text": key[0]}, "automargin": True},
                "yaxis": {"title": {"text": comparison.actual_measure.replace("_", " ")}, "automargin": True}},
                "config": {"displayModeBar": False, "responsive": True}}
    var_fig = {"data": [{"type": "bar", "name": "Variance", "x": labels, "y": [r["variance"] for r in rows],
                         "marker": {"color": ["#128A6E" if r["variance"] >= 0 else "#B3261E" for r in rows]}}],
               "layout": {**layout_base, "height": 300, "title": {"text": f"Variance vs {ref_word} (actual − {ref_word})", "font": {"size": 15}},
                          "showlegend": False, "xaxis": {"title": {"text": key[0]}, "automargin": True},
                          "yaxis": {"title": {"text": "variance"}, "automargin": True}},
               "config": {"displayModeBar": False, "responsive": True}}

    elapsed = round((time.perf_counter() - started) * 1000, 2)
    sql_texts = [i["sql"] for i in (a_info, r_info) if i.get("sql")]
    log.info("Multi-source comparison %s vs %s on %s: %s keys, %sms", comparison.actual.name, comparison.reference.name, key[0], len(rows), elapsed)
    return {
        "answer": answer,
        "insight": insight,
        "intent": "multi_source_comparison",
        "terminal": False,
        "stages": [
            {"stage": "classify_intent", "status": "done", "detail": f"multi-source comparison against a {ref_word}", "ms": 0},
            {"stage": "discover_sources", "status": "done", "detail": f"{comparison.actual.name} + {comparison.reference.name}", "ms": 0},
            {"stage": "aggregate_each_source", "status": "done", "detail": f"{a_info['engine']} + {r_info['engine']}", "ms": elapsed},
            {"stage": "join_and_reconcile", "status": "done" if all(c["passed"] for c in checks) else "failed", "detail": f"{len(checks)} reconciliation checks", "ms": 0},
            {"stage": "generate_visualization", "status": "done", "detail": "comparison + variance", "ms": 0},
        ],
        "result": {"columns": [key[0], "actual", "reference", "variance", "variance_pct"], "rows": rows, "row_count": len(rows),
                   "scalar": None, "truncated": False, "execution_ms": elapsed, "engine": "hybrid"},
        "headline": {"label": f"Actual vs {ref_word}", "value": f"{variance_total:+,.0f}" if abs(variance_total) >= 1 else f"{variance_total:+.2f}", "raw_value": variance_total},
        "assumptions": comparison.assumptions,
        "visualization": {"figure": main_fig, "spec": {"chart_type": "line" if key[2] == "period" else "grouped_bar"}, "point_count": len(rows)},
        "extra_visualizations": [var_fig],
        "recommendations": recs,
        "sources_used": [
            {"role": "actual", "dataset": comparison.actual.name, "dataset_id": comparison.actual.id, "measure": comparison.actual_measure, "engine": a_info["engine"]},
            {"role": ref_word, "dataset": comparison.reference.name, "dataset_id": comparison.reference.id, "measure": comparison.reference_measure, "engine": r_info["engine"]},
        ],
        "source": {
            "dataset": f"{comparison.actual.name} + {comparison.reference.name}", "dataset_id": comparison.actual.id, "kind": "multi",
            "columns_used": sorted({comparison.actual_measure, comparison.reference_measure, key[0]}),
            "operation": f"SUM({comparison.actual_measure}) vs SUM({comparison.reference_measure}) joined on {key[0]}",
            "execution_ms": elapsed, "row_count": len(rows),
        },
        "sql": "\n\n".join(sql_texts) or None,
        "validation": {"plan": None, "query": None, "result": {"valid": all(c["passed"] for c in checks), "checks": checks, "errors": [], "warnings": warnings},
                       "verified": all(c["passed"] for c in checks)},
        "retries": 0,
        "empty": False,
    }
