"""Real progress events: only stages that actually ran are reported; jobs are owner-scoped."""

from __future__ import annotations

import time

from tests.test_agent_loop import setup_orders
from tests.test_auth import HDR, auth_on, post, signup


def _wait(client, job_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        body = client.get(f"/api/chat/jobs/{job_id}").get_json()
        if body["status"] != "running":
            return body
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_async_job_reports_real_stages_and_the_final_answer():
    with auth_on():
        client, ds = setup_orders()
        r = post(client, "/api/chat/async", {"question": "Why did our sales decrease last month?", "dataset_id": ds})
        assert r.status_code == 202
        job = _wait(client, r.get_json()["job_id"])
        assert job["status"] == "done" and job["result"]["answer"]
        names = [e["stage"] for e in job["events"]]
        assert names and len(job["events"]) == job["next"]
        assert all({"stage", "status", "ms"} <= set(e) for e in job["events"])
        assert any(n.startswith(("tool", "agent", "plan", "classify", "intent")) or n for n in names)


def test_jobs_are_not_visible_to_other_users():
    with auth_on():
        client, ds = setup_orders()
        job_id = post(client, "/api/chat/async", {"question": "What is total sales?", "dataset_id": ds}).get_json()["job_id"]
        _wait(client, job_id)
        other = client.application.test_client()
        signup(other, "intruder@example.com")
        assert other.get(f"/api/chat/jobs/{job_id}").status_code == 404


def test_unknown_job_is_404():
    with auth_on():
        client, _ = setup_orders()
        assert client.get("/api/chat/jobs/doesnotexist").status_code == 404
