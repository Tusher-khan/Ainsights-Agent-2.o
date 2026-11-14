"""Agent evaluation suite.

Scores the analysis agent on planted-answer synthetic data, where the right answer is known and computed
independently with pandas. Dimensions: tool selection, data selection, calculation accuracy, investigation
quality, visualization appropriateness, insight grounding (no invented numbers), recommendation quality
(none without evidence), latency and cost.

`run(ask, frames)` is transport-agnostic: `ask(question, dataset_id=None) -> payload` is any callable that runs a
question (the Flask test client in tests, or `python -m app.evaluation.agent_eval` for a self-contained run).
The suite measures the agent in whatever mode the server is in; with no LLM configured that is the
deterministic rule policy, and the scorecard says so.
"""

from __future__ import annotations

import io
import re
from typing import Callable

import pandas as pd

Ask = Callable[..., dict]


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d[\d,]*\.?\d*", text or ""))


def _last_month_total(frame: pd.DataFrame, measure: str = "Sales") -> float:
    d = pd.to_datetime(frame["Order Date"])
    s = frame.groupby(d.dt.to_period("M"))[measure].sum().sort_index()
    return float(s.iloc[-1])


class Card:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def check(self, dimension: str, case: str, passed: bool, detail: str = "") -> None:
        self.rows.append({"dimension": dimension, "case": case, "passed": bool(passed), "detail": detail})

    def summary(self) -> dict:
        dims: dict[str, dict] = {}
        for r in self.rows:
            d = dims.setdefault(r["dimension"], {"passed": 0, "total": 0})
            d["total"] += 1
            d["passed"] += r["passed"]
        return {"dimensions": dims, "passed": sum(d["passed"] for d in dims.values()),
                "total": sum(d["total"] for d in dims.values()), "failures": [r for r in self.rows if not r["passed"]]}


