"""Agent loop: result-aware tool selection, safety of LLM-chosen calls, budgets and honesty.

Agent loop tests use a deterministic stub LLM (tests/stub_llm.py) when no external provider is
configured. No real OpenAI / Anthropic / Gemini call is made anywhere in this suite.
"""

from __future__ import annotations

import pytest

from config import Config
from tests import agent_data as data
from tests.stub_llm import ScriptedProvider, installed
from tests.test_auth import auth_on, post, signup


def chat(client, question, **extra):
    r = post(client, "/api/chat", {"question": question, **extra})
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def tools_of(body):
    return [s["tool"] for s in body["agent"]["steps"]]


def setup_orders(collapse=("South", "Furniture")):
    client, _ = signup()
    return client, data.upload(client, data.orders_csv(collapse), "orders.csv")


class Limits:
    """Temporarily change Config limits."""

    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(Config, k)
            setattr(Config, k, v)

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(Config, k, v)


# --------------------------------------------------------------------------
# rules policy: next step depends on the previous result
# --------------------------------------------------------------------------


def test_why_question_drills_into_the_segment_found_in_the_previous_step():
    with auth_on():
        client, _ = setup_orders(("South", "Furniture"))
        body = chat(client, "Why did our sales decrease last month?")
        agent = body["agent"]
        assert agent["mode"] == "rules"  # no LLM configured: honest about it
        assert "No language model was used" in agent["mode_note"]
        tools = tools_of(body)
        assert tools[:2] == ["get_dataset_metadata", "compare_periods"]
        drills = [s for s in agent["steps"] if s["tool"] == "period_contribution"]
        assert drills, tools
        facts = " ".join(agent["facts"])
        assert "South" in facts and "Furniture" in facts
        assert agent["evidence"] in {"strong", "moderate"}
        assert agent["interpretation"] and agent["recommendations"]
        assert any("Discount" in s["why"] or "discount" in s["why"].lower() for s in drills)  # related-measure check
        assert body["intent"] == "investigation" and body["investigation"]["measure"] == "Sales"
        assert body["extra_visualizations"]  # drill-down charts
        assert all(s["validated"] for s in agent["steps"] if s["ok"])
        assert body["history_id"]


def test_a_different_planted_cause_leads_to_a_different_drill_path():
    with auth_on():
        client, _ = setup_orders(("North", "Technology"))
        agent = chat(client, "Why did our sales decrease last month?")["agent"]
        facts = " ".join(agent["facts"])
        assert "North" in facts and "Technology" in facts and "Furniture" not in facts


def test_simple_question_uses_the_single_pass_pipeline_not_the_agent_loop():
    with auth_on():
        client, _ = setup_orders()
        body = chat(client, "What is total sales?")
        assert "agent" not in body  # no ten-step investigation for a one-number question
        assert body["result"]["row_count"] == 1


def test_no_drill_when_the_change_is_not_concentrated():
    with auth_on():
        client, _ = setup_orders(collapse=None)
        body = chat(client, "Why did our sales change last month?")
        agent = body["agent"]
        assert agent["evidence"] == "insufficient"  # a -0.5% wobble is not a finding
        assert len(agent["facts"]) == 1, agent["facts"]
        assert "ordinary fluctuation" in " ".join(agent["interpretation"]), agent["interpretation"]
        assert not agent["recommendations"]
        assert "period_contribution" not in tools_of(body)  # the agent did not chase noise
        assert "share of the total change" not in " ".join(body["investigation"]["facts"])


# --------------------------------------------------------------------------
# agent-only intents
# --------------------------------------------------------------------------


def test_correlation_distribution_quality_and_anomaly_each_pick_their_tool():
    with auth_on():
        client, _ = setup_orders()
        assert "correlate" in tools_of(chat(client, "Is there a correlation between Sales and Profit?"))
        dist = chat(client, "Show the distribution of Sales")
        assert "describe_distribution" in tools_of(dist) and dist["visualization"]["spec"]["chart_type"] == "histogram"
        assert "data_quality" in tools_of(chat(client, "Check data quality"))
        an = chat(client, "Find anomalies in sales")
        assert "detect_anomalies" in tools_of(an) and an["agent"]["findings"]


def test_forecasting_is_honestly_unsupported():
    with auth_on():
        client, _ = setup_orders()
        body = chat(client, "Forecast next month's sales")
        assert "does not generate forecasts" in body["answer"]


