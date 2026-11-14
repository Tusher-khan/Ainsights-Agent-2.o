"""Build the API/chat payload from a finished agent run, and run the agent for a question.

`answer_with_agent` is the entry point used by the chat service: it chooses the data sources
(data discovery), runs the loop, and returns a payload in the same shape the UI already renders
(plus an `agent` block with the steps performed, evidence level and budget use).
Private model reasoning is never included: only the concise tool/step log.
"""

from __future__ import annotations

from app.agent import findings as F
from app.agent import loop
from app.agent.intents import BusinessIntent
from app.agent.state import AgentState
from app.core.datasource import from_dataset_record
from app.database import repository
from app.services import usage_service
from app.utils.helpers import human_number, json_safe
from app.utils.logger import get_logger

log = get_logger("agent.response")

PARTIAL_NOTE = "The analysis reached its execution limit before completing the full investigation; what follows is what was verified so far."


def _table(state: AgentState) -> dict | None:
    """A compact result table taken from the most informative validated tool result."""
    for n in sorted(state.tool_results, reverse=True):
        e = state.tool_results[n]
        r, tool = e["result"], e["tool"]
        if not e["validated"] or not isinstance(r, dict):
            continue
        if tool == "period_contribution" and r.get("rows"):
            cols = [r["dimension"], "previous", "current", "delta", "delta_pct"]
            rows = [{r["dimension"]: x["segment"], "previous": x["previous"], "current": x["current"], "delta": x["delta"],
                     "delta_pct": x["delta_pct"]} for x in r["rows"]]
            return {"columns": cols, "rows": rows, "row_count": len(rows)}
        if tool == "detect_anomalies":
            rows = [{"period": p["period"], r["measure"]: p["value"]} for p in r["series"]]
            return {"columns": ["period", r["measure"]], "rows": rows, "row_count": len(rows)}
        if tool == "compare_trends":
            ka, kb = f"{r['a']['measure']} ({r['a']['source']})", f"{r['b']['measure']} ({r['b']['source']})"
            rows = [{"period": p, ka: r["a"]["values"][i], kb: r["b"]["values"][i]} for i, p in enumerate(r["periods"])]
            return {"columns": ["period", ka, kb], "rows": rows, "row_count": len(rows)}
        if tool == "data_quality" and r.get("issues"):
            rows = [{"issue": i["kind"].replace("_", " "), "column": i.get("column", "(all)"), "count": i["count"], "pct": i.get("pct")} for i in r["issues"]]
            return {"columns": ["issue", "column", "count", "pct"], "rows": rows, "row_count": len(rows)}
        if tool == "describe_distribution":
            rows = [{"from": h["low"], "to": h["high"], "count": h["count"]} for h in r["histogram"]]
            return {"columns": ["from", "to", "count"], "rows": rows, "row_count": len(rows)}
    return None


def _kpis(state: AgentState) -> list[dict]:
    out = []
    for f in state.findings:
        if f["kind"] in {"change", "slice_change"} and f["validated"]:
            n = f["numbers"]
            out.append({"label": f["subject"].get("measure", "").replace("_", " ").title() + (" (slice)" if f["kind"] == "slice_change" else ""),
                        "previous": n["previous"], "current": n["current"], "change": n["delta"], "change_pct": n["delta_pct"],
                        "direction": "up" if n["delta"] > 0 else "down" if n["delta"] < 0 else "flat", "display": human_number(n["current"])})
    return out[:4]


