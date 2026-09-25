"""Human-in-the-loop review contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from sdlc_loop.schemas.common import Handoff, Persona, StrictModel

ReviewAction = Literal["accept_as_is", "retry", "abort"]


class HumanDecision(StrictModel):
    action: ReviewAction
    guidance: str | None = Field(default=None, description="Required when action == 'retry'.")
    reviewer: str = "unknown"

    @model_validator(mode="after")
    def _guidance_for_retry(self) -> HumanDecision:
        if self.action == "retry" and not (self.guidance and self.guidance.strip()):
            raise ValueError("guidance is required when action is 'retry'")
        return self


class ReviewRequest(StrictModel):
    """Payload surfaced to the reviewer by ``interrupt()``."""

    handoff: Handoff
    persona: Persona
    artifact: dict[str, Any]
    evaluator_feedback: str
    blocking_issues: list[str]
    overall_score: float | None
    critical_flag: bool
    attempts_so_far: int
