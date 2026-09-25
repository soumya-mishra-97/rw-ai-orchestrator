"""Transport-level contracts between agents and whichever LLM backend is wired in.

Agents depend only on :class:`LLMClient` (a ``Protocol``), so tests and the
offline demo swap in :class:`~sdlc_loop.llm.fake_client.ScriptedLLMClient`
without touching agent code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from sdlc_loop.schemas.common import Persona, StrictModel


@dataclass(frozen=True, slots=True)
class SystemBlock:
    text: str
    cache: bool = False  # place a cache_control breakpoint at the end of this block


@dataclass(frozen=True, slots=True)
class CallTags:
    """Who is calling and why — used for routing fixtures, audit and metrics."""

    run_id: str
    persona: Persona
    purpose: str  # e.g. "persona:architect", "judge:architect_to_dev:tier1"
    attempt: int = 1
    handoff: str | None = None


@dataclass(frozen=True, slots=True)
class LLMRequest:
    model_id: str
    system: tuple[SystemBlock, ...]
    user_content: str
    output_schema: type[BaseModel]
    max_tokens: int
    tags: CallTags
    effort: str | None = None


class TokenUsage(StrictModel):
    input_tokens: int = 0  # uncached input
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens
            + other.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
        )


class LLMResponse(StrictModel):
    text: str
    usage: TokenUsage
    model_id: str
    stop_reason: str | None
    latency_ms: float
    batched: bool = False
    request_id: str | None = None


class LLMError(RuntimeError):
    """Non-retryable failure talking to the model (after SDK retries)."""


class LLMRefusalError(LLMError):
    pass


@runtime_checkable
class LLMClient(Protocol):
    def complete(self, request: LLMRequest) -> LLMResponse: ...
