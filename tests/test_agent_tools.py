"""Agent tool layer: authorization, validation and audit trail."""

from __future__ import annotations

from tests.test_auth import HDR, auth_on, browser, post, signup
from tests.test_connectors import connect_sqlite, import_orders


def call(client, name, body=None, **kw):
    return post(client, f"/api/agent/tools/{name}", body or {}, **kw)


def test_tool_list_requires_login_and_lists_tools():
    with auth_on():
        assert browser().get("/api/agent/tools").status_code == 401
        client, _ = signup()
        names = {t["name"] for t in client.get("/api/agent/tools").get_json()["tools"]}
        assert {"get_data_sources", "query_database", "analyze_dataframe", "calculate_metric",
                "detect_anomalies", "compare_sources", "validate_result"} <= names


def test_tools_run_real_work_on_own_data(tmp_path):
    with auth_on():
        client, _ = signup()
        ds = import_orders(client, connect_sqlite(client, tmp_path)["id"])
        meta = call(client, "get_dataset_metadata", {"dataset_id": ds}).get_json()["result"]
        assert meta["measures"] and meta["row_count"] > 0
        measure = meta["measures"][0]
        out = call(client, "query_database", {"dataset_id": ds, "sql": f"SELECT SUM({measure}) AS total FROM orders"})
        assert out.status_code == 200, out.get_json()
        assert out.get_json()["result"]["row_count"] == 1
        bad = call(client, "query_database", {"dataset_id": ds, "sql": "DELETE FROM orders"})
        assert bad.status_code in (400, 403, 422)
        stacked = call(client, "query_database", {"dataset_id": ds, "sql": "SELECT 1; DROP TABLE orders"})
        assert stacked.status_code in (400, 403, 422)


def test_other_users_ids_are_not_found(tmp_path):
    with auth_on():
        alice, _ = signup()
        bob, _ = signup()
        ds = import_orders(alice, connect_sqlite(alice, tmp_path)["id"])
        for name, body in [
            ("get_dataset_metadata", {"dataset_id": ds}),
            ("query_database", {"dataset_id": ds, "sql": "SELECT 1 FROM orders"}),
            ("detect_anomalies", {"dataset_id": ds, "measure": "amount"}),
        ]:
            assert call(bob, name, body).status_code == 404, name
        assert call(bob, "get_schema", {"source_id": "src_nope"}).status_code == 404
        assert call(bob, "get_data_sources").get_json()["result"]["datasets"] == []


def test_bad_calls_are_rejected():
    with auth_on():
        client, _ = signup()
        assert call(client, "does_not_exist").status_code in (400, 403, 422)
        assert call(client, "get_schema", {}).status_code in (400, 422)  # missing argument
        assert call(client, "get_data_sources", {"extra": 1}).status_code in (400, 422)  # unknown argument


def test_audit_trail_has_no_arguments_sql_or_data(tmp_path):
    with auth_on():
        client, _ = signup()
        ds = import_orders(client, connect_sqlite(client, tmp_path)["id"])
        first = call(client, "query_database", {"dataset_id": ds, "sql": "SELECT 'SECRET-MARKER-123' AS x FROM orders"})
        assert first.status_code == 200, first.get_json()
        call(client, "query_database", {"dataset_id": ds, "sql": "DROP TABLE orders"})
        events = client.get("/api/agent/events").get_json()["events"]
        assert [e["ok"] for e in events][:2] == [0, 1]
        assert all(e["tool"] == "query_database" for e in events)
        assert "SECRET-MARKER-123" not in str(events) and "DROP" not in str(events)
        other, _ = signup()
        assert other.get("/api/agent/events").get_json()["events"] == []


def test_zz_cleanup_search_index():
    from tests.test_connectors import test_zz_cleanup_search_index as cleanup

    cleanup()


def test_viewer_role_cannot_run_sql_or_rest_tools_but_can_analyze():
    from app.agent import tools
    from app.security import permissions as perm

    for name, needs in (("query_database", perm.SQL), ("call_business_api", perm.REST)):
        assert needs in tools.TOOLS[name].permissions
        assert needs not in perm.permissions_for("viewer") and needs in perm.permissions_for("analyst")
    assert perm.ANALYZE in perm.permissions_for("viewer")


def test_agent_chat_on_someone_elses_dataset_leaks_nothing():
    from tests import agent_data as data

    with auth_on():
        alice, _ = signup()
        ds = data.upload(alice, data.orders_csv(), "orders.csv")
        bob, _ = signup()
        r = post(bob, "/api/chat", {"question": "Why did our sales decrease last month?", "dataset_id": ds})
        text = str(r.get_json())
        assert r.status_code >= 400 or ("South" not in text and "Furniture" not in text)
        assert "orders" not in text.lower() or r.status_code >= 400
