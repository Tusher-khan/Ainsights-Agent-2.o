"""The controlled agent loop.

    while not done:
        decide next action   (LLM policy → rules policy as fallback)
        validate the call    (tool exists, role allows it, schema, data source belongs to this analysis)
        execute the tool     (owner-scoped, timeout, bounded retry, audit event)
        validate the result  (shape, checks, arithmetic recomputation)
        extract findings     (numbers only from the tool result)
        update the state     (policy sees the findings on the next turn)

Hard limits: maximum steps, wall-clock timeout, token budget, per-tool retry limit,
repeated-call (loop) detection and consecutive-failure cut-off. Hitting any limit ends the run
with status "partial" and says so; it never loops indefinitely.
"""

from __future__ import annotations

import time
from typing import Any

from config import Config
from app.agent import findings as F
from app.agent import tools as registry
from app.agent import viz
from app.agent.policy import Decision, LLMPolicy, RulesPolicy
from app.agent.state import AgentState, safe_args
from app.security.request_scope import current_user
from app.services import usage_service
from app.utils.exceptions import AInsightError
from app.utils.logger import get_logger

log = get_logger("agent.loop")

# What the loop may offer a policy. Raw SQL and live API tools stay out unless the sources need them;
# validation and visualization are loop stages, not choices.
ANALYSIS_TOOLS = {"get_dataset_metadata", "plan_analysis", "analyze_dataframe", "calculate_metric", "compare_periods",
                  "period_contribution", "detect_anomalies", "correlate", "describe_distribution", "data_quality",
                  "compare_trends"}
DATASET_ARGS = ("dataset_id", "dataset_a", "dataset_b")
MAX_CONSECUTIVE_FAILURES = 2


# --------------------------------------------------------------------------
# step validation
# --------------------------------------------------------------------------


def _close(a: float, b: float, tol: float = 0.011) -> bool:
    return abs(a - b) <= tol


def validate_step(tool: str, result: Any) -> list[str]:
    """Problems with a tool result. Empty list = accepted."""
    issues: list[str] = []
    spec = registry.TOOLS[tool]
    if not isinstance(result, dict):
        return ["result was not a structured object"]
    for key in spec.output_keys:
        if key not in result:
            issues.append(f"missing '{key}' in result")
    for c in result.get("checks") or []:
        if c.get("passed") is False:
            issues.append(f"check failed: {c.get('name')} ({c.get('detail')})")
    if tool == "period_contribution":
        t = result.get("total") or {}
        if t:
            if not _close(t["current"] - t["previous"], t["delta"]):
                issues.append("recomputed change does not match the reported change")
            if t["previous"]:
                pct = (t["current"] - t["previous"]) / abs(t["previous"]) * 100
                if t.get("delta_pct") is None or abs(pct - t["delta_pct"]) > 0.06:
                    issues.append("recomputed growth % does not match the reported growth %")
            if not t.get("current_rows") or not t.get("previous_rows"):
                issues.append("one of the two periods has no rows")
        for r in result.get("rows") or []:
            if not _close(r["current"] - r["previous"], r["delta"]):
                issues.append(f"segment {r['segment']}: change does not equal current − previous")
                break
    if tool == "compare_periods":
        for k in result.get("kpis") or []:
            if not _close(k["current"] - k["previous"], k["change"], 0.02):
                issues.append(f"KPI {k['label']}: change does not equal current − previous")
    if tool == "analyze_dataframe":
        val = result.get("validation") or {}
        if val.get("valid") is False:
            issues.append("; ".join(val.get("errors", [])) or "result validation failed")
        if (result.get("result") or {}).get("row_count") == 0:
            issues.append("the analysis returned no rows")
    if tool == "compare_trends" and len(result.get("periods", [])) < 2:
        issues.append("fewer than two aligned periods")
    return issues


# --------------------------------------------------------------------------
# visualization stage
# --------------------------------------------------------------------------


