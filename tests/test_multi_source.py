"""Hybrid analysis: actual (connected database) vs forecast (uploaded spreadsheet)."""

from __future__ import annotations

import io

import pytest

from tests.test_auth import HDR, auth_on, post, signup
from tests.test_connectors import connect_sqlite, import_orders

FORECAST_CSV = (
    "Month Date,Forecast Sales\n2024-01-31,300\n2024-02-29,400\n2024-03-31,250\n2024-04-30,100\n"
).encode()


def upload_forecast(client, name="Sales Forecast.csv"):
    r = client.post("/api/upload", data={"file": (io.BytesIO(FORECAST_CSV), name)}, content_type="multipart/form-data", headers=HDR)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["dataset"]["id"]


def test_actual_in_database_vs_forecast_in_spreadsheet(tmp_path):
    with auth_on():
        client, _ = signup()
        import_orders(client, connect_sqlite(client, tmp_path)["id"])
        upload_forecast(client)
        reply = post(client, "/api/chat", {"question": "Compare actual sales with our forecast"})
        body = reply.get_json()
        assert reply.status_code == 200, body
        assert body["intent"] == "multi_source_comparison"
        rows = {r["order_date"] if "order_date" in r else list(r.values())[0]: r for r in body["result"]["rows"]}
        assert set(rows) == {"2024-01", "2024-02", "2024-03"}  # April exists only in the forecast
        assert rows["2024-01"]["actual"] == 350.0 and rows["2024-01"]["reference"] == 300.0 and rows["2024-01"]["variance"] == 50.0
        assert rows["2024-02"]["variance"] == -25.0 and rows["2024-02"]["variance_pct"] == pytest.approx(-6.25)
        assert {s["engine"] for s in body["sources_used"]} == {"sql", "pandas"}  # each aggregated in its own source
        checks = {c["name"]: c["passed"] for c in body["validation"]["result"]["checks"]}
        assert all(checks.values()) and body["validation"]["verified"]
        assert any("only exist in the reference" in w for w in body["validation"]["result"]["warnings"])
        assert body["visualization"]["figure"]["data"] and body["extra_visualizations"]
        assert "below" in body["answer"] or "above" in body["answer"]
        assert any("Used" not in a for a in body["assumptions"])


def test_no_forecast_dataset_falls_back_to_normal_analysis(tmp_path):
    with auth_on():
        client, _ = signup()
        import_orders(client, connect_sqlite(client, tmp_path)["id"])
        body = post(client, "/api/chat", {"question": "Compare sales against target by region"}).get_json()
        assert body.get("intent") != "multi_source_comparison"


def test_comparison_never_reads_another_users_forecast(tmp_path):
    with auth_on():
        alice, _ = signup()
        bob, _ = signup()
        import_orders(alice, connect_sqlite(alice, tmp_path)["id"])
        upload_forecast(bob)  # bob's forecast must be invisible to alice
        body = post(alice, "/api/chat", {"question": "Compare actual sales with our forecast"}).get_json()
        assert body.get("intent") != "multi_source_comparison"
        assert "300" not in str(body.get("result", ""))


def test_unmatched_keys_are_an_honest_error(tmp_path):
    with auth_on():
        client, _ = signup()
        import_orders(client, connect_sqlite(client, tmp_path)["id"])
        far = b"Month Date,Forecast Sales\n2030-01-31,300\n2030-02-28,400\n"
        client.post("/api/upload", data={"file": (io.BytesIO(far), "forecast.csv")}, content_type="multipart/form-data", headers=HDR)
        reply = post(client, "/api/chat", {"question": "Compare actual sales with the forecast"})
        assert reply.status_code == 422 and "share no common" in reply.get_json()["message"]


def test_zz_cleanup_search_index():
    from tests.test_connectors import test_zz_cleanup_search_index as cleanup

    cleanup()
