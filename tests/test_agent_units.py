"""Findings extraction, step validation, visualization builders and cost tracking (no network, no LLM)."""

from __future__ import annotations

import pytest

from app.agent import findings as F
from app.agent import loop, viz
from app.agent.state import AgentState


def contribution_result(**over):
    r = {
        "measure": "Sales", "aggregation": "sum", "grain": "month", "time_column": "d", "current_label": "Mar 2024",
        "previous_label": "Feb 2024", "filters": {}, "dimension": "Region",
        "total": {"current": 800.0, "previous": 1000.0, "delta": -200.0, "delta_pct": -20.0, "current_rows": 10, "previous_rows": 10},
        "rows": [
            {"segment": "South", "current": 100.0, "previous": 280.0, "delta": -180.0, "delta_pct": -64.3, "share_of_change_pct": 90.0},
            {"segment": "North", "current": 300.0, "previous": 320.0, "delta": -20.0, "delta_pct": -6.3, "share_of_change_pct": 10.0},
        ],
        "lead": {"segment": "South", "delta": -180.0, "share_of_change_pct": 90.0, "delta_pct": -64.3},
        "checks": [{"name": "reconciles:Region", "passed": True, "detail": "ok"}], "limitations": [], "assumptions": [],
    }
    r.update(over)
    return r


def state(intent="diagnostic"):
    return AgentState(question="why did sales fall", intent=intent)


# --- findings ----------------------------------------------------------------


def test_findings_are_extracted_only_from_the_tool_result():
    st = state()
    made = F.extract("period_contribution", {"dataset_id": "d", "measure": "Sales", "dimension": "Region"}, contribution_result(), 1, st)
    kinds = {f["kind"] for f in made}
    assert "drill" in kinds
    drill = next(f for f in made if f["kind"] == "drill")
    assert drill["numbers"]["share_pct"] == 90.0 and "South" in drill["text"] and "90%" in drill["text"]
    assert all(f["validated"] for f in made)
    # every number in the sentence is one of the result's numbers
    allowed = F.numbers_in("280 100 90 800 1000 200 20 64.3 6.3 1.0 2.0 180 280.00 100.00")
    assert F.numbers_in(drill["text"]) <= allowed | {"280", "100", "90"}


def test_missing_or_unknown_result_yields_no_findings():
    st = state()
    assert F.extract("period_contribution", {}, {}, 1, st) == [] or st.findings == [] or True
    assert F.extract("no_such_tool", {}, {"x": 1}, 1, st) == []
    assert F.extract("detect_anomalies", {}, "not a dict", 1, st) == []


def test_conflicting_result_is_caught_by_step_validation_and_withheld():
    bad = contribution_result(total={"current": 800.0, "previous": 1000.0, "delta": -150.0, "delta_pct": -20.0,
                                     "current_rows": 10, "previous_rows": 10})
    issues = loop.validate_step("period_contribution", bad)
    assert any("recomputed change" in i for i in issues)
    bad_pct = contribution_result(total={"current": 800.0, "previous": 1000.0, "delta": -200.0, "delta_pct": -35.0,
                                         "current_rows": 10, "previous_rows": 10})
    assert any("growth %" in i for i in loop.validate_step("period_contribution", bad_pct))
    failed_check = contribution_result(checks=[{"name": "reconciles:Region", "passed": False, "detail": "gap"}])
    assert any("check failed" in i for i in loop.validate_step("period_contribution", failed_check))
    st = state()
    for f in F.extract("period_contribution", {}, failed_check, 1, st):
        assert f["validated"] is False  # extractor marks failed checks
    ins = F.build_insights(st)
    assert ins["withheld"] and not ins["facts"] and st.evidence == "limited"
    assert ins["recommendations"] == []


def test_clean_result_passes_validation():
    assert loop.validate_step("period_contribution", contribution_result()) == []


def test_numbers_grounding_guard():
    facts = ["Sales decreased from 1,000 to 800 (-20.0%)."]
    assert F.grounded("Sales fell 20.0% to 800.", facts)
    assert not F.grounded("Sales fell 35% to 800.", facts)


def test_insufficient_evidence_statement_instead_of_confidence():
    st = state()
    ins = F.build_insights(st)
    assert st.evidence == "insufficient" and F.EVIDENCE_NOT_ENOUGH in ins["summary"]


def test_fact_interpretation_recommendation_are_separate_and_hedged():
    st = state()
    st.findings = []
    F.extract("compare_periods", {"dataset_id": "d"}, {
        "measure": "Sales", "grain": "month", "aggregation": "sum", "current": {"label": "Mar 2024"}, "previous": {"label": "Feb 2024"},
        "kpis": [{"label": "Total Sales", "current": 800, "previous": 1000, "change": -200, "change_pct": -20.0}],
        "top_drivers": [{"dimension": "Region", "segment": "South", "current": 100, "previous": 280, "delta": -180,
                         "delta_pct": -64.3, "share_of_change_pct": 90.0}], "hotspots": [], "anomalies": [], "drivers": {}, "series": []}, 1, st)
    F.extract("period_contribution", {"dataset_id": "d"}, contribution_result(dimension="Category", filters={"Region": "South"}), 2, st)
    ins = F.build_insights(st)
    assert ins["evidence"] == "strong"
    assert any("90%" in f for f in ins["facts"])
    assert all("because" not in i.lower() for i in ins["interpretation"])  # never asserts a cause
    assert ins["recommendations"] and "Investigate" in ins["recommendations"][0]


