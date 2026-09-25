"""Deterministic LLM backends for tests, the offline demo and fault injection.

``ScriptedLLMClient`` replays canned responses keyed by call *purpose*
(``persona:architect``, ``judge:architect_to_dev:tier1`` …). Each run consumes
its own copy of the script, so concurrent eval runs stay independent.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from sdlc_loop.llm.token_meter import estimate_tokens
from sdlc_loop.llm.types import LLMClient, LLMRequest, LLMResponse, TokenUsage

ScriptItem = str | Mapping[str, Any]


def _as_text(item: ScriptItem) -> str:
    return item if isinstance(item, str) else json.dumps(item)


def _usage_for(request: LLMRequest, text: str) -> TokenUsage:
    system_tokens = sum(estimate_tokens(b.text) for b in request.system)
    return TokenUsage(
        input_tokens=system_tokens + estimate_tokens(request.user_content),
        output_tokens=estimate_tokens(text),
    )


class ScriptExhaustedError(LookupError):
    pass


class ScriptedLLMClient:
    def __init__(self, script: Mapping[str, Sequence[ScriptItem]], *, repeat_last: bool = True):
        self._script = {k: [_as_text(i) for i in v] for k, v in script.items()}
        self._repeat_last = repeat_last
        self._cursor: dict[tuple[str, str], int] = defaultdict(int)
        self._lock = threading.Lock()
        self.calls: list[LLMRequest] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        purpose = request.tags.purpose
        with self._lock:
            self.calls.append(request)
            responses = self._script.get(purpose)
            if not responses:
                raise ScriptExhaustedError(f"no scripted response for {purpose!r}")
            key = (request.tags.run_id, purpose)
            idx = self._cursor[key]
            if idx >= len(responses):
                if not self._repeat_last:
                    raise ScriptExhaustedError(f"script for {purpose!r} exhausted")
                idx = len(responses) - 1
            self._cursor[key] += 1
        text = responses[idx]
        return LLMResponse(
            text=text,
            usage=_usage_for(request, text),
            model_id=request.model_id,
            stop_reason="end_turn",
            latency_ms=0.0,
        )


class FaultInjectingLLMClient:
    """Replaces specific calls with a known-bad response (spec Section 7 anti-pattern check).

    ``injections`` maps ``(purpose, attempt)`` to the response to return instead
    of calling the wrapped client.
    """

    def __init__(self, inner: LLMClient, injections: Mapping[tuple[str, int], ScriptItem]) -> None:
        self._inner = inner
        self._injections = {k: _as_text(v) for k, v in injections.items()}
        self.injected: list[tuple[str, int]] = []

    def complete(self, request: LLMRequest) -> LLMResponse:
        key = (request.tags.purpose, request.tags.attempt)
        text = self._injections.get(key)
        if text is None:
            return self._inner.complete(request)
        self.injected.append(key)
        return LLMResponse(
            text=text,
            usage=_usage_for(request, text),
            model_id=request.model_id,
            stop_reason="end_turn",
            latency_ms=0.0,
        )
