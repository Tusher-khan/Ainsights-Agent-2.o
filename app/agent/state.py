"""Structured agent state.

Holds only safe, machine-readable information about one analysis run. It never holds
database passwords, API secrets, tokens or private keys: tool arguments are reduced to ids
and short scalars before being recorded, and tool results are kept in compact form.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.utils.helpers import new_id, utcnow

_SENSITIVE = ("password", "secret", "token", "api_key", "apikey", "authorization", "credential", "private_key")


def safe_args(args: dict) -> dict:
    """Arguments as they may be stored: scalars only, long text truncated, sensitive names dropped, SQL omitted."""
    out: dict[str, Any] = {}
    for key, value in (args or {}).items():
        low = key.lower()
        if any(s in low for s in _SENSITIVE) or low in {"sql", "plan", "rows", "columns", "body"}:
            out[key] = "(omitted)"
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, str):
            out[key] = value if len(value) <= 80 else value[:77] + "…"
        elif isinstance(value, dict):
            out[key] = {str(k): (str(v)[:40]) for k, v in list(value.items())[:6]}
        else:
            out[key] = f"({type(value).__name__})"
    return out


@dataclass
class AgentState:
    question: str
    user_id: str | None = None
    analysis_id: str = field(default_factory=lambda: new_id("an"))
    intent: str = ""
    complexity: str = "complex"
    selected_sources: list[dict] = field(default_factory=list)
    available_tools: list[str] = field(default_factory=list)
    analysis_plan: list[dict] = field(default_factory=list)
    completed_steps: list[dict] = field(default_factory=list)
    tool_results: dict[int, dict] = field(default_factory=dict)  # compact, in-memory only
    findings: list[dict] = field(default_factory=list)
    visualization_data: list[dict] = field(default_factory=list)
    final_insights: dict = field(default_factory=dict)
    recommendations: list[str] = field(default_factory=list)
    status: str = "running"  # running | complete | partial | failed
    errors: list[str] = field(default_factory=list)
    policy: str = "rules"  # which decision-maker chose the steps: "llm" | "rules" | "llm+rules"
    evidence: str = "limited"
    stages: list[dict] = field(default_factory=list)  # real events, in order
    started: float = field(default_factory=time.perf_counter)
    started_at: str = field(default_factory=utcnow)
    tokens_used: int = 0
    llm_calls: int = 0
    context: dict = field(default_factory=dict)  # working memory for the policy (not persisted)

    def stage(self, name: str, status: str = "done", detail: str = "", ms: float | None = None) -> None:
        self.stages.append({"stage": name, "status": status, "detail": detail,
                            "ms": round(ms if ms is not None else (time.perf_counter() - self.started) * 1000, 1)})
        from app.services import progress
        progress.emit(self.stages[-1])

    @property
    def elapsed_s(self) -> float:
        return time.perf_counter() - self.started

    def source_by_id(self, dataset_id: str) -> dict | None:
        return next((s for s in self.selected_sources if s["id"] == dataset_id), None)

    def public(self) -> dict:
        """What may be saved or returned: no tool result payloads beyond findings, no secrets."""
        return {
            "analysis_id": self.analysis_id, "question": self.question, "intent": self.intent, "complexity": self.complexity,
            "selected_sources": self.selected_sources, "available_tools": self.available_tools,
            "analysis_plan": self.analysis_plan, "completed_steps": self.completed_steps, "findings": self.findings,
            "status": self.status, "errors": self.errors, "policy": self.policy, "evidence": self.evidence,
            "steps_used": len(self.completed_steps), "tokens_used": self.tokens_used, "llm_calls": self.llm_calls,
            "elapsed_ms": round(self.elapsed_s * 1000), "started_at": self.started_at,
        }