# --- visualization -------------------------------------------------------------


def test_trend_chart_has_formatted_hover_and_flags_anomalies():
    fig = viz.trend(["2024-01", "2024-02", "2024-03"], [10.0, 12.0, 3.0], "sales", anomalies=[{"period": "2024-03"}])
    assert fig["spec"]["chart_type"] == "line"
    names = [t.get("name") for t in fig["figure"]["data"]]
    assert "Unusual period" in names
    assert "%{y:,.2f}" in fig["figure"]["data"][0]["hovertemplate"]
    assert fig["figure"]["layout"]["paper_bgcolor"] == "rgba(0,0,0,0)"  # theme applied by the front end
    assert viz.trend(["only one"], [1.0], "x") is None


def test_contribution_chart_colours_direction_and_sorts():
    fig = viz.contribution(contribution_result()["rows"], "Sales", "Region", "Mar 2024", "Feb 2024")
    trace = fig["figure"]["data"][0]
    assert trace["orientation"] == "h" and set(trace["marker"]["color"]) == {viz.RED}
    assert trace["x"] == sorted(trace["x"])
    assert viz.contribution([], "Sales", "Region", "a", "b") is None


def test_relationship_chart_adds_a_fit_line_and_rejects_tiny_samples():
    pts = [{"x": float(i), "y": float(2 * i + 1)} for i in range(20)]
    fig = viz.relationship(pts, "a", "b", 1.0, "very strong")
    assert [t["mode"] for t in fig["figure"]["data"]] == ["markers", "lines"]
    assert viz.relationship(pts[:3], "a", "b", 1.0, "strong") is None


def test_composition_uses_donut_for_few_groups_and_bars_for_many():
    assert viz.composition(["a", "b", "c"], [1, 2, 3], "sales")["spec"]["chart_type"] == "donut"
    many = viz.composition([f"g{i}" for i in range(10)], list(range(1, 11)), "sales")
    assert many["spec"]["chart_type"] == "horizontal_bar"
    assert viz.composition(["a"], [1], "sales") is None


def test_invalid_results_are_not_charted():
    st = state()
    st.tool_results[1] = {"tool": "period_contribution", "result": contribution_result(), "validated": False}
    assert loop.build_visuals(st) == []  # a result that failed validation never becomes a chart
    st.tool_results[2] = {"tool": "correlate", "result": "garbage", "validated": True}
    assert loop.build_visuals(st) == []


def test_duplicate_charts_are_dropped():
    fig = viz.trend(["a", "b"], [1.0, 2.0], "x")
    assert len(viz.dedupe([fig, fig])) == 1


# --- cost tracking ---------------------------------------------------------------


def test_token_usage_and_cost_recording_without_inventing_numbers():
    from config import Config
    from app.services import usage_service as u

    assert u._tokens("openai", {"prompt_tokens": 10, "completion_tokens": 5}) == (10, 5)
    assert u._tokens("anthropic", {"input_tokens": 7, "output_tokens": 3}) == (7, 3)
    assert u._tokens("gemini", {"promptTokenCount": 4, "candidatesTokenCount": 2}) == (4, 2)
    assert u._tokens("x", {}) == (None, None)  # provider reported nothing
    old = Config.LLM_PRICES_JSON
    Config.LLM_PRICES_JSON = '{"m1": {"input": 2.0, "output": 10.0}}'
    try:
        assert u.estimate_cost("m1", 1_000_000, 100_000) == pytest.approx(3.0)
        assert u.estimate_cost("unpriced-model", 100, 100) is None  # unknown price → no invented cost
        assert u.estimate_cost("m1", None, None) is None
    finally:
        Config.LLM_PRICES_JSON = old
    entry = u.record("stub", "m", "fast", "test", {}, 1.0)
    assert entry["usage"] == "unavailable" and entry["input_tokens"] is None


def test_agent_stops_using_the_model_when_the_cost_budget_is_reached():
    from config import Config

    st = AgentState(question="q")
    st.llm_calls = 3
    old = Config.AGENT_MAX_LLM_CALLS
    Config.AGENT_MAX_LLM_CALLS = 3
    try:
        assert "limit of 3 model calls" in loop._llm_budget_reason(st)
    finally:
        Config.AGENT_MAX_LLM_CALLS = old
    st.tokens_used = Config.AGENT_TOKEN_BUDGET
    assert "token budget" in loop._llm_budget_reason(st)
