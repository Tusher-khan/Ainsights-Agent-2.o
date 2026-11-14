"""Analysis history: save, reopen (no re-run), refresh (re-run), rename, favorite, search/filter, delete, isolation."""

from __future__ import annotations

import io

from tests import agent_data as data
from tests.test_agent_loop import chat, setup_orders
from tests.test_auth import HDR, auth_on, post, signup


def patch(client, path, body):
    return client.patch(path, json=body, headers=HDR)


def events_count(client):
    return len(client.get("/api/agent/events").get_json()["events"])


def test_agent_analysis_is_saved_with_everything_needed_to_reopen_it():
    with auth_on():
        client, _ = setup_orders()
        body = chat(client, "Why did our sales decrease last month?")
        hid = body["history_id"]
        before = events_count(client)
        saved = client.get(f"/api/history/{hid}/reopen").get_json()
        assert events_count(client) == before  # reopening runs nothing
        assert saved["replayed"] and saved["data_last_analyzed"]
        resp = saved["response"]
        assert resp["answer"] == body["answer"] and resp["agent"]["steps"] and resp["investigation"]["kpis"]
        assert resp["agent"]["findings"] and resp["extra_visualizations"]
        text = str(saved)
        assert "password" not in text.lower() and "api_key" not in text.lower()


def test_normal_pipeline_answers_are_saved_too():
    with auth_on():
        client, _ = setup_orders()
        body = chat(client, "What is total sales?")
        saved = client.get(f"/api/history/{body['history_id']}/reopen").get_json()
        assert saved["response"]["result"]["rows"] == body["result"]["rows"]


def test_refresh_reruns_the_analysis_and_replaces_the_stored_result():
    with auth_on():
        client, ds = setup_orders()
        body = chat(client, "Why did our sales decrease last month?")
        hid = body["history_id"]
        runs_before = events_count(client)
        saved_before = client.get(f"/api/history/{hid}/reopen").get_json()
        assert events_count(client) == runs_before  # reopen: nothing executed
        refreshed = post(client, f"/api/history/{hid}/refresh").get_json()
        assert events_count(client) > runs_before  # refresh: the tools really ran again
        assert refreshed["refreshed"] and refreshed["history_id"] == hid and refreshed["agent"]["steps"]
        saved = client.get(f"/api/history/{hid}/reopen").get_json()
        assert saved["refreshed_at"] and saved["data_last_analyzed"] == saved["refreshed_at"]
        assert saved["response"]["agent"]["analysis_id"] != saved_before["response"]["agent"]["analysis_id"]
        ids = [h["id"] for h in client.get("/api/history").get_json()["history"]]
        assert ids == [hid]  # refresh replaces the entry; it does not add a duplicate


def test_rename_favorite_search_filter_delete():
    with auth_on():
        client, _ = setup_orders()
        a = chat(client, "What is total sales?")["history_id"]
        b = chat(client, "Why did our sales decrease last month?")["history_id"]
        assert patch(client, f"/api/history/{a}", {"title": "Headline sales", "favorite": True}).status_code == 200
        assert patch(client, f"/api/history/{a}", {"title": "  "}).status_code in (400, 422)
        listing = client.get("/api/history").get_json()["history"]
        assert listing[0]["id"] == a and listing[0]["favorite"] and listing[0]["title"] == "Headline sales"  # favorites first
        assert [h["id"] for h in client.get("/api/history?favorite=1").get_json()["history"]] == [a]
        assert [h["id"] for h in client.get("/api/history?q=headline").get_json()["history"]] == [a]
        assert [h["id"] for h in client.get("/api/history?q=decrease").get_json()["history"]] == [b]
        assert [h["id"] for h in client.get("/api/history?intent=investigation").get_json()["history"]] == [b]
        assert client.get("/api/history?q=nonexistent-zzz").get_json()["history"] == []
        assert client.delete(f"/api/history/{b}", headers=HDR).status_code == 200
        assert client.get(f"/api/history/{b}/reopen").status_code in (400, 404, 422)


def test_other_users_cannot_reopen_refresh_rename_or_see_history():
    with auth_on():
        alice, _ = setup_orders()
        hid = chat(alice, "What is total sales?")["history_id"]
        bob, _ = signup()
        assert bob.get(f"/api/history/{hid}/reopen").status_code in (400, 404, 422)
        assert post(bob, f"/api/history/{hid}/refresh").status_code in (400, 404, 422)
        assert patch(bob, f"/api/history/{hid}", {"title": "mine now"}).status_code in (400, 404, 422)
        assert bob.get("/api/history").get_json()["history"] == []
        assert alice.get("/api/history").get_json()["history"][0]["title"] is None


def test_zz_cleanup_search_index():
    from tests.test_connectors import test_zz_cleanup_search_index as cleanup

    cleanup()