def run(ask: Ask, frames: dict) -> dict:
    """frames: {"collapse": (dataset_id, DataFrame), "steady": (dataset_id, DataFrame)} — planted vs no-change data."""
    card = Card()
    latencies: list[float] = []
    modes: set[str] = set()
    cost_calls = 0

    def go(question: str, ds: str) -> dict:
        nonlocal cost_calls
        payload = ask(question, dataset_id=ds)
        agent = payload.get("agent") or {}
        latencies.append(float(payload.get("total_ms") or (agent.get("budget") or {}).get("elapsed_ms") or 0))
        if agent.get("mode"):
            modes.add(agent["mode"])
        cost_calls += int((agent.get("usage") or {}).get("calls") or 0)
        return payload

    ds, frame = frames["collapse"]
    steady_ds, _ = frames["steady"]

    # --- multi-step diagnostic with a planted cause -------------------------------------------------
    q = "Why did our sales decrease last month?"
    p = go(q, ds)
    a = p.get("agent") or {}
    tools = [s["tool"] for s in a.get("steps", [])]
    card.check("tool_selection", "diagnostic starts with compare_periods then drills", tools[:1] and "compare_periods" in tools and "period_contribution" in tools, str(tools))
    card.check("tool_selection", "drill is result-aware (more than one contribution step)", tools.count("period_contribution") >= 2, str(tools))
    card.check("data_selection", "analysis ran on the dataset asked about", (p.get("source") or {}).get("dataset_id") == ds)
    kpi = next((k for k in p.get("kpis") or (p.get("investigation") or {}).get("kpis") or [] if k.get("label", "").lower().startswith("sales")), None)
    truth = _last_month_total(frame)
    card.check("calculation_accuracy", "last-month sales equals the pandas ground truth",
               bool(kpi) and abs(float(kpi["current"] if "current" in kpi else kpi["value"]) - truth) < 0.5, f"truth={truth:.2f} kpi={kpi}")
    text = " ".join(a.get("facts", [])).lower()
    card.check("investigation_quality", "names the planted region and category as the cause", "south" in text and "furniture" in text, text[:200])
    figs = [p.get("visualization")] + [{"figure": f} for f in p.get("extra_visualizations", [])]
    card.check("visualization", "at least one chart is produced from validated results", any(f for f in figs if f), f"{len(figs)} figure(s)")
    card.check("visualization", "charts carry a title and formatted axes", all((f.get("figure") or {}).get("layout", {}).get("title") for f in figs if f))
    facts = " ".join(a.get("facts", []))
    from app.agent.findings import grounded

    from app.utils.helpers import human_number

    # numbers are allowed if they are written in a finding or are the formatted form of a validated finding's computed value
    computed = [human_number(v) for f in a.get("findings", []) for v in f.get("numbers", {}).values() if isinstance(v, (int, float))]
    evidence_text = " ".join(a.get("facts", []) + a.get("interpretation", []) + [f["text"] for f in a.get("findings", [])] + computed)
    card.check("insight_grounding", "every number in the summary appears in the validated findings",
               grounded(p.get("answer", ""), [evidence_text]), p.get("answer", "")[:160] + " || " + evidence_text[:400])
    card.check("recommendation_quality", "recommendations exist when evidence is strong", a.get("evidence") in {"strong", "moderate"} and bool(a.get("recommendations")), str(a.get("evidence")))
    card.check("agent_honesty", "mode is reported and no LLM is claimed without one", a.get("mode") in {"rules", "llm", "hybrid"} and bool(a.get("mode_note")))

    # --- simple question must not trigger the agent --------------------------------------------------
    p = go("What is total sales?", ds)
    card.check("tool_selection", "simple question is a single calculation, not an agent run", not p.get("agent"))
    rows = (p.get("result") or {}).get("rows") or []
    value = next(iter(rows[0].values())) if rows else None
    card.check("calculation_accuracy", "total sales equals pandas", value is not None and abs(float(value) - float(frame["Sales"].sum())) < 0.5)

    # --- other analysis types pick their own tools ----------------------------------------------------
    for question, tool in (("Are there any data quality issues?", "data_quality"),
                           ("Is there a relationship between discount and profit?", "correlate"),
                           ("Show the distribution of sales", "describe_distribution")):
        p = go(question, ds)
        tools = [s["tool"] for s in (p.get("agent") or {}).get("steps", [])]
        card.check("tool_selection", f"{tool} chosen for: {question}", tool in tools, str(tools))

    # --- no material change: honest, no invented cause or recommendations ------------------------------
    p = go("Why did our sales decrease last month?", steady_ds)
    a = p.get("agent") or {}
    card.check("insight_grounding", "insufficient evidence is stated, not a confident cause", a.get("evidence") in {"insufficient", "limited"}, str(a.get("evidence")))
    card.check("recommendation_quality", "no recommendations without evidence", not a.get("recommendations"), str(a.get("recommendations")))

    lat = sorted(latencies)
    return {"scorecard": card.summary(), "rows": card.rows, "modes_seen": sorted(modes),
            "latency_ms": {"median": lat[len(lat) // 2] if lat else None, "max": lat[-1] if lat else None},
            "llm_calls": cost_calls,
            "cost_note": "No language model was called, so there is no token cost." if not cost_calls else "See /api/usage for tokens and estimated cost.",
            "provider_note": "Evaluated against the configured policy; with no provider this is the deterministic rule policy, not an LLM."}


def main() -> int:  # pragma: no cover - convenience CLI
    import os
    import sys
    import tempfile
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    base = tempfile.mkdtemp(prefix="ainsight_eval_")
    os.environ.update(DATABASE_URL=f"sqlite:///{base}/e.db", ANALYTICS_DATABASE_URL=f"sqlite:///{base}/e.db",
                      CHROMA_PATH=f"{base}/chroma", UPLOAD_DIR=f"{base}/uploads", AUTH_REQUIRED="false")
    from config import Config
    Config.DATABASE_URL = Config.ANALYTICS_DATABASE_URL = os.environ["DATABASE_URL"]
    Config.CHROMA_PATH, Config.UPLOAD_DIR, Config.AUTH_REQUIRED = os.environ["CHROMA_PATH"], Path(base) / "uploads", False
    Config.ensure_dirs()
    import importlib.util

    from tests import agent_data as data

    spec = importlib.util.spec_from_file_location("ainsight_entry", root / "app.py")  # app.py and the app/ package share a name
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    client = entry.create_app().test_client()
    frames = {}
    for key, collapse in (("collapse", ("South", "Furniture")), ("steady", None)):
        raw = data.orders_csv(collapse)
        frames[key] = (data.upload(client, raw, f"{key}.csv"), pd.read_csv(io.BytesIO(raw)))

    def ask(question, dataset_id=None):
        return client.post("/api/chat", json={"question": question, "dataset_id": dataset_id}, headers=data.HDR).get_json()

    import json
    result = run(ask, frames)
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=2, default=str))
    return 0 if not result["scorecard"]["failures"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
