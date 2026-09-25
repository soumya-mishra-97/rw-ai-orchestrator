"""Evaluator rubric and verdict records (spec Sections 5 and 6.6).

The judge model only returns :class:`JudgeOutput` (five integer scores plus
feedback). ``overall_score``, ``critical_flag`` and the verdict are computed in
code (see :mod:`sdlc_loop.graph.routing`) — we never trust model arithmetic for a
gate decision.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field

from sdlc_loop.schemas.common import Handoff, StrictModel


class Dimension(StrEnum):
    COMPLETENESS = "completeness"
    CORRECTNESS_FEASIBILITY = "correctness_feasibility"
    LEGACY_CONSTRAINT_AWARENESS = "legacy_constraint_awareness"
    CLARITY_ACTIONABILITY = "clarity_actionability"
    TRACEABILITY = "traceability"

    @property
    def label(self) -> str:
        return {
            Dimension.COMPLETENESS: "Completeness",
            Dimension.CORRECTNESS_FEASIBILITY: "Correctness & feasibility",
            Dimension.LEGACY_CONSTRAINT_AWARENESS: "Constraint awareness",
            Dimension.CLARITY_ACTIONABILITY: "Clarity & actionability",
            Dimension.TRACEABILITY: "Traceability",
        }[self]


_SCORE = Field(ge=1, le=5)


class DimensionScores(StrictModel):
    completeness: int = _SCORE
    correctness_feasibility: int = _SCORE
    legacy_constraint_awareness: int = _SCORE
    clarity_actionability: int = _SCORE
    traceability: int = _SCORE

    def as_dict(self) -> dict[str, int]:
        return {d.value: int(getattr(self, d.value)) for d in Dimension}

    @property
    def mean(self) -> float:
        values = list(self.as_dict().values())
        return round(sum(values) / len(values), 2)

    @property
    def minimum(self) -> int:
        return min(self.as_dict().values())


class JudgeOutput(StrictModel):
    """What the evaluator model returns."""

    dimension_scores: DimensionScores
    blocking_issues: list[str] = Field(
        description="Each concrete defect that must be fixed, one per item. Empty if none."
    )
    feedback: str = Field(
        description="Specific, actionable instructions for the persona's retry. "
        "Name exactly what is missing or wrong."
    )


class Verdict(StrEnum):
    PASS = "pass"  # noqa: S105 - not a password
    RETRY = "retry"
    ESCALATE = "escalate"


JudgeSource = Literal["deterministic", "tier1", "tier2"]


class EvaluationRecord(StrictModel):
    handoff: Handoff
    attempt_number: int
    dimension_scores: DimensionScores | None  # None when the deterministic pre-gate rejected
    overall_score: float | None
    critical_flag: bool
    verdict: Verdict
    feedback: str
    blocking_issues: list[str] = Field(default_factory=list)
    judge: JudgeSource
    model_used: str | None
    tier1_overall_score: float | None = None  # set when a Tier 2 judge re-scored
    judge_escalated: bool = False
