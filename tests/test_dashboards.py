"""Dashboard builder: config-only storage, data-driven rendering, filters, chat intent, isolation."""

from __future__ import annotations

from tests.test_agent_loop import chat, setup_orders
from tests.test_auth import HDR, auth_on, post, signup


def make(client, ds, **extra):
    r = post(client, "/api/dashboards", {"dataset_id": ds, **extra})
    assert r.status_code == 201, r.get_json()
    return r.get_json()["dashboard"]


def test_dashboard_is_built_from_dataset_metadata_and_stores_config_only():
    with auth_on():
        client, ds = setup_orders()
        d = make(client, ds)
        cfg = d["config"]
        assert cfg["kpis"] and any(c["type"] == "trend" for c in cfg["charts"])
        assert "rows" not in str(cfg) and "data" not in cfg
        view = client.get(f"/api/dashboards/{d['id']}").get_json()
        assert view["kpis"] and view["figures"]
        assert all("data" in f and "layout" in f for f in view["figures"])
        assert view["kpis"][0]["value"] > 0


def test_filters_change_the_numbers_and_unknown_filters_are_ignored():
    with auth_on():
        client, ds = setup_orders()
        d = make(client, ds)
        full = client.get(f"/api/dashboards/{d['id']}").get_json()
        dim = next(iter(full["filters"]))
        value = full["filters"][dim][0]
        filt = client.get(f"/api/dashboards/{d['id']}?f_{dim}={value}").get_json()
        assert filt["rows"] < full["rows"] and filt["applied_filters"]
        ignored = client.get(f"/api/dashboards/{d['id']}?f_NotAColumn=x").get_json()
        assert ignored["rows"] == full["rows"]


def test_rename_delete_and_cross_user_isolation():
    with auth_on():
        client, ds = setup_orders()
        d = make(client, ds)
        assert client.patch(f"/api/dashboards/{d['id']}", json={"name": "Board"}, headers=HDR).status_code == 200
        assert any(x["name"] == "Board" for x in client.get("/api/dashboards").get_json()["dashboards"])
        other = client.application.test_client()
        signup(other, "dash-intruder@example.com")
        assert other.get(f"/api/dashboards/{d['id']}").status_code >= 400
        assert other.get("/api/dashboards").get_json()["dashboards"] == []
        assert other.delete(f"/api/dashboards/{d['id']}", headers=HDR).status_code >= 400
        assert client.delete(f"/api/dashboards/{d['id']}", headers=HDR).status_code == 200


def test_cannot_build_a_dashboard_from_someone_elses_dataset():
    with auth_on():
        client, ds = setup_orders()
        other = client.application.test_client()
        signup(other, "dash-intruder2@example.com")
        assert post(other, "/api/dashboards", {"dataset_id": ds}).status_code >= 400


def test_chat_can_create_a_dashboard_and_history_can_seed_one():
    with auth_on():
        client, ds = setup_orders()
        body = chat(client, "Create a sales dashboard", dataset_id=ds)
        assert body["dashboard"]["url"].startswith("/dashboards/")
        assert client.get(f"/api/dashboards/{body['dashboard']['id']}").status_code == 200
        why = chat(client, "Why did our sales decrease last month?")
        d = make(client, ds, from_history=why["history_id"])
        assert "Why did our sales" in d["name"]


def test_dashboard_numbers_match_pandas_and_rates_are_averaged_not_summed():
    import io

    import pandas as pd

    from tests import agent_data as data

    frame = pd.read_csv(io.BytesIO(data.orders_csv(("South", "Furniture"))))
    with auth_on():
        client, ds = setup_orders()
        d = make(client, ds)
        view = client.get(f"/api/dashboards/{d['id']}").get_json()
        kp = {k["label"]: k for k in view["kpis"]}
        assert abs(kp["Sales"]["value"] - frame.Sales.sum()) < 0.01
        assert "Average Discount" in kp and abs(kp["Average Discount"]["value"] - frame.Discount.mean()) < 1e-6
        trend = next(f for f in view["figures"] if f["data"][0]["type"] == "scatter")
        assert abs(sum(trend["data"][0]["y"]) - frame.Sales.sum()) < 0.01
        titles = [f["layout"]["title"]["text"] for f in view["figures"]]
        assert len(titles) == len(set(titles)) and any(" by " in t for t in titles)
        office = client.get(f"/api/dashboards/{d['id']}?f_Category=Office").get_json()
        assert abs(office["kpis"][0]["value"] - frame[frame.Category == "Office"].Sales.sum()) < 0.01
