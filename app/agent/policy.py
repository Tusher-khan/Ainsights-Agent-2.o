"""Decision policies: who chooses the next tool.

Both policies see only the agent state (question, intent, sources, what has been run and the
findings so far) and answer with a `Decision`. The loop validates every decision against the
tool registry before anything runs, so neither policy can call an unknown tool, use another
user's id, or skip permission checks.

* `RulesPolicy`  – deterministic and *result-aware*: the next step depends on what the
  previous step found (e.g. the largest contributor becomes the filter of the next drill-down).
  Used when no LLM is configured, or when the LLM's answer cannot be used.
* `LLMPolicy`    – asks the configured model to choose the next tool from the tool
  descriptions and the compact findings so far. The model never supplies numbers.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.ai import investigation_engine as ie
from app.agent import findings as F
from app.utils.exceptions import LLMError
from app.utils.logger import get_logger

log = get_logger("agent.policy")

MAX_DRILL_DEPTH = 3
MIN_MATERIAL_PCT = 2.0  # a change smaller than this (in %) is treated as noise, not investigated
MIN_DRILL_SHARE = 30.0  # % of the change a segment must explain before drilling deeper is worthwhile
RELATED_HINTS = ("discount", "margin", "price", "quantity", "units", "cost", "profit", "orders")


@dataclass
class Decision:
    action: str  # "call" | "finish"
    tool: str | None = None
    args: dict = field(default_factory=dict)
    reason: str = ""
    source: str = "rules"
    plan: list[str] | None = None


def finish(reason: str, source: str = "rules") -> Decision:
    return Decision("finish", reason=reason, source=source)


def call(tool: str, args: dict, reason: str, source: str = "rules") -> Decision:
    return Decision("call", tool, args, reason, source)


# --------------------------------------------------------------------------
# helpers shared by the policies
# --------------------------------------------------------------------------


def done(state, tool: str, **match) -> list[dict]:
    """Completed (successful) steps of `tool` whose recorded args contain `match`."""
    out = []
    for s in state.completed_steps:
        if s["tool"] == tool and s["ok"] and all(s["args"].get(k) == v for k, v in match.items()):
            out.append(s)
    return out


def meta_for(state, dataset_id: str) -> dict | None:
    return state.context.get("meta", {}).get(dataset_id)


def pick_measure(question: str, measures: list[str]) -> str | None:
    """Measure named in the question, via synonyms, else a headline business measure."""
    text = question.lower()
    for m in measures:
        if m.lower().replace("_", " ") in text:
            return m
    for _word, group in ie._SYNONYMS.items():
        if any(re.search(rf"\b{re.escape(t)}\b", text) for t in group):
            for m in measures:
                if any(t in m.lower() for t in group):
                    return m
    for hint in ie._MEASURE_PRIORITY:
        for m in measures:
            if hint in m.lower() and not ie._MEAN_HINT.search(m):
                return m
    return next((m for m in measures if not ie._MEAN_HINT.search(m)), measures[0] if measures else None)


def measures_named(question: str, measures: list[str]) -> list[str]:
    text = question.lower()
    hit = [m for m in measures if m.lower().replace("_", " ") in text]
    for _w, group in ie._SYNONYMS.items():
        if any(re.search(rf"\b{re.escape(t)}\b", text) for t in group):
            hit += [m for m in measures if any(t in m.lower() for t in group) and m not in hit]
    return hit


def primary(state) -> dict | None:
    return state.selected_sources[0] if state.selected_sources else None


def usable_dimensions(meta: dict) -> list[dict]:
    cols = {c["name"]: c for c in meta.get("columns", [])}
    return [cols[d] for d in meta.get("dimensions", []) if d in cols and 2 <= (cols[d].get("distinct") or 0) <= 40]


# --------------------------------------------------------------------------
# rules policy
# --------------------------------------------------------------------------


class RulesPolicy:
    name = "rules"

    def outline(self, state) -> list[str]:
        """The *expected* shape of the analysis. The steps actually taken depend on the results."""
        i = state.intent
        base = ["Inspect the available data"]
        if i in {"diagnostic", "root_cause"}:
            return base + ["Measure the change between the latest two periods",
                           "Find which segments contributed most",
                           "Drill into the largest contributor while it explains most of the change",
                           "Check related measures inside that slice", "Validate, visualize and explain"]
        if i == "anomaly":
            return base + ["Scan the series for unusual periods", "Break down the most unusual period", "Validate, visualize and explain"]
        if i == "correlation":
            return base + ["Measure the relationship between the two columns", "Validate, visualize and explain"]
        if i == "distribution":
            return base + ["Describe the distribution and outliers", "Validate, visualize and explain"]
        if i == "data_quality":
            return base + ["Scan every row for quality problems", "Explain what to fix"]
        if i == "cross_source":
            return base + ["Aggregate each measure inside its own source", "Align the sources by period and relate them",
                           "Look for where the first source's change is concentrated", "Validate, visualize and explain"]
        return base + ["Plan the analysis", "Run it", "Validate, visualize and explain"]

    # -- main entry ------------------------------------------------------
    def decide(self, state) -> Decision:
        sources = state.selected_sources
        if not sources:
            return finish("no data source is available")
        for s in sources:  # always start by looking at the data
            if not done(state, "get_dataset_metadata", dataset_id=s["id"]):
                return call("get_dataset_metadata", {"dataset_id": s["id"]}, f"inspect “{s['name']}” before choosing an analysis")
        handler = {
            "diagnostic": self._diagnose, "root_cause": self._diagnose, "anomaly": self._anomaly,
            "correlation": self._correlation, "distribution": self._distribution, "data_quality": self._quality,
            "cross_source": self._cross_source,
        }.get(state.intent, self._generic)
        return handler(state)

    # -- strategies ------------------------------------------------------
    def _diagnose(self, state) -> Decision:
        src = primary(state)
        meta = meta_for(state, src["id"]) or {}
        if not done(state, "compare_periods"):
            if not meta.get("date_columns"):
                state.errors.append("This dataset has no date column, so a change over time cannot be explained.")
                return finish("no date column")
            return call("compare_periods", {"dataset_id": src["id"], "question": state.question},
                        "measure the change and rank every segment's contribution")
        ctx = state.context
        inv = ctx.get("inv") or {}
        if not inv:
            return finish("the period comparison did not produce a result")
        measure = ctx["measure"]
        grain = ctx.get("grain", "month")
        sum_based = inv.get("aggregation") == "sum" and (ctx.get("total_delta") or 0) != 0
        change = next((f for f in state.findings if f["kind"] == "change"), None)
        pct = change["numbers"].get("delta_pct") if change else None
        if pct is not None and abs(pct) < MIN_MATERIAL_PCT:
            state.context["immaterial"] = True  # too small to explain: further drilling would only chase noise
            return finish(f"the change ({pct:+.1f}%) is too small to investigate further")

        # --- drill into the largest contributor, as long as it explains most of the change ---
        chain = F.chain_of(state)
        if sum_based and chain and len(chain) <= MAX_DRILL_DEPTH:
            last = chain[-1]
            share = last.get("share_pct")
            filters = dict(last.get("filters") or {})
            filters[last["dimension"]] = last["segment"]
            if share is not None and abs(share) >= MIN_DRILL_SHARE and len(chain) < MAX_DRILL_DEPTH + 1:
                used = set(filters)
                tried = sum(1 for st in state.completed_steps if st["tool"] == "period_contribution"
                            and st["args"].get("dimension") and st["args"].get("filters") == {str(k): str(v)[:40] for k, v in filters.items()})
                candidates = self._next_dimensions(meta, inv, used)[: max(0, 2 - tried)]
                for dim in candidates:
                    args = {"dataset_id": src["id"], "measure": measure, "dimension": dim, "filters": filters, "grain": grain}
                    if not self._already(state, args):
                        return call("period_contribution", args,
                                    f"{last['segment']} explains {abs(share):.0f}% of the change; see what drives it by {dim}")
            ctx["final_filters"] = filters
        elif sum_based and not chain:
            ctx["final_filters"] = {}
        else:
            ctx["final_filters"] = {}

        # --- related measures inside the final slice (discount, margin, quantity ...) ---
        filters = ctx.get("final_filters", {})
        if sum_based or filters:
            for other in self._related_measures(meta, measure)[:2]:
                args = {"dataset_id": src["id"], "measure": other, "filters": filters or None, "grain": grain}
                args = {k: v for k, v in args.items() if v is not None}
                if not self._already(state, args):
                    return call("period_contribution", args,
                                f"check whether {other} moved in the same slice" if filters else f"check whether {other} moved overall")
        return finish("enough evidence gathered")

    def _next_dimensions(self, meta: dict, inv: dict, used: set) -> list[str]:
        """Unused dimensions, ordered by how concentrated the top-level breakdown already looked."""
        scores = {}
        for dim, rows in (inv.get("drivers") or {}).items():
            shares = [abs(r.get("share_of_change_pct") or 0) for r in rows]
            scores[dim] = max(shares) if shares else 0
        usable = [c["name"] for c in usable_dimensions(meta) if c["name"] not in used]
        return sorted(usable, key=lambda d: -scores.get(d, 0))

    def _related_measures(self, meta: dict, measure: str) -> list[str]:
        others = [m for m in meta.get("measures", []) if m != measure]
        ranked = []
        for hint in RELATED_HINTS:
            ranked += [m for m in others if hint in m.lower() and m not in ranked]
        return ranked

    def _already(self, state, args: dict) -> bool:
        for s in state.completed_steps:
            if s["tool"] == "period_contribution" and all(
                    (s["args"].get(k) == v) if not isinstance(v, dict) else
                    ({str(a): str(b)[:40] for a, b in v.items()} == s["args"].get(k)) for k, v in args.items()):
                return True
        return False

    def _anomaly(self, state) -> Decision:
        src = primary(state)
        meta = meta_for(state, src["id"]) or {}
        measure = pick_measure(state.question, meta.get("measures", []))
        if not measure or not meta.get("date_columns"):
            state.errors.append("Anomaly detection needs a numeric measure and a date column.")
            return finish("missing measure or date column")
        grain = "week" if re.search(r"\bweek", state.question, re.I) else "month"
        if not done(state, "detect_anomalies"):
            return call("detect_anomalies", {"dataset_id": src["id"], "measure": measure, "grain": grain},
                        "scan the series for periods that are unusual for it")
        anomalies = state.context.get("anomalies") or []
        if anomalies and sum(1 for s in state.completed_steps if s["tool"] == "period_contribution") < 1:
            dims = usable_dimensions(meta)
            if dims:
                lead = anomalies[-1]
                return call("period_contribution", {"dataset_id": src["id"], "measure": measure, "dimension": dims[0]["name"],
                                                    "grain": grain, "current_period": lead["period"]},
                            f"break down {lead['period']}, the most recent unusual period")
        return finish("anomaly scan complete")

    def _correlation(self, state) -> Decision:
        src = primary(state)
        meta = meta_for(state, src["id"]) or {}
        measures = meta.get("measures", [])
        named = measures_named(state.question, measures)
        pair = (named + [m for m in measures if m not in named])[:2]
        if len(pair) < 2:
            state.errors.append("A correlation needs two numeric columns.")
            return finish("fewer than two numeric columns")
        if not done(state, "correlate"):
            return call("correlate", {"dataset_id": src["id"], "x": pair[0], "y": pair[1]}, f"measure how {pair[0]} relates to {pair[1]}")
        return finish("relationship measured")

    def _distribution(self, state) -> Decision:
        src = primary(state)
        meta = meta_for(state, src["id"]) or {}
        measure = pick_measure(state.question, meta.get("measures", []))
        if not measure:
            state.errors.append("There is no numeric column to describe.")
            return finish("no numeric column")
        if not done(state, "describe_distribution"):
            return call("describe_distribution", {"dataset_id": src["id"], "measure": measure}, f"describe how {measure} is distributed")
        return finish("distribution described")

    def _quality(self, state) -> Decision:
        src = primary(state)
        if not done(state, "data_quality"):
            return call("data_quality", {"dataset_id": src["id"]}, "scan every row for quality problems")
        return finish("scan complete")

    def _cross_source(self, state) -> Decision:
        if len(state.selected_sources) < 2:
            state.errors.append("Two sources are needed to compare them.")
            return finish("only one source")
        a, b = state.selected_sources[0], state.selected_sources[1]
        ma, mb = meta_for(state, a["id"]) or {}, meta_for(state, b["id"]) or {}
        measure_a = state.context.get("cross_measures", {}).get(a["id"]) or pick_measure(state.question, ma.get("measures", []))
        measure_b = state.context.get("cross_measures", {}).get(b["id"]) or pick_measure(state.question, mb.get("measures", []))
        if not measure_a or not measure_b or not ma.get("date_columns") or not mb.get("date_columns"):
            state.errors.append("Each source needs a numeric measure and a date column to be compared over time.")
            return finish("missing measure or date column")
        if not done(state, "compare_trends"):
            return call("compare_trends", {"dataset_a": a["id"], "measure_a": measure_a, "dataset_b": b["id"], "measure_b": measure_b,
                                           "grain": "month"}, "aggregate each measure inside its own source and align by period")
        tr = state.context.get("trends") or {}
        if tr.get("a", {}).get("change", 0) < 0:
            dims = usable_dimensions(ma)
            if dims and not done(state, "period_contribution"):
                return call("period_contribution", {"dataset_id": a["id"], "measure": measure_a, "dimension": dims[0]["name"],
                                                    "grain": "month"}, f"{measure_a} fell; see where the fall is concentrated")
        return finish("sources compared")

    def _generic(self, state) -> Decision:
        src = primary(state)
        if not done(state, "plan_analysis"):
            return call("plan_analysis", {"dataset_id": src["id"], "question": state.question}, "turn the question into a validated plan")
        if not done(state, "analyze_dataframe"):
            plan = state.context.get("plan")
            if not plan:
                return finish("no plan could be built")
            return call("analyze_dataframe", {"dataset_id": src["id"], "plan": plan}, "run the plan against the data")
        return finish("analysis complete")


# --------------------------------------------------------------------------
# LLM policy
# --------------------------------------------------------------------------

SYSTEM = (
    "You are the planning component of a business data analyst agent. You do not compute anything and you never write "
    "numbers: you choose which tool to run next, one at a time, based on the question and the findings so far. "
    "Tool results are computed from the user's data by code.\n"
    "Reply with JSON only. To run a tool: {\"action\":\"call\",\"tool\":\"<name>\",\"args\":{...},\"reason\":\"<short>\"}. "
    "To stop: {\"action\":\"finish\",\"reason\":\"<short>\"}. On your first reply you may add \"plan\": [\"short goal\", ...].\n"
    "Rules: use only the listed tools and the listed dataset ids; do not repeat a call; use the fewest steps that answer the "
    "question (a simple question needs 1-3); for 'why' questions measure the change, find the largest contributor, then drill into "
    "it (period_contribution with filters) while it explains most of the change; stop when the evidence is enough."
)


class LLMPolicy:
    name = "llm"

    def __init__(self, tools: list[dict]):
        self.tool_text = [{"name": t["name"], "description": t["description"],
                           "args": {k: v.get("type") for k, v in t["input_schema"].get("properties", {}).items()},
                           "required": t["input_schema"].get("required", [])} for t in tools]

    def _prompt(self, state, steps_left: int) -> str:
        steps = []
        for s in state.completed_steps[-8:]:
            digest = " | ".join(f["text"] for f in state.findings if f["step"] == s["n"])[:600]
            steps.append({"n": s["n"], "tool": s["tool"], "args": s["args"], "ok": s["ok"],
                          "result": digest or s.get("note", "")})
        payload = {
            "question": state.question, "intent": state.intent, "complexity": state.complexity,
            "sources": [{**src, **({k: meta_for(state, src["id"])[k] for k in ("measures", "dimensions", "date_columns")}
                                  if meta_for(state, src["id"]) else {})} for src in state.selected_sources],
            "tools": self.tool_text, "steps_so_far": steps, "steps_left": steps_left,
            "problem_with_your_last_reply": state.context.pop("policy_feedback", None),
        }
        return json.dumps(payload, default=str)[:12000]

    def decide(self, state, steps_left: int) -> Decision | None:
        from app.ai import llm_provider

        try:
            response = llm_provider.complete(SYSTEM, self._prompt(state, steps_left), json_mode=True, tier="strong", purpose="agent_step")
        except LLMError as exc:
            log.warning("LLM policy unavailable (%s); using rules for this step", exc.detail)
            return None
        state.llm_calls += 1
        usage = response.usage or {}
        used = sum(int(usage.get(k) or 0) for k in ("prompt_tokens", "completion_tokens", "input_tokens", "output_tokens",
                                                     "promptTokenCount", "candidatesTokenCount"))
        state.tokens_used += used or (len(response.text) + 3000) // 4  # no usage reported → rough estimate
        if response.from_demo:
            return None
        data = response.json()
        if not isinstance(data, dict) or data.get("action") not in {"call", "finish"}:
            state.context["policy_feedback"] = "Reply must be a JSON object with action 'call' or 'finish'."
            return None
        plan = data.get("plan") if isinstance(data.get("plan"), list) else None
        if data["action"] == "finish":
            return Decision("finish", reason=str(data.get("reason", ""))[:200], source="llm", plan=plan)
        args = data.get("args") if isinstance(data.get("args"), dict) else {}
        return Decision("call", str(data.get("tool", "")), args, str(data.get("reason", ""))[:200], "llm", plan)
