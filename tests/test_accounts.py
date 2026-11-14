"""Plan limits, usage endpoint, organizations/roles scaffolding, rate-limit store."""

from __future__ import annotations

from config import Config
from app.database.connection import app_store
from app.security import rate_store
from tests.test_agent_loop import chat, setup_orders
from tests.test_auth import HDR, auth_on, post, signup


def test_usage_endpoint_reports_plan_and_never_invents_cost():
    with auth_on():
        client, ds = setup_orders()
        chat(client, "What is total sales?")
        body = client.get("/api/usage").get_json()
        assert body["plan"]["plan"] == "free" and body["plan"]["analyses_used"] >= 1
        assert body["llm"]["est_cost_usd"] is None and body["llm"]["total_calls"] == 0  # no LLM, no cost


def test_plan_limit_is_enforced_server_side_only_when_switched_on():
    with auth_on():
        client, ds = setup_orders()
        old = (Config.ENFORCE_PLAN_LIMITS, Config.PLAN_LIMIT_FREE)
        try:
            Config.PLAN_LIMIT_FREE = 2
            Config.ENFORCE_PLAN_LIMITS = False
            for _ in range(3):
                assert post(client, "/api/chat", {"question": "What is total sales?"}).status_code == 200
            Config.ENFORCE_PLAN_LIMITS = True
            r = post(client, "/api/chat", {"question": "What is total sales?"})
            assert r.status_code == 429 and r.get_json()["error"] == "PlanLimitError"
            st = client.get("/api/usage").get_json()["plan"]
            assert st["analyses_remaining"] == 0 and st["enforced"]
            # deleting history does not hand the allowance back
            client.delete("/api/history", headers=HDR)
            assert post(client, "/api/chat", {"question": "What is total sales?"}).status_code == 429
            # a higher plan lifts it
            with app_store() as conn:
                conn.execute("UPDATE users SET plan='business'")
            assert post(client, "/api/chat", {"question": "What is total sales?"}).status_code == 200
        finally:
            Config.ENFORCE_PLAN_LIMITS, Config.PLAN_LIMIT_FREE = old


def test_organization_roles_are_validated_and_members_only():
    with auth_on():
        client, _ = setup_orders()
        org = post(client, "/api/orgs", {"name": "Acme Retail"}).get_json()
        other = client.application.test_client()
        signup(other, "analyst@example.com")
        assert other.get(f"/api/orgs/{org['id']}/members").status_code == 403  # not a member
        r = client.put(f"/api/orgs/{org['id']}/members", json={"email": "analyst@example.com", "role": "analyst"}, headers=HDR)
        assert r.status_code == 200
        assert other.get(f"/api/orgs/{org['id']}/members").status_code == 200
        assert client.put(f"/api/orgs/{org['id']}/members", json={"email": "analyst@example.com", "role": "god"}, headers=HDR).status_code == 400
        # an analyst cannot grant roles
        denied = other.put(f"/api/orgs/{org['id']}/members", json={"email": "analyst@example.com", "role": "admin"}, headers=HDR)
        assert denied.status_code == 403
        # the last owner cannot be demoted
        me = client.get("/api/auth/me").get_json()
        email = (me.get("user") or me)["email"]
        assert client.put(f"/api/orgs/{org['id']}/members", json={"email": email, "role": "viewer"}, headers=HDR).status_code == 400


def test_rate_store_is_swappable_and_enforces_its_window():
    store = rate_store.MemoryRateStore()
    assert [store.hit("k", 2, 60) for _ in range(3)] == [True, True, False]
    assert store.hit("other", 2, 60)
    calls = []

    class Fake:
        def hit(self, name, limit, window):
            calls.append(name)
            return False

        def reset(self):
            pass

    from app.services import auth_service
    from app.utils.exceptions import TooManyRequestsError
    old = rate_store.get_store()
    rate_store.set_store(Fake())
    try:
        try:
            auth_service.rate_limit("b", "k", 1, 1)
            raised = False
        except TooManyRequestsError:
            raised = True
        assert raised and calls == ["b:k"]
    finally:
        rate_store.set_store(old)
