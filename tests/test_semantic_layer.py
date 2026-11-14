"""Business semantic layer: parsing, safety, and agreement between SQL and pandas execution."""

from __future__ import annotations

import pytest

from tests.test_auth import HDR, auth_on, browser, post, signup, upload
from tests.test_connectors import connect_sqlite, import_orders


def test_parser_accepts_business_formulas_and_rejects_code():
    from app.agent.metrics import MetricError, parse, render

    assert render(parse("SUM(profit) / SUM(sales)")) == '(SUM("profit") / SUM("sales"))'
    assert parse('SUM("Gross Revenue") - SUM([Returns]) - SUM(discounts)')
    for bad in ["__import__('os').system('x')", "SUM(a); DROP TABLE x", "profit / sales", "SUM(a) +", "SUM()", "LOWER(a)", "1 +* 2", "SUM(a" ]:
        with pytest.raises(MetricError):
            parse(bad)


def test_metric_rejects_unknown_and_non_numeric_columns(tmp_path):
    with auth_on():
        client, _ = signup()
        ds = import_orders(client, connect_sqlite(client, tmp_path)["id"])
        for expr in ["SUM(nonexistent)", "SUM(region)"]:
            r = post(client, "/api/business-metrics", {"dataset_id": ds, "name": "Bad", "expression": expr})
            assert r.status_code == 422, (expr, r.get_json())


def test_metric_matches_between_sql_source_and_csv_source(tmp_path):
    """The same definition must give the same numbers whether it runs in the database or in pandas."""
    with auth_on():
        client, _ = signup()
        sql_ds = import_orders(client, connect_sqlite(client, tmp_path)["id"])
        r = post(client, "/api/business-metrics", {"dataset_id": sql_ds, "name": "Profit Margin", "expression": "SUM(profit) / SUM(sales)"})
        assert r.status_code == 201, r.get_json()
        body = post(client, "/api/chat", {"question": "What is the profit margin by region?", "dataset_id": sql_ds}).get_json()
        assert body["metric"]["name"] == "Profit Margin"
        by_region = {row["region"]: row["profit_margin"] for row in body["result"]["rows"]}
        assert by_region["North"] == pytest.approx((10 + 7.5 + 0) / (100 + 75))  # North rows: 1,3
        assert by_region["East"] == pytest.approx(0.1)
        assert body["result"]["engine"] == "sql"
        assert any("Used your definition" in a for a in body["assumptions"])

        csv_ds = upload(client)  # sales.csv: Region, Sales, Profit
        post(client, "/api/business-metrics", {"dataset_id": csv_ds, "name": "Margin", "expression": "SUM(profit) / SUM(sales)"})
        b2 = post(client, "/api/chat", {"question": "Show the margin by region", "dataset_id": csv_ds}).get_json()
        got = {row["Region"]: row["margin"] for row in b2["result"]["rows"]}
        assert got["North"] == pytest.approx(16 / 160)
        assert b2["result"]["engine"] == "pandas"


def test_metrics_are_private_to_their_owner(tmp_path):
    with auth_on():
        alice, _ = signup()
        bob, _ = signup()
        ds = upload(alice)
        m = post(alice, "/api/business-metrics", {"dataset_id": ds, "name": "Mine", "expression": "SUM(Sales)"}).get_json()["metric"]
        assert bob.get("/api/business-metrics").get_json()["metrics"] == []
        assert post(bob, "/api/business-metrics", {"dataset_id": ds, "name": "Steal", "expression": "SUM(Sales)"}).status_code == 404
        assert bob.delete(f"/api/business-metrics/{m['id']}", headers=HDR).status_code == 422
        assert len(alice.get("/api/business-metrics").get_json()["metrics"]) == 1


def test_zz_cleanup_search_index():
    from tests.test_connectors import test_zz_cleanup_search_index as cleanup

    cleanup()