def build_visuals(state: AgentState) -> list[dict]:
    figures: list[dict] = []
    for n in sorted(state.tool_results):
        entry = state.tool_results[n]
        tool, r = entry["tool"], entry["result"]
        if not entry["validated"] or not isinstance(r, dict):
            continue
        fig = None
        if tool == "period_contribution" and r.get("dimension") and r.get("rows"):
            fig = viz.contribution(r["rows"], r["measure"], r["dimension"], r["current_label"], r["previous_label"], r.get("filters"))
        elif tool == "detect_anomalies":
            fig = viz.trend([p["period"] for p in r["series"]], [p["value"] for p in r["series"]], r["measure"], anomalies=r["anomalies"])
        elif tool == "compare_trends":
            fig = viz.comparison(r["periods"], r["a"], r["b"])
        elif tool == "correlate":
            fig = viz.relationship(r["points"], r["x"], r["y"], r["pearson"], r["strength"])
        elif tool == "describe_distribution":
            fig = viz.distribution(r["histogram"], r["measure"], r["median"])
        elif tool == "data_quality":
            fig = viz.issues_bar(r["issues"])
        elif tool == "analyze_dataframe" and r.get("visualization"):
            fig = r["visualization"]
        if fig:
            figures.append(fig)
    return viz.dedupe(figures)[:5]


# --------------------------------------------------------------------------
# optional LLM wording of the summary (never of the numbers)
# --------------------------------------------------------------------------


def _polish(state: AgentState) -> None:
    from app.ai import llm_provider

    if not Config.AGENT_USE_LLM or state.tokens_used >= Config.AGENT_TOKEN_BUDGET or not state.final_insights.get("facts"):
        return
    facts = state.final_insights["facts"]
    system = ("You are a business analyst writing a 2-3 sentence executive summary. Use ONLY the facts given. "
              "Do not add any number, percentage, date or name that is not in the facts. Do not claim causes; say 'suggests' for interpretation. "
              "Plain prose, no lists.")
    try:
        response = llm_provider.complete(system, "Facts:\n- " + "\n- ".join(facts[:8]) + "\n\nQuestion: " + state.question[:300],
                                         tier="fast", purpose="agent_summary")
    except AInsightError:
        return
    state.llm_calls += 1
    text = (response.text or "").strip()
    if response.from_demo or not text or text.startswith("{"):
        return
    if F.grounded(text, facts, state.question):
        state.final_insights["summary"] = text
        state.final_insights["summary_method"] = "llm (checked: every number appears in the computed facts)"
    else:
        state.final_insights["summary_method"] = "deterministic (the model's wording used a number that was not in the facts)"


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------


def _llm_budget_reason(state: AgentState) -> str | None:
    if state.tokens_used >= Config.AGENT_TOKEN_BUDGET:
        return "The model token budget for this analysis was reached."
    if Config.AGENT_MAX_LLM_CALLS and state.llm_calls >= Config.AGENT_MAX_LLM_CALLS:
        return f"The limit of {Config.AGENT_MAX_LLM_CALLS} model calls for one analysis was reached."
    if Config.AGENT_MAX_COST_USD:
        cost = usage_service.analysis_usage(state.analysis_id).get("est_cost_usd")
        if cost is not None and cost >= Config.AGENT_MAX_COST_USD:
            return "The estimated cost limit for this analysis was reached."
    return None


def _allowed_tools(sources: list) -> set[str]:
    allowed = set(ANALYSIS_TOOLS)
    if any(getattr(s, "kind", "") == "sql" and hasattr(s, "connection_id") for s in sources):
        allowed.add("query_database")
    return allowed


def run(question: str, intent: str, sources: list, *, complexity: str = "complex", policy: str | None = None,
        cross_measures: dict | None = None, role: str | None = None) -> AgentState:
    """Run one analysis. `sources` are owner-scoped DataSource objects chosen by the caller (the data-discovery stage)."""
    user = current_user()
    state = AgentState(question=question, user_id=(user or {}).get("id"), intent=intent, complexity=complexity)
    state.selected_sources = [{"id": s.id, "name": s.name, "kind": s.kind} for s in sources]
    allowed = _allowed_tools(sources)
    state.available_tools = sorted(allowed)
    if cross_measures:
        state.context["cross_measures"] = cross_measures
    token = usage_service.set_analysis(state.analysis_id)
    try:
        _loop(state, allowed, policy, role)
    finally:
        usage_service.reset_analysis(token)
    return state


