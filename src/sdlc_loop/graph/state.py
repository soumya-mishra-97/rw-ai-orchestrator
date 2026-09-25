"""LangGraph state for one SDLC run.

Artifacts are stored as Pydantic models; the checkpointer's msgpack serializer
is given an explicit allowlist of these classes (see ``graph/checkpoint.py``) so
nothing else can be deserialised from a checkpoint.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from sdlc_loop.schemas.artifacts import (
    Implementation,
    ProblemBrief,
    RequirementsPackage,
    TechnicalDesign,
    TestPlan,
)
from sdlc_loop.schemas.common import Handoff, RunStatus
from sdlc_loop.schemas.evaluation import EvaluationRecord
from sdlc_loop.schemas.review import HumanDecision


class SDLCState(TypedDict, total=False):
    run_id: str
    scenario_id: str | None
    request: str  # already PII-redacted

    problem_brief: ProblemBrief | None
    requirements: RequirementsPackage | None
    design: TechnicalDesign | None
    implementation: Implementation | None
    test_plan: TestPlan | None

    evaluations: Annotated[list[EvaluationRecord], operator.add]
    human_decisions: Annotated[list[HumanDecision], operator.add]

    # Reliability-engine bookkeeping, keyed by stage key (handoff value or "qa").
    retry_counts: dict[str, int]
    tier2_forced: dict[str, bool]
    critical_retry_used: dict[str, bool]
    feedback_history: dict[str, list[str]]
    human_guidance: dict[str, str]
    invalid_output: dict[
        str, str
    ]  # stage key -> schema error when a persona produced nothing usable

    pending_handoff: Handoff | None
    last_verdict: str | None  # Verdict value or "human_accept" / "human_retry" / "human_abort"
    status: RunStatus
