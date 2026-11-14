"""Deterministic stub LLM for agent-loop tests.

Agent loop tests use a deterministic stub LLM when no external provider is configured: no real
OpenAI / Anthropic / Gemini call is made anywhere in the test suite. The stub replays a scripted
list of replies (tool choices), so tests can exercise LLM-policy behaviour - valid choices,
hallucinated tools, bad arguments, repeated calls - without network access or API keys.
"""

from __future__ import annotations

import json

from app.ai import llm_provider
from app.ai.llm_provider import LLMProvider, LLMResponse


class ScriptedProvider(LLMProvider):
    name = "stub"
    default_model = "stub-scripted-1"

    def __init__(self, replies: list, usage: dict | None = None, text_fallback: str = ""):
        super().__init__()
        self.replies = list(replies)
        self.usage = usage
        self.text_fallback = text_fallback
        self.calls: list[tuple[str, str]] = []

    def available(self) -> bool:
        return True

    def complete(self, system: str, user: str, json_mode: bool = False) -> LLMResponse:
        self.calls.append((system, user))
        if json_mode or not self.text_fallback:
            reply = self.replies.pop(0) if self.replies else {"action": "finish", "reason": "script exhausted"}
            text = reply if isinstance(reply, str) else json.dumps(reply)
        else:
            text = self.text_fallback
        return LLMResponse(text=text, provider=self.name, model=self.model, latency_ms=1.0,
                           usage=self.usage if self.usage is not None else {}, from_demo=False)


class installed:
    """Context manager: make the stub the active provider, then restore the previous one."""

    def __init__(self, provider: ScriptedProvider):
        self.provider = provider

    def __enter__(self):
        self.previous = llm_provider._provider
        llm_provider._provider = self.provider
        return self.provider

    def __exit__(self, *exc):
        llm_provider._provider = self.previous
