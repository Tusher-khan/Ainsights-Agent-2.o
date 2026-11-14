"""Landing page, chart interaction support, narrative, sample data and onboarding endpoints."""

from __future__ import annotations

from pathlib import Path

from tests import agent_data as data
from tests.test_agent_loop import chat, setup_orders
from tests.test_auth import auth_on, browser, post, signup

ROOT = Path(__file__).resolve().parents[1]


def test_landing_is_public_scrollable_and_makes_no_private_api_calls():
    with auth_on():
        r = browser().get("/")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "Turn Your Business Data Into" in html and 'href="/register"' in html and "See How It Works" in html
        assert "/api/datasets" not in html and "/api/chat" not in html  # an anonymous visitor must never trigger a 401 redirect
        assert "app-shell" not in html  # the viewport lock belongs to the signed-in shell only


def test_viewport_lock_is_scoped_and_public_paths_never_redirect():
    css = (ROOT / "static/css/style.css").read_text()
    assert "html.app-shell, html.app-shell body" in css
    assert "\n  html, body { height: 100%" not in css
    assert 'class="app-shell"' in (ROOT / "templates/base.html").read_text()
    assert "PUBLIC_PATHS" in (ROOT / "static/js/app.js").read_text()


def test_landing_for_signed_in_user_offers_workspace():
    with auth_on():
        client, _ = signup()
        assert "Open workspace" in client.get("/").get_data(as_text=True)


def test_sample_dataset_is_private_idempotent_and_deletable():
    with auth_on():
        a, _ = signup()
        b, _ = signup()
        first = post(a, "/api/datasets/sample").get_json()["dataset"]
        again = post(a, "/api/datasets/sample").get_json()["dataset"]
        assert first["id"] == again["id"]
        assert not [d for d in b.get("/api/datasets").get_json()["datasets"] if d["id"] == first["id"]]
        assert b.get(f"/api/datasets/{first['id']}").status_code == 404
        assert a.delete(f"/api/datasets/{first['id']}", headers={"X-AInsight-Client": "test"}).status_code == 200
        assert (ROOT / "data/examples/superstore_sample.csv").exists()  # the bundled example is never removed


def _region_chart(client):
    ds = data.upload(client, data.orders_csv(None), "orders.csv")
    body = chat(client, "Which region performs best?", dataset_id=ds)
    return ds, body


def test_ranking_answer_has_validated_narrative_and_next_steps():
    with auth_on():
        client, _ = signup()
        _, body = _region_chart(client)
        n = body["narrative"]
        top = body["result"]["rows"][0]
        label, value = body["result"]["columns"][0], body["result"]["columns"][-1]
        assert str(top[label]) in n["fact"]
        assert n["interpretation"] and n["recommendation"] and n["next"]
        assert "because" not in (n["interpretation"] + n["recommendation"]).lower()  # no causal claims
        assert all(q.strip() for q in n["next"])


def test_drilldown_only_for_real_chart_values_and_owner():
    with auth_on():
        a, _ = signup()
        _, body = _region_chart(a)
        hid = body["history_id"]
        top = str(body["result"]["rows"][0][body["result"]["columns"][0]])
        ok = a.get(f"/api/chart/{hid}/drilldown?label={top}")
        assert ok.status_code == 200 and ok.get_json()["questions"]
        assert all(top in q for q in ok.get_json()["questions"])
        assert a.get(f"/api/chart/{hid}/drilldown?label=Atlantis").status_code == 400  # forged value refused
        b, _ = signup()
        assert b.get(f"/api/chart/{hid}/drilldown?label={top}").status_code == 400  # another user's analysis is invisible


def test_chart_context_follows_up_and_cannot_cross_users():
    with auth_on():
        a, _ = signup()
        ds, body = _region_chart(a)
        hid = body["history_id"]
        r = post(a, "/api/chat", {"question": "Why is North ahead?", "chart_context": {"history_id": hid}})
        assert r.status_code == 200, r.get_json()
        out = r.get_json()
        assert out["plan"]["filters"] and out["plan"]["filters"][0]["value"] == "North"
        assert out["plan"]["dimensions"] and out["plan"]["dimensions"][0] != "Region"  # a breakdown, not the same ranking
        assert any("can show what" in s for s in out["assumptions"])  # honest: composition, not causation
        b, _ = signup()
        denied = post(b, "/api/chat", {"question": "Why is North ahead?", "chart_context": {"history_id": hid}})
        assert denied.status_code == 400


def test_explicit_breakdown_does_not_group_by_the_filtered_column():
    with auth_on():
        client, _ = signup()
        ds = data.upload(client, data.orders_csv(None), "orders.csv")
        out = chat(client, "Show Sales by Category where Region is West", dataset_id=ds)
        assert out["plan"]["dimensions"] == ["Category"]
        assert out["plan"]["filters"][0]["column"] == "Region"
        trend = chat(client, "Show monthly Sales trend where Region is West", dataset_id=ds)
        assert trend["plan"]["dimensions"] == []


def test_frontend_chart_tools_are_present():
    js = (ROOT / "static/js/charts.js").read_text()
    for needle in ("openModal", "downloadImage", "plotly_click", "dataTable", "chart-ask", "ainsight:ask", "hiddenlabels"):
        assert needle in js
    chat_js = (ROOT / "static/js/chat.js").read_text()
    assert "chart_context" in chat_js and "narrativeSection" in chat_js
