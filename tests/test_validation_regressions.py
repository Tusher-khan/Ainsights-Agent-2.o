"""Found during browser validation: unknown values/columns must be refused, not silently replaced."""

from __future__ import annotations

from tests import agent_data as data
from tests.test_agent_loop import chat, setup_orders
from tests.test_auth import auth_on, post


def test_unknown_category_value_is_refused_with_the_real_values():
    with auth_on():
        client, ds = setup_orders()
        r = post(client, "/api/chat", {"question": "What was revenue from the Mars region?", "dataset_id": ds})
        assert r.status_code == 422
        msg = r.get_json()["message"]
        assert "Mars" in msg and "North" in msg and "South" in msg
        ok = post(client, "/api/chat", {"question": "What was sales in the South region?", "dataset_id": ds})
        assert ok.status_code == 200


def test_unknown_grouping_column_is_refused_not_guessed():
    with auth_on():
        client, ds = setup_orders()
        r = post(client, "/api/chat", {"question": "Show average Profit by Foo", "dataset_id": ds})
        assert r.status_code == 422 and "Foo" in r.get_json()["message"] and "Region" in r.get_json()["message"]
        for good in ("Show total Sales by Region", "Show Sales by month", "Show total Sales by Category and Region"):
            assert post(client, "/api/chat", {"question": good, "dataset_id": ds}).status_code == 200, good


def test_margin_without_a_definition_says_the_measure_was_substituted():
    with auth_on():
        client, ds = setup_orders()
        body = chat(client, "Show Profit Margin by Region", dataset_id=ds)
        assert any("not a column or a defined business metric" in a for a in body.get("assumptions", []))


def test_no_change_dataset_is_labelled_insufficient_everywhere():
    with auth_on():
        client, _ = signup_steady()
        body = client[0]
        assert body["agent"]["evidence"] == "insufficient"
        assert body["investigation"]["confidence"] == "low" and body["investigation"]["evidence"] == "insufficient"
        assert "% of the total change" not in body["answer"]


def signup_steady():
    from tests.test_auth import signup
    client, _ = signup()
    ds = data.upload(client, data.orders_csv(None), "steady.csv")
    return [chat(client, "Why did our sales decrease last month?", dataset_id=ds)], ds


def test_app_finds_templates_regardless_of_working_directory(tmp_path):
    import importlib.util
    import os
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("ainsight_entry_cwd", root / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        client = module.create_app().test_client()
        assert client.get("/login").status_code == 200
    finally:
        os.chdir(old)


def test_followup_keeps_the_previous_filter_and_replaces_it_when_a_new_value_is_named():
    with auth_on():
        client, ds = setup_orders()
        conv = chat(client, "Show the top 5 products in South", dataset_id=ds)["conversation_id"]
        east = chat(client, "What about East?", dataset_id=ds, conversation_id=conv)
        top3 = chat(client, "only the top 3", dataset_id=ds, conversation_id=conv)
        rows = top3["result"]["rows"]
        import io
        import pandas as pd
        frame = pd.read_csv(io.BytesIO(data.orders_csv(("South", "Furniture"))))
        truth = frame[frame.Region == "East"].groupby("Product").Sales.sum().sort_values(ascending=False).head(3)
        assert [r["Product"] for r in rows] == list(truth.index)
        assert abs(rows[0]["total_Sales"] - truth.iloc[0]) < 0.01
        assert any("Region = East" in a for a in top3["assumptions"])
        first = east["result"]["rows"][0]["Product"]
        assert first == frame[frame.Region == "East"].groupby("Product").Sales.sum().idxmax()


def test_lowest_ranking_text_does_not_call_the_lowest_one_a_leader():
    with auth_on():
        client, ds = setup_orders()
        body = chat(client, "Which region had the lowest sales?", dataset_id=ds)
        text = body["insight"]
        assert "South is the lowest" in text and "leads" not in text and "highest in this view is West" in text


def test_defined_metric_suppresses_the_undefined_margin_warning_and_matches_pandas():
    import io

    import pandas as pd

    with auth_on():
        client, ds = setup_orders()
        r = post(client, "/api/business-metrics", {"dataset_id": ds, "name": "Profit Margin", "expression": "SUM(Profit) / SUM(Sales)"})
        assert r.status_code == 201
        body = chat(client, "What is Profit Margin?", dataset_id=ds)
        assert not any("not a column or a defined business metric" in a for a in body.get("assumptions", []))
        frame = pd.read_csv(io.BytesIO(data.orders_csv(("South", "Furniture"))))
        value = next(v for v in body["result"]["rows"][0].values() if isinstance(v, (int, float)))
        assert abs(value - frame.Profit.sum() / frame.Sales.sum()) < 1e-4


def test_chart_axis_titles_are_readable_not_raw_column_names():
    with auth_on():
        client, ds = setup_orders()
        body = chat(client, "Show total Sales by Region", dataset_id=ds)
        lay = body["visualization"]["figure"]["layout"]
        texts = [lay["xaxis"]["title"]["text"], lay["yaxis"]["title"]["text"]]
        assert not any("_" in t for t in texts), texts
        trend = chat(client, "Show monthly sales trend", dataset_id=ds)["visualization"]["figure"]["layout"]
        assert not any("_" in trend[a]["title"]["text"] for a in ("xaxis", "yaxis"))