def test_cross_source_sales_vs_inventory_uses_both_sources():
    with auth_on():
        client, _ = signup()
        sales = data.upload(client, data.sales_csv(), "sales.csv")
        inventory = data.upload(client, data.inventory_csv(), "inventory.csv")
        body = chat(client, "Why are sales falling while inventory is increasing?")
        assert body["business_intent"] == "cross_source"
        trend = next(s for s in body["agent"]["steps"] if s["tool"] == "compare_trends")
        assert trend["ok"] and trend["validated"]
        assert {d["id"] for d in body["source"]["datasets"]} == {sales, inventory}
        assert body["visualization"]["spec"]["chart_type"] == "dual_line"
        text = " ".join(body["agent"]["facts"] + body["agent"]["interpretation"])
        assert "not evidence that one causes" in text or "does not show" in text or "not proof" in text


# --------------------------------------------------------------------------
# LLM policy (deterministic stub)
# --------------------------------------------------------------------------


def _script(ds):
    return [
        {"action": "call", "tool": "get_dataset_metadata", "args": {"dataset_id": ds}, "reason": "look at the data",
         "plan": ["inspect", "compare periods", "drill into the leader"]},
        {"action": "call", "tool": "compare_periods", "args": {"dataset_id": ds, "question": "why did sales fall"}, "reason": "measure change"},
        {"action": "call", "tool": "period_contribution",
         "args": {"dataset_id": ds, "measure": "Sales", "dimension": "Region", "filters": {"Category": "Furniture"}}, "reason": "drill"},
        {"action": "finish", "reason": "enough"},
    ]


def test_stub_llm_chooses_the_steps_and_every_choice_goes_through_the_registry():
    with auth_on():
        client, ds = setup_orders()
        stub = ScriptedProvider(_script(ds), usage={"prompt_tokens": 120, "completion_tokens": 30}, text_fallback="")
        with installed(stub):
            body = chat(client, "Why did our sales decrease last month?")
        agent = body["agent"]
        assert agent["mode"].startswith("llm")
        assert tools_of(body)[:3] == ["get_dataset_metadata", "compare_periods", "period_contribution"]
        assert {s["decided_by"] for s in agent["steps"][:3]} == {"llm"}
        assert agent["plan"][0]["goal"] == "inspect"  # the model's own plan is kept
        assert agent["usage"]["calls"] >= 3 and agent["usage"]["input_tokens"] >= 360
        assert agent["budget"]["llm_calls"] >= 3
        sent = " ".join(u for _s, u in stub.calls)
        assert "password" not in sent.lower() and "api_key" not in sent.lower()


def test_hallucinated_tool_is_rejected_and_the_rules_policy_still_answers():
    with auth_on():
        client, ds = setup_orders()
        stub = ScriptedProvider([{"action": "call", "tool": "drop_all_tables", "args": {}}] * 3)
        with installed(stub):
            body = chat(client, "Why did our sales decrease last month?")
        stages = [s for s in body["stages"] if s["stage"] == "reject_tool_call"]
        assert stages and "does not exist" in stages[0]["detail"]
        assert "compare_periods" in tools_of(body)  # rules finished the job
        assert body["agent"]["mode"] in {"llm+rules", "rules"}


@pytest.mark.parametrize("args", [
    {"dataset_id": "DS", "measure": "Sales", "evil": 1},          # unknown field
    {"dataset_id": "DS"},                                          # missing required field
    {"dataset_id": "DS", "measure": 5},                            # wrong type
    {"dataset_id": "DS", "measure": "Sales", "grain": "decade"},   # not in enum
])
def test_invalid_llm_arguments_are_rejected_before_execution(args):
    with auth_on():
        client, ds = setup_orders()
        args = {k: (ds if v == "DS" else v) for k, v in args.items()}
        stub = ScriptedProvider([{"action": "call", "tool": "period_contribution", "args": args}] * 3)
        with installed(stub):
            body = chat(client, "Why did our sales decrease last month?")
        rejected = [s for s in body["stages"] if s["stage"] == "reject_tool_call"]
        assert rejected
        assert not any(s["tool"] == "period_contribution" and s["decided_by"] == "llm" for s in body["agent"]["steps"])


def test_llm_cannot_reach_a_dataset_outside_this_analysis(tmp_path):
    with auth_on():
        alice, ds_a = setup_orders()
        bob, ds_b = signup()[0], None
        ds_b = data.upload(bob, data.orders_csv(), "bob.csv")
        # Alice's model proposes Bob's dataset id
        stub = ScriptedProvider([{"action": "call", "tool": "get_dataset_metadata", "args": {"dataset_id": ds_b}}] * 3)
        with installed(stub):
            body = chat(alice, "Why did our sales decrease last month?")
        assert any("not part of this analysis" in s["detail"] for s in body["stages"] if s["stage"] == "reject_tool_call")
        assert ds_b not in str(body["agent"]["steps"])


