"""Business-question intent classification for the agent layer.

Rules decide first (fast, free, testable). When a rule cannot tell and a real LLM is configured,
the model may choose from the same closed list; anything outside the list is ignored.
The intent sets the *strategy and complexity*; it never decides a number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.utils.exceptions import LLMError
from app.utils.logger import get_logger

log = get_logger("agent.intents")

INTENTS = ("kpi", "trend", "comparison", "ranking", "distribution", "correlation", "diagnostic", "root_cause", "anomaly",
           "segmentation", "forecasting", "recommendation", "data_quality", "dashboard_generation", "cross_source", "other")
SIMPLE = {"kpi", "trend", "comparison", "ranking", "segmentation", "other"}
# Intents the agent loop handles itself. Simple intents go through the single-pass pipeline (a fixed
# 10-step investigation for "what is total sales?" would be wasteful); recommendation keeps its own engine.
AGENT_INTENTS = {"diagnostic", "root_cause", "anomaly", "correlation", "distribution", "data_quality", "cross_source"}


@dataclass
class BusinessIntent:
    name: str
    complexity: str  # simple | complex
    method: str  # rules | llm


_RULES: list[tuple[str, re.Pattern]] = [
    ("dashboard_generation", re.compile(r"\b(dashboard|overview page|kpi (page|board))\b|\bcreate (a |an )?(sales |revenue |executive )?(report|dashboard)\b", re.I)),
    ("data_quality", re.compile(r"\b(data quality|missing (values|data)|null values|duplicates?|duplicate rows|how clean|data (issues|problems)|quality (issues|problems|check))\b", re.I)),
    ("forecasting", re.compile(r"\b(forecast(ing)?|predict\w*|projection|project(ed)? (sales|revenue|profit)|next (month|quarter|year)'?s? (sales|revenue|profit))\b", re.I)),
    ("root_cause", re.compile(r"\b(root cause|what caused|what is causing|what's causing|reason(s)? (for|behind))\b", re.I)),
    ("diagnostic", re.compile(
        r"\b(why\s+(did|has|have|is|are|was|were|do|does)\b.{0,60}\b(fall|fell|drop\w*|declin\w*|decreas\w*|dip\w*|increas\w*|grow\w*|grew|rise|rose|"
        r"chang\w*|down|up|lower|higher|worse|better|low\w*|high\w*)\b|what\s+(drove|is driving|are driving|explains?|happened to)\b|"
        r"(find|investigate|explain|diagnose)\b.{0,30}\b(drop|decline|decrease|change|increase|fall)\b|what('?s| is| are)\s+(behind|driving))", re.I)),
    ("anomaly", re.compile(r"\b(anomal\w*|unusual|outliers?|spikes?|abnormal|unexpected (drop|jump|change)|out of the ordinary|irregular)\b", re.I)),
    ("correlation", re.compile(r"\b(correlat\w*|relationship between|associated with|related to|link between)\b", re.I)),
    ("distribution", re.compile(r"\b(distribut\w*|histogram|spread of|how (are|is) .{0,40} spread|range of values)\b", re.I)),
    ("recommendation", re.compile(r"\b(recommend\w*|what should (we|i)|how (can|do|should) (we|i) (improve|fix|increase|reduce)|next steps?|opportunit(y|ies))\b", re.I)),
    ("ranking", re.compile(r"\b(top|bottom|highest|lowest|best|worst|rank\w*|leading|largest|smallest)\b", re.I)),
    ("comparison", re.compile(r"\b(compare|versus|vs\.?|difference between|against)\b", re.I)),
    ("trend", re.compile(r"\b(trend|over time|monthly|weekly|daily|quarterly|yearly|per month|by month|by year|growth)\b", re.I)),
    ("segmentation", re.compile(r"\b(segment\w*|breakdown|split by|group(ed)? by|by (region|category|product|customer))\b", re.I)),
    ("kpi", re.compile(r"\b(total|sum|average|avg|mean|count|how much|how many|number of|max|min|kpi)\b", re.I)),
]


def classify_with_rules(question: str) -> BusinessIntent:
    for name, pattern in _RULES:
        if pattern.search(question or ""):
            return BusinessIntent(name, "simple" if name in SIMPLE else "complex", "rules")
    return BusinessIntent("other", "simple", "rules")


def classify(question: str) -> BusinessIntent:
    found = classify_with_rules(question)
    if found.name != "other":
        return found
    try:
        from app.ai import llm_provider

        provider = llm_provider.get_provider()
        if provider.name == "demo":
            return found
        response = llm_provider.complete(
            "Classify the business question into exactly one of: " + ", ".join(INTENTS) +
            '. Reply with JSON: {"intent": "<one of them>"}.', question[:500], json_mode=True, purpose="intent")
        data = response.json()
        name = data.get("intent") if isinstance(data, dict) else None
        if name in INTENTS:
            return BusinessIntent(name, "simple" if name in SIMPLE else "complex", "llm")
    except LLMError as exc:
        log.info("LLM intent classification unavailable (%s); keeping rules result", exc.detail)
    return found
