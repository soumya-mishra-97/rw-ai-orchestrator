"""The fixed stage order of the pipeline — the single source of truth the graph
builder, routing and metrics all derive from."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sdlc_loop.schemas.common import Handoff, Persona

ArtifactKey = Literal["problem_brief", "requirements", "design", "implementation", "test_plan"]


@dataclass(frozen=True, slots=True)
class Stage:
    persona: Persona
    artifact_key: ArtifactKey
    handoff: Handoff | None  # evaluator gate that follows this stage

    @property
    def key(self) -> str:
        """Bookkeeping key for retry counters etc."""
        return self.handoff.value if self.handoff else self.persona.value

    @property
    def node(self) -> str:
        return self.persona.value

    @property
    def gate_node(self) -> str:
        if self.handoff is None:
            raise ValueError(f"stage {self.persona} has no gate")
        return f"gate_{self.handoff.value}"


STAGES: tuple[Stage, ...] = (
    Stage(Persona.PM, "problem_brief", Handoff.PM_TO_BA),
    Stage(Persona.BA, "requirements", Handoff.BA_TO_ARCHITECT),
    Stage(Persona.ARCHITECT, "design", Handoff.ARCHITECT_TO_DEV),
    Stage(Persona.DEV, "implementation", Handoff.DEV_TO_QA),
    Stage(Persona.QA, "test_plan", None),
)

HUMAN_REVIEW_NODE = "human_review"
FINALIZE_NODE = "finalize"


def stage_for_handoff(handoff: Handoff) -> Stage:
    return next(s for s in STAGES if s.handoff is handoff)


def next_stage(stage: Stage) -> Stage | None:
    idx = STAGES.index(stage)
    return STAGES[idx + 1] if idx + 1 < len(STAGES) else None
