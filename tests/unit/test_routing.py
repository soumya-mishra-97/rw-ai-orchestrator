"""The retry / escalate state machine — where real reliability bugs hide."""

from __future__ import annotations

import pytest

from sdlc_loop.config import CriticalPolicy
from sdlc_loop.graph.routing import (
    END_NODE,
    GatePolicy,
    GateState,
    decide_verdict,
    decide_without_scores,
    is_critical,
    needs_tier2_confirmation,
    passes,
    route_after_gate,
    route_after_review,
)
from sdlc_loop.graph.stages import FINALIZE_NODE, HUMAN_REVIEW_NODE, STAGES
from sdlc_loop.schemas import DimensionScores, Verdict

POLICY = GatePolicy()
STRICT = GatePolicy(critical_policy=CriticalPolicy.STRICT)


def s(*v: int) -> DimensionScores:
    keys = DimensionScores.model_fields
    return DimensionScores(**dict(zip(keys, v, strict=True)))


@pytest.mark.parametrize(
    ("scores", "gate", "policy", "expected"),
    [
        # PASS needs mean >= 4.0, every dimension >= 3, no 1s
        (s(4, 4, 4, 4, 4), GateState(), POLICY, Verdict.PASS),
        (s(5, 5, 5, 5, 2), GateState(), POLICY, Verdict.RETRY),  # mean 4.4 but a 2
        (s(4, 4, 4, 4, 3), GateState(), POLICY, Verdict.RETRY),  # mean 3.8
        (s(4, 4, 4, 4, 3), GateState(retries_used=1), POLICY, Verdict.RETRY),
        (s(4, 4, 4, 4, 3), GateState(retries_used=2), POLICY, Verdict.ESCALATE),  # exhausted
        # critical: fast-track retry once, then escalate
        (s(5, 5, 1, 5, 5), GateState(), POLICY, Verdict.RETRY),
        (
            s(5, 5, 1, 5, 5),
            GateState(retries_used=1, critical_retry_used=True),
            POLICY,
            Verdict.ESCALATE,
        ),
        (s(5, 5, 1, 5, 5), GateState(retries_used=2), POLICY, Verdict.ESCALATE),
        # strict mode (spec Section 5 verbatim): critical escalates immediately
        (s(5, 5, 1, 5, 5), GateState(), STRICT, Verdict.ESCALATE),
        (s(4, 4, 4, 4, 3), GateState(), STRICT, Verdict.RETRY),
    ],
)
def test_decide_verdict(
    scores: DimensionScores, gate: GateState, policy: GatePolicy, expected: Verdict
) -> None:
    assert decide_verdict(scores, gate, policy) is expected


def test_mean_and_critical() -> None:
    scores = s(5, 4, 1, 4, 5)
    assert scores.mean == 3.8
    assert scores.minimum == 1
    assert is_critical(scores)
    assert not passes(s(5, 5, 5, 5, 1), POLICY)  # high mean never hides a 1


def test_without_scores_retries_then_escalates() -> None:
    assert decide_without_scores(GateState(retries_used=0), POLICY) is Verdict.RETRY
    assert decide_without_scores(GateState(retries_used=2), POLICY) is Verdict.ESCALATE


@pytest.mark.parametrize(
    ("scores", "verdict", "expected"),
    [
        (s(4, 4, 3, 3, 3), Verdict.RETRY, True),  # 3.4 borderline
        (s(3, 3, 3, 3, 3), Verdict.RETRY, True),  # 3.0 is inside the band
        (s(2, 2, 2, 2, 2), Verdict.RETRY, False),  # clearly bad, cheap judge is enough
        (s(5, 5, 5, 4, 4), Verdict.PASS, False),  # clearly good
        (s(2, 2, 2, 2, 2), Verdict.ESCALATE, True),  # confirm before paging a human
    ],
)
def test_tier2_confirmation(scores: DimensionScores, verdict: Verdict, expected: bool) -> None:
    assert needs_tier2_confirmation(scores, verdict, POLICY) is expected


def test_route_after_gate() -> None:
    pm, ba = STAGES[0], STAGES[1]
    assert route_after_gate("pass", pm) == ba.node
    assert route_after_gate("retry", pm) == pm.node
    assert route_after_gate("escalate", pm) == HUMAN_REVIEW_NODE
    dev = STAGES[3]
    assert route_after_gate("pass", dev) == "qa"


def test_route_after_review() -> None:
    arch, dev = STAGES[2], STAGES[3]
    assert route_after_review("accept_as_is", arch) == dev.node
    assert route_after_review("retry", arch) == arch.node
    assert route_after_review("abort", arch) == END_NODE
    assert route_after_review("accept_as_is", STAGES[4]) == FINALIZE_NODE
