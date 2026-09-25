"""The reliability engine (spec Section 5) as pure, unit-tested functions.

Retry ladder per handoff (defaults: ``max_retries_per_handoff=2``):

    attempt 1  persona default tier
    attempt 2  same tier, evaluator feedback appended            (retry 1)
    attempt 3  Tier 2, full feedback history appended            (retry 2)
    human      attempt 3 still failing, or a critical flag that survives
               the fast-track Tier 2 retry (or any critical flag in STRICT mode)

Verdict:
    PASS      overall >= 4.0 and min(dimension) >= 3 and not critical
    RETRY     not PASS and retries remain (critical: see CriticalPolicy)
    ESCALATE  otherwise
"""

from __future__ import annotations

from dataclasses import dataclass

from sdlc_loop.config import CriticalPolicy, Settings
from sdlc_loop.graph.stages import FINALIZE_NODE, HUMAN_REVIEW_NODE, Stage, next_stage
from sdlc_loop.schemas.evaluation import DimensionScores, Verdict
from sdlc_loop.schemas.review import ReviewAction

HUMAN_ACCEPT = "human_accept"
HUMAN_RETRY = "human_retry"
HUMAN_ABORT = "human_abort"
END_NODE = "__end__"


@dataclass(frozen=True, slots=True)
class GatePolicy:
    pass_threshold: float = 4.0
    min_dimension_score: int = 3
    borderline_low: float = 3.0
    max_retries: int = 2
    critical_policy: CriticalPolicy = CriticalPolicy.FAST_TRACK

    @classmethod
    def from_settings(cls, settings: Settings) -> GatePolicy:
        return cls(
            pass_threshold=settings.pass_threshold,
            min_dimension_score=settings.min_dimension_score,
            borderline_low=settings.borderline_low,
            max_retries=settings.max_retries_per_handoff,
            critical_policy=settings.critical_policy,
        )


@dataclass(frozen=True, slots=True)
class GateState:
    retries_used: int = 0
    critical_retry_used: bool = False


def is_critical(scores: DimensionScores) -> bool:
    return scores.minimum == 1


def passes(scores: DimensionScores, policy: GatePolicy) -> bool:
    return (
        scores.mean >= policy.pass_threshold
        and scores.minimum >= policy.min_dimension_score
        and not is_critical(scores)
    )


def decide_verdict(scores: DimensionScores, gate: GateState, policy: GatePolicy) -> Verdict:
    if passes(scores, policy):
        return Verdict.PASS
    exhausted = gate.retries_used >= policy.max_retries
    if is_critical(scores):
        if policy.critical_policy is CriticalPolicy.STRICT or gate.critical_retry_used or exhausted:
            return Verdict.ESCALATE
        return Verdict.RETRY
    return Verdict.ESCALATE if exhausted else Verdict.RETRY


def decide_without_scores(gate: GateState, policy: GatePolicy) -> Verdict:
    """Verdict when the deterministic pre-gate (or unparseable output) rejected the artifact."""
    return Verdict.ESCALATE if gate.retries_used >= policy.max_retries else Verdict.RETRY


def needs_tier2_confirmation(
    scores: DimensionScores, tier1_verdict: Verdict, policy: GatePolicy
) -> bool:
    """Re-score on Tier 2 when the cheap judge is borderline or about to page a human."""
    borderline = policy.borderline_low <= scores.mean < policy.pass_threshold
    return borderline or tier1_verdict is Verdict.ESCALATE


def route_after_gate(last_verdict: str | None, stage: Stage) -> str:
    if last_verdict == Verdict.PASS.value:
        nxt = next_stage(stage)
        return nxt.node if nxt else FINALIZE_NODE
    if last_verdict == Verdict.RETRY.value:
        return stage.node
    return HUMAN_REVIEW_NODE


def route_after_review(action: ReviewAction, stage: Stage) -> str:
    if action == "abort":
        return END_NODE
    if action == "retry":
        return stage.node
    nxt = next_stage(stage)
    return nxt.node if nxt else FINALIZE_NODE
