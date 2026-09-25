"""Prompt-cache layout.

Render order on the API is ``tools → system → messages`` and caching is a
byte-exact prefix match, so the layout is:

1. shared preamble + rubric — identical on every call, breakpoint #1
2. persona system prompt — identical for every call of that persona, breakpoint #2
3. user message — the per-call artifacts (never cached)

Nothing volatile (timestamps, run ids, attempt numbers) is allowed above the
user message; those go in ``CallTags`` which never reach the prompt.
"""

from __future__ import annotations

from dataclasses import dataclass

from sdlc_loop.llm.models import ModelSpec
from sdlc_loop.llm.token_meter import estimate_tokens
from sdlc_loop.llm.types import SystemBlock
from sdlc_loop.prompts import shared_preamble


@dataclass(frozen=True, slots=True)
class CachePlan:
    blocks: tuple[SystemBlock, ...]
    eligible: bool  # prefix is at least the model's minimum cacheable size


class CachePolicy:
    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled

    def plan(self, persona_prompt: str, spec: ModelSpec) -> CachePlan:
        preamble = shared_preamble()
        prefix_tokens = estimate_tokens(preamble) + estimate_tokens(persona_prompt)
        eligible = prefix_tokens >= spec.cache_min_tokens
        # Marking a too-short prefix is harmless (the API just does not cache it and
        # charges no write premium), so we mark whenever caching is enabled and let
        # measured cache_read_input_tokens tell the truth.
        mark = self._enabled
        return CachePlan(
            blocks=(SystemBlock(preamble, cache=mark), SystemBlock(persona_prompt, cache=mark)),
            eligible=eligible,
        )