def _loop(state: AgentState, allowed: set, policy_mode: str | None, role: str | None) -> None:
    rules = RulesPolicy()
    use_llm = Config.AGENT_USE_LLM and policy_mode != "rules"
    llm = None
    if use_llm:
        from app.ai import llm_provider

        provider = llm_provider.get_provider()
        if provider.name != "demo" and provider.available():
            llm = LLMPolicy(registry.tool_specs(allowed))
    used_llm = used_rules = False
    state.analysis_plan = [{"goal": g, "status": "planned"} for g in rules.outline(state)]
    state.stage("understand_question", detail=f"{state.intent} ({state.complexity}); {len(state.selected_sources)} source(s)")
    cache: set[str] = set()  # signatures of calls whose result was accepted
    failures = 0
    repeats = 0
    max_steps = max(1, Config.AGENT_MAX_STEPS)

    while True:
        step_no = len(state.completed_steps) + 1
        # ---- hard limits ----------------------------------------------------
        if len(state.completed_steps) >= max_steps:
            state.errors.append(f"Stopped after the maximum of {max_steps} steps.")
            state.status = "partial"
            break
        if state.elapsed_s > Config.AGENT_TIMEOUT_SECONDS:
            state.errors.append(f"Stopped after the {Config.AGENT_TIMEOUT_SECONDS}s time limit.")
            state.status = "partial"
            break
        if llm is not None:
            reason = _llm_budget_reason(state)
            if reason:
                state.errors.append(f"{reason} The remaining steps used the built-in rules.")
                state.stage("budget", "failed", reason)
                llm = None

        # ---- decide ---------------------------------------------------------
        decision: Decision | None = None
        if llm is not None:
            step_token = usage_service.set_step(step_no)
            try:
                decision = llm.decide(state, max_steps - len(state.completed_steps))
            finally:
                usage_service._step.reset(step_token)
            if decision is not None and decision.plan and not any(p.get("from_llm") for p in state.analysis_plan):
                state.analysis_plan = [{"goal": str(g)[:120], "status": "planned", "from_llm": True} for g in decision.plan[:10]]
        if decision is None:
            decision = rules.decide(state)
            used_rules = True
        else:
            used_llm = True
        if decision.action == "finish" and decision.source == "llm" and not state.findings:
            # A model that stops before any evidence exists has not answered the question: let the rules continue.
            fallback = rules.decide(state)
            if fallback.action == "call":
                state.stage("finish_overridden", "done", "the model stopped before any evidence was gathered; rules continued")
                decision = fallback
                used_rules = True
        state.stage("decide_next_step", detail=f"[{decision.source}] {decision.action} {decision.tool or ''} — {decision.reason}"[:240])
        if decision.action == "finish":
            state.status = "complete" if state.status == "running" else state.status
            break

        # ---- validate the call ----------------------------------------------
        args = decision.args or {}
        problem = None
        for key in DATASET_ARGS:
            if key in args and not state.source_by_id(str(args[key])):
                problem = "that dataset is not part of this analysis"
        signature = f"{decision.tool}:{sorted((k, str(v)) for k, v in args.items())}"
        if problem is None and signature in cache:
            # Same tool + arguments already ran and its result was accepted: reuse it, do not run it again.
            problem = "that exact call was already made; its result is already in the findings"
            state.completed_steps.append({"n": step_no, "tool": decision.tool, "args": safe_args(args), "reason": decision.reason,
                                          "source": decision.source, "ok": True, "validated": True, "issues": [],
                                          "reused": True, "ms": 0, "note": "result reused (no second execution)"})
            state.stage("reuse_result", "done", f"{decision.tool}: identical call, previous result reused")
            repeats += 1
            if repeats >= 2:
                state.stage("loop_detected", "failed", decision.tool or "")
                if decision.source == "llm" and llm is not None:
                    state.errors.append("The model kept repeating the same step, so the built-in rules took over.")
                    llm, repeats = None, 0
                    continue
                state.errors.append("The agent kept repeating the same step, so it was stopped.")
                state.status = "partial"
                break
        if problem is None:
            try:
                registry.validate_call(decision.tool or "", args, allowed=allowed, role=role)
            except AInsightError as exc:
                problem = exc.user_message
        if problem:
            state.context["policy_feedback"] = f"{decision.tool}: {problem}"
            state.stage("reject_tool_call", "failed", f"{decision.tool}: {problem}")
            failures += 1
            if decision.source == "llm":  # a bad model answer: retry this step with the rules policy next time
                llm = None if failures >= 2 else llm
                if llm is None:
                    state.errors.append("The model's tool choices were not usable; the built-in rules finished the analysis.")
                continue
            if failures >= MAX_CONSECUTIVE_FAILURES:
                state.status = "partial"
                break
            continue

        # ---- execute --------------------------------------------------------
        started = time.perf_counter()
        step: dict = {"n": step_no, "tool": decision.tool, "args": safe_args(args), "reason": decision.reason,
                      "source": decision.source, "ok": False, "validated": False, "issues": []}
        try:
            result = registry.call(decision.tool, args, state.analysis_id, allowed=allowed, role=role,
                                   model=None)
            step["ok"] = True
            failures = 0
        except AInsightError as exc:
            step["note"] = exc.user_message
            state.errors.append(f"{decision.tool}: {exc.user_message}")
            failures += 1
            step["ms"] = round((time.perf_counter() - started) * 1000, 1)
            state.completed_steps.append(step)
            state.stage(f"tool:{decision.tool}", "failed", exc.user_message, step["ms"])
            state.context["policy_feedback"] = f"{decision.tool} failed: {exc.user_message}"
            if failures >= MAX_CONSECUTIVE_FAILURES:
                state.status = "partial"
                state.errors.append("Two steps failed in a row, so the analysis was stopped.")
                break
            continue
        step["ms"] = round((time.perf_counter() - started) * 1000, 1)

        # ---- validate the result, record findings -----------------------------
        issues = validate_step(decision.tool, result)
        step["issues"] = issues
        step["validated"] = not issues
        if not issues:
            cache.add(signature)
        state.completed_steps.append(step)
        state.tool_results[step_no] = {"tool": decision.tool, "result": result, "validated": step["validated"]}
        state.stage(f"tool:{decision.tool}", "done", decision.reason, step["ms"])
        state.stage("validate_result", "done" if step["validated"] else "failed",
                    "all checks passed" if step["validated"] else "; ".join(issues)[:200])
        if decision.tool == "get_dataset_metadata":
            state.context.setdefault("meta", {})[args["dataset_id"]] = result
        elif decision.tool == "plan_analysis":
            state.context["plan"] = result.get("plan")
        else:
            made = F.extract(decision.tool, args, result, step_no, state)
            if made and not step["validated"]:
                for f in made:
                    f["validated"] = False

    # ---- finish: insights, visuals, optional wording -------------------------------
    for g in state.analysis_plan:
        g["status"] = "done" if state.status in {"complete", "running"} else "partial"
    state.policy = "llm+rules" if used_llm and used_rules else "llm" if used_llm else "rules"
    insights = F.build_insights(state)
    state.stage("generate_insight", detail=f"evidence: {state.evidence}")
    state.visualization_data = build_visuals(state)
    state.stage("generate_visualization", detail=f"{len(state.visualization_data)} chart(s) chosen from the result shapes")
    if used_llm:
        _polish(state)
    if state.status == "running":
        state.status = "complete"
    if not state.findings and state.status == "complete":
        state.status = "partial"
    if state.status == "partial" and not state.findings:
        state.status = "failed"
    state.final_insights = insights
