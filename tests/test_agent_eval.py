"""The agent evaluation suite itself must pass on the planted-answer data."""

from __future__ import annotations

import io

import pandas as pd

from app.evaluation import agent_eval
from tests import agent_data as data
from tests.test_auth import auth_on, post, signup


def test_agent_scorecard_has_no_failures():
    with auth_on():
        client, _ = signup()
        frames = {}
        for key, collapse in (("collapse", ("South", "Furniture")), ("steady", None)):
            raw = data.orders_csv(collapse)
            frames[key] = (data.upload(client, raw, f"{key}.csv"), pd.read_csv(io.BytesIO(raw)))

        def ask(question, dataset_id=None):
            r = post(client, "/api/chat", {"question": question, "dataset_id": dataset_id})
            assert r.status_code == 200, r.get_json()
            return r.get_json()

        result = agent_eval.run(ask, frames)
        assert result["scorecard"]["total"] >= 15
        assert not result["scorecard"]["failures"], result["scorecard"]["failures"]
        assert result["llm_calls"] == 0 and "rule policy" in result["provider_note"]
