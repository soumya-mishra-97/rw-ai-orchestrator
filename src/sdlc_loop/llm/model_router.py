"""Model routing by task risk and retry state (spec Section 4).

| Persona   | Default | Escalates to Tier 2 when                    |
|-----------|---------|---------------------------------------------|
| PM, BA, QA| Tier 1  | final attempt, or after a critical flag     |
| Architect | Tier 2  | always — design mistakes are the costliest  |
| Dev       | Tier 2  | always — same reasoning                     |
| Evaluator | Tier 1  | borderline score / before a human escalation|
"""

from __future__ import annotations

from dataclasses import dataclass

from sdlc_loop.config import RoutingPolicy, Settings
from sdlc_loop.llm.models import ModelSpec, get_model_spec
from sdlc_loop.schemas.common import ModelTier, Persona

DEFAULT_TIERS: dict[Persona, ModelTier] = {
    Persona.ORCHESTRATOR: ModelTier.TIER1,  # requirement triage is a cheap classification
    Persona.PM: ModelTier.TIER1,
    Persona.BA: ModelTier.TIER1,
    Persona.ARCHITECT: ModelTier.TIER2,
    Persona.DEV: ModelTier.TIER2,
    Persona.QA: ModelTier.TIER1,
    Persona.EVALUATOR: ModelTier.TIER1,
}


@dataclass(frozen=True, slots=True)
class RouteDecision:
    spec: ModelSpec
    tier: ModelTier
    reason: str


class ModelRouter:
    def __init__(self, settings: Settings) -> None:
        self._tier1 = get_model_spec(settings.tier1_model)
        self._tier2 = get_model_spec(settings.tier2_model)
        self._policy = settings.routing_policy
        self._max_attempts = settings.max_attempts

    @property
    def tier1(self) -> ModelSpec:
        return self._tier1

    @property
    def tier2(self) -> ModelSpec:
        return self._tier2

    def spec_for(self, tier: ModelTier) -> ModelSpec:
        return self._tier2 if tier is ModelTier.TIER2 else self._tier1

    def for_persona(self, persona: Persona, attempt: int, *, force_tier2: bool) -> RouteDecision:
        if self._policy is RoutingPolicy.ALL_TIER2:
            return RouteDecision(self._tier2, ModelTier.TIER2, "baseline: all calls on tier 2")
        default = DEFAULT_TIERS[persona]
        if default is ModelTier.TIER2:
            return RouteDecision(self._tier2, ModelTier.TIER2, "high-risk persona: always tier 2")
        if force_tier2:
            return RouteDecision(self._tier2, ModelTier.TIER2, "escalated after critical flag")
        if attempt >= self._max_attempts:
            return RouteDecision(self._tier2, ModelTier.TIER2, "final attempt: escalated to tier 2")
        return RouteDecision(self._tier1, ModelTier.TIER1, "default tier 1")

    def for_judge(self, *, confirm: bool) -> RouteDecision:
        if self._policy is RoutingPolicy.ALL_TIER2:
            return RouteDecision(self._tier2, ModelTier.TIER2, "baseline: all calls on tier 2")
        if confirm:
            return RouteDecision(self._tier2, ModelTier.TIER2, "judge confirmation")
        return RouteDecision(self._tier1, ModelTier.TIER1, "judge first pass")