def test_repeated_identical_call_reuses_the_result_and_is_not_executed_twice():
    with auth_on():
        client, ds = setup_orders()
        same = {"action": "call", "tool": "get_dataset_metadata", "args": {"dataset_id": ds}}
        stub = ScriptedProvider([same, same, same, {"action": "finish"}])
        with installed(stub):
            body = chat(client, "Why did our sales decrease last month?")
        steps = body["agent"]["steps"]
        reused = [s for s in steps if s["reused"]]
        assert reused and reused[0]["tool"] == "get_dataset_metadata"
        executed = [e for e in client.get("/api/agent/events").get_json()["events"] if e["tool"] == "get_dataset_metadata"]
        assert len(executed) == 1  # one real execution, one audit event


def test_model_call_budget_falls_back_to_rules_with_an_honest_note():
    with auth_on(), Limits(AGENT_MAX_LLM_CALLS=1):
        client, ds = setup_orders()
        stub = ScriptedProvider(_script(ds))
        with installed(stub):
            body = chat(client, "Why did our sales decrease last month?")
        assert len(stub.calls) <= 2  # at most the one decision (+ optional wording call)
        assert any("limit of 1 model calls" in e for e in body["agent"]["errors"])
        assert "compare_periods" in tools_of(body)


def test_llm_summary_is_used_only_when_every_number_is_grounded():
    with auth_on():
        client, ds = setup_orders()
        bad = ScriptedProvider([{"action": "finish"}], text_fallback="Sales fell by 99.9% because of weather.")
        with installed(bad):
            body = chat(client, "Why did our sales decrease last month?")
        assert "99.9" not in body["answer"] and "weather" not in body["answer"]
        assert "deterministic" in body["methods"]["insight"] or body["methods"]["insight"] == "deterministic"


# --------------------------------------------------------------------------
# limits and failures
# --------------------------------------------------------------------------


def test_maximum_steps_stops_the_run_with_a_partial_honest_result():
    with auth_on(), Limits(AGENT_MAX_STEPS=3):
        client, _ = setup_orders()
        body = chat(client, "Why did our sales decrease last month?")
        agent = body["agent"]
        assert agent["status"] == "partial" and len(agent["steps"]) <= 3
        assert any("maximum of 3 steps" in e for e in agent["errors"])
        assert "execution limit" in " ".join(agent["limitations"])


def test_time_limit_stops_the_run():
    with auth_on(), Limits(AGENT_TIMEOUT_SECONDS=-1):
        client, _ = setup_orders()
        body = chat(client, "Find anomalies in sales")
        assert "time limit" in " ".join(body["agent"]["errors"]) or "time limit" in body["answer"]


def test_tool_failure_is_recorded_not_fatal_and_transient_errors_are_retried():
    from app.agent import tools as registry
    from app.utils.exceptions import ConnectorError

    with auth_on():
        client, ds = setup_orders()
        original = registry.TOOLS["data_quality"].func
        calls = {"n": 0}

        def flaky(dataset_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectorError("temporary outage")
            return original(dataset_id)

        registry.TOOLS["data_quality"].func = flaky
        try:
            body = chat(client, "Check data quality")
        finally:
            registry.TOOLS["data_quality"].func = original
        assert calls["n"] == 2  # retried once, then succeeded
        event = next(e for e in client.get("/api/agent/events").get_json()["events"] if e["tool"] == "data_quality")
        assert event["ok"] == 1 and event["retries"] == 1
        assert body["agent"]["status"] == "complete"


def test_two_failures_in_a_row_stop_the_agent_without_inventing_a_result():
    from app.agent import tools as registry
    from app.utils.exceptions import UnsupportedRequestError

    with auth_on():
        client, ds = setup_orders()
        original = registry.TOOLS["data_quality"].func

        def broken(dataset_id):
            raise UnsupportedRequestError("not possible here")

        registry.TOOLS["data_quality"].func = broken
        try:
            body = chat(client, "Check data quality")
        finally:
            registry.TOOLS["data_quality"].func = original
        assert body["agent"]["status"] in {"failed", "partial"}
        assert not body["agent"]["findings"]
        assert "not possible here" in body["answer"] or "could not" in body["answer"].lower() or body["agent"]["errors"]


def test_no_secrets_in_agent_output_or_state():
    from app.agent.state import safe_args

    cleaned = safe_args({"password": "hunter2", "api_key": "sk-x", "sql": "SELECT 1", "dataset_id": "ds_1", "note": "x" * 200})
    assert cleaned["password"] == "(omitted)" and cleaned["api_key"] == "(omitted)" and cleaned["sql"] == "(omitted)"
    assert len(cleaned["note"]) <= 80


def test_zz_cleanup_search_index():
    from tests.test_connectors import test_zz_cleanup_search_index as cleanup

    cleanup()