def to_payload(state: AgentState, sources: list, mode: str) -> dict:
    ins = state.final_insights
    steps = [{"n": s["n"], "tool": s["tool"], "why": s.get("reason", ""), "decided_by": s.get("source", "rules"), "ok": s["ok"],
              "validated": s["validated"], "ms": s.get("ms", 0), "reused": bool(s.get("reused")), "issues": s.get("issues", []),
              "note": s.get("note", "")} for s in state.completed_steps]
    figures = state.visualization_data
    usage = usage_service.analysis_usage(state.analysis_id)
    answer = ins.get("summary") or F.EVIDENCE_NOT_ENOUGH
    if state.status == "partial" and any("maximum" in e or "limit" in e or "Stopped" in e for e in state.errors):
        ins.setdefault("limitations", []).insert(0, PARTIAL_NOTE)
    primary = sources[0]
    checks = [{"name": f"step {s['n']}: {s['tool']}", "passed": s["validated"],
               "detail": "; ".join(s["issues"]) if s["issues"] else "all checks passed"} for s in steps if s["ok"] and not s["reused"]
              and s["tool"] not in {"get_dataset_metadata", "plan_analysis"}]
    payload = {
        "answer": answer, "insight": answer, "intent": "investigation" if state.intent in {"diagnostic", "root_cause"} else state.intent,
        "business_intent": state.intent, "intent_confidence": 1.0, "terminal": True, "stages": state.stages,
        "methods": {"intent": mode, "plan": state.policy, "insight": ins.get("summary_method", "deterministic")},
        "workflow": "agent loop",
        "source": {"dataset": primary.name, "dataset_id": primary.id, "kind": primary.kind,
                   "operation": "agent: " + ", ".join(dict.fromkeys(s["tool"] for s in steps if s["ok"])),
                   "row_count": primary.row_count, "execution_ms": round(state.elapsed_s * 1000),
                   "datasets": [{"id": s.id, "name": s.name} for s in sources]},
        "assumptions": ins.get("limitations", []),
        "validation": {"verified": bool(checks) and all(c["passed"] for c in checks) and bool(state.findings),
                       "result": {"valid": all(c["passed"] for c in checks), "checks": checks,
                                  "errors": [c["detail"] for c in checks if not c["passed"]], "warnings": ins.get("limitations", [])}},
        "kpis": _kpis(state),
        "agent": {
            "mode": state.policy, "status": state.status, "analysis_id": state.analysis_id, "evidence": state.evidence,
            "intent": state.intent, "complexity": state.complexity,
            "plan": state.analysis_plan, "steps": steps, "facts": ins.get("facts", []), "interpretation": ins.get("interpretation", []),
            "recommendations": ins.get("recommendations", []), "withheld": ins.get("withheld", []),
            "findings": [{k: f[k] for k in ("id", "kind", "text", "numbers", "validated", "step")} for f in state.findings],
            "limitations": ins.get("limitations", []), "errors": state.errors,
            "budget": {"steps_used": len(steps), "max_steps": loop.Config.AGENT_MAX_STEPS, "llm_calls": state.llm_calls,
                       "tokens_used_estimate": state.tokens_used, "elapsed_ms": round(state.elapsed_s * 1000)},
            "usage": usage,
            "mode_note": ("Steps were chosen by the language model (checked against the tool registry), with built-in rules as fallback."
                          if state.policy != "rules" else
                          "No language model was used to choose steps: the built-in rule policy decided each step from the previous result."),
        },
    }
    table = _table(state)
    if table:
        payload["result"] = {**table, "scalar": None, "truncated": False, "execution_ms": round(state.elapsed_s * 1000), "engine": "pandas"}
    if figures:
        payload["visualization"] = figures[0]
        payload["extra_visualizations"] = [f["figure"] for f in figures[1:]]
    return json_safe(payload)


