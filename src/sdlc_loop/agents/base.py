"""Shared machinery for every agent: prompt assembly, the structured call with
schema validation and parse repair, and per-call records for audit/metrics."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar

from pydantic import BaseModel, ValidationError

from sdlc_loop.agents.context import (
    ContextSection,
    full_history_sections,
    render_sections,
    retry_sections,
)
from sdlc_loop.config import ContextPolicy, Settings
from sdlc_loop.governance.hashing import hash_payload
from sdlc_loop.graph.state import SDLCState
from sdlc_loop.llm.cache import CachePolicy
from sdlc_loop.llm.model_router import ModelRouter, RouteDecision
from sdlc_loop.llm.token_meter import estimate_tokens
from sdlc_loop.llm.types import CallTags, LLMClient, LLMRequest, LLMResponse
from sdlc_loop.prompts import persona_prompt
from sdlc_loop.schemas.common import Persona


@dataclass(frozen=True, slots=True)
class AgentDeps:
    llm: LLMClient
    router: ModelRouter
    cache: CachePolicy
    settings: Settings


@dataclass(slots=True)
class CallRecord:
    """One LLM round-trip, as recorded in the audit log."""

    tags: CallTags
    route: RouteDecision
    response: LLMResponse
    input_hash: str
    parse_error: str | None
    minimal_context_tokens: int  # estimate of the context actually sent (user message)
    naive_context_tokens: int  # estimate of the full-history counterfactual
    cache_eligible: bool

    @property
    def ok(self) -> bool:
        return self.parse_error is None


@dataclass(slots=True)
class CallOutcome[T: BaseModel]:
    value: T | None
    records: list[CallRecord] = field(default_factory=list)
    error: str | None = None


def call_structured[T: BaseModel](
    deps: AgentDeps,
    *,
    schema: type[T],
    system_prompt: str,
    user_content: str,
    route: RouteDecision,
    tags: CallTags,
    max_tokens: int,
    naive_context_tokens: int,
) -> CallOutcome[T]:
    plan = deps.cache.plan(system_prompt, route.spec)
    outcome: CallOutcome[T] = CallOutcome(value=None)
    content = user_content
    for _ in range(deps.settings.parse_repair_attempts + 1):
        request = LLMRequest(
            model_id=route.spec.model_id,
            system=plan.blocks,
            user_content=content,
            output_schema=schema,
            max_tokens=max_tokens,
            tags=tags,
            effort=deps.settings.tier2_effort if route.spec.supports_effort else None,
        )
        response = deps.llm.complete(request)
        error: str | None = None
        value: T | None = None
        try:
            value = schema.model_validate_json(response.text)
        except ValidationError as exc:
            error = f"{exc.error_count()} validation error(s): {exc.errors()[:5]}"
            if response.stop_reason == "max_tokens":
                error = f"output truncated at max_tokens; {error}"
        outcome.records.append(
            CallRecord(
                tags=tags,
                route=route,
                response=response,
                input_hash=hash_payload(
                    [route.spec.model_id, [b.text for b in plan.blocks], content]
                ),
                parse_error=error,
                minimal_context_tokens=estimate_tokens(user_content),
                naive_context_tokens=naive_context_tokens,
                cache_eligible=plan.eligible,
            )
        )
        if value is not None:
            outcome.value = value
            outcome.error = None
            return outcome
        outcome.error = error
        content = (
            f"{user_content}\n\n<schema_validation_error>\n{error}\n</schema_validation_error>\n"
            "Your previous output did not validate against the schema. Return a corrected "
            "JSON object."
        )
    return outcome


@dataclass(slots=True)
class PersonaResult:
    artifact: BaseModel | None
    records: list[CallRecord]
    route: RouteDecision
    error: str | None = None
    measured: BaseModel | None = None  # non-LLM evidence gathered by ``prepare``


@dataclass(frozen=True, slots=True)
class RetryContext:
    attempt: int = 1
    force_tier2: bool = False
    feedback: tuple[str, ...] = ()
    human_guidance: str | None = None
    previous: BaseModel | None = None


class PersonaAgent(ABC):
    persona: ClassVar[Persona]
    output_model: ClassVar[type[BaseModel]]
    max_tokens: ClassVar[int] = 8000

    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    @abstractmethod
    def context_sections(self, state: SDLCState) -> list[ContextSection]:
        """The *minimal* upstream context this persona needs."""

    def prepare(self, state: SDLCState) -> tuple[list[ContextSection], BaseModel | None]:
        """Hook for measured, non-LLM inputs (e.g. QA's code execution report)."""
        return [], None

    def finalize(self, draft: BaseModel, measured: BaseModel | None) -> BaseModel:
        return draft

    def run(self, state: SDLCState, retry: RetryContext) -> PersonaResult:
        route = self.deps.router.for_persona(
            self.persona, retry.attempt, force_tier2=retry.force_tier2
        )
        measured_sections, measured = self.prepare(state)
        if self.deps.settings.context_policy is ContextPolicy.FULL_HISTORY:
            base = full_history_sections(state)
        else:
            base = self.context_sections(state)
        extras = retry_sections(retry.feedback, retry.human_guidance, retry.previous)
        if not self.deps.settings.include_previous_attempt_on_retry:
            extras = [s for s in extras if s.tag != "previous_attempt"]
        user_content = render_sections([*base, *measured_sections, *extras])
        naive = estimate_tokens(
            render_sections([*full_history_sections(state), *measured_sections, *extras])
        )
        outcome = call_structured(
            self.deps,
            schema=self.output_model,
            system_prompt=persona_prompt(self.persona),
            user_content=user_content,
            route=route,
            tags=CallTags(
                run_id=state["run_id"],
                persona=self.persona,
                purpose=f"persona:{self.persona.value}",
                attempt=retry.attempt,
            ),
            max_tokens=self.max_tokens,
            naive_context_tokens=naive,
        )
        artifact = self.finalize(outcome.value, measured) if outcome.value is not None else None
        return PersonaResult(
            artifact=artifact,
            records=outcome.records,
            route=route,
            error=outcome.error,
            measured=measured,
        )