def merge_investigation(payload: dict, state: AgentState) -> None:
    """Diagnostic runs keep the existing investigation view (KPIs, charts, narrative) and extend it with the drill-down."""
    first = next((e for n, e in sorted(state.tool_results.items()) if e["tool"] == "compare_periods" and e["validated"]), None)
    if not first:
        return
    inv = dict(first["result"])
    ins = state.final_insights
    if state.evidence == "insufficient":
        # Nothing material to explain: show the one fact and the honest reading, not segment shares of noise.
        inv["facts"] = list(ins.get("facts", []))[:1]
        inv["interpretation"] = list(ins.get("interpretation", []))
        inv["recommendations"] = []
        inv["drivers"], inv["top_drivers"], inv["hotspots"] = {}, [], []
        inv["summary"] = ins.get("summary", inv.get("summary", ""))
    else:
        inv["facts"] = list(dict.fromkeys(list(inv.get("facts", [])) + [f for f in ins.get("facts", [])]))[:12]
        inv["interpretation"] = list(dict.fromkeys(list(ins.get("interpretation", [])) + list(inv.get("interpretation", []))))
        inv["recommendations"] = list(dict.fromkeys(list(ins.get("recommendations", [])) + list(inv.get("recommendations", []))))[:6]
    inv["how_calculated"] = list(inv.get("how_calculated", [])) + [
        "Drill-down steps compare the same two periods inside the slice found in the previous step (segment shares reconcile to the slice's total change).",
        "Evidence level is judged internally from validation and concentration, not reported by a model.",
    ]
    inv["limitations"] = list(dict.fromkeys(list(inv.get("limitations", [])) + list(ins.get("limitations", []))))
    inv["evidence"] = state.evidence
    if state.evidence in {"insufficient", "limited"}:
        inv["confidence"] = "low"  # the headline label must agree with the evidence level
    payload["investigation"] = inv
    payload["headline"] = None
    payload["answer"] = inv.get("headline") or payload["answer"]
    payload["assumptions"] = inv.get("assumptions", [])
    payload["source"]["columns_used"] = sorted({inv["measure"], inv["time_column"], *inv["drivers"].keys()})
    payload["source"]["operation"] = "agent: period comparison + iterative drill-down"


def run_for_question(question: str, intent: BusinessIntent, sources: list, *, cross_measures: dict | None = None) -> tuple[AgentState, dict]:
    state = loop.run(question, intent.name, sources, complexity=intent.complexity, cross_measures=cross_measures)
    payload = to_payload(state, sources, intent.method)
    if intent.name in {"diagnostic", "root_cause"}:
        merge_investigation(payload, state)
    return state, payload


# ---------------------------------------------------------------------------
# data discovery for cross-source questions
# ---------------------------------------------------------------------------

_CONNECTORS = ("while", "versus", " vs", "compared with", "against", " and ", "even though", "but ")


_GENERIC = {"total", "units", "unit", "amount", "count", "number", "value", "date", "avg", "average", "sum", "the", "of"}


def _tokens(name: str) -> list[str]:
    return [t for t in name.lower().replace("_", " ").split() if len(t) > 2 and t not in _GENERIC]


def find_cross_sources(question: str) -> tuple[list, dict] | None:
    """Two datasets that each own a measure named in the question (e.g. sales and inventory), each with a date column.
    None when the question is not a cross-source one."""
    import re

    from app.ai import investigation_engine as ie

    text = question.lower()
    if not any(c in text for c in _CONNECTORS):
        return None
    words = set(re.findall(r"[a-z]+", text))
    for _w, group in ie._SYNONYMS.items():  # "revenue" should find a "sales" column and vice versa
        if words & set(group):
            words |= set(group)
    best: dict[str, tuple[int, int, str, dict]] = {}  # dataset id -> (score, position, column, record)
    for record in repository.list_datasets():
        cols = (record.get("profile") or {}).get("columns", [])
        if not any(c.get("semantic_type") == "datetime" for c in cols):
            continue
        for c in cols:
            if c.get("semantic_type") != "numeric":
                continue
            toks = _tokens(c["name"])
            score = sum(1 for t in toks if t in words)
            if not score:
                continue
            pos = min((text.find(t) for t in toks if t in text), default=9999)
            if record["id"] not in best or score > best[record["id"]][0]:
                best[record["id"]] = (score, pos, c["name"], record)
    if len(best) < 2:
        return None
    chosen = sorted(best.values(), key=lambda v: (-v[0], v[1]))[:2]
    chosen.sort(key=lambda v: v[1])  # the first measure mentioned is the one being explained
    sources = [from_dataset_record(v[3]) for v in chosen]
    return sources, {v[3]["id"]: v[2] for v in chosen}
