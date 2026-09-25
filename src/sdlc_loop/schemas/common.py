"""Shared enums and the strict base model used for every boundary object."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Immutable, closed-world model.

    ``extra="forbid"`` doubles as the ``additionalProperties: false`` that the
    Claude structured-outputs API requires on every object.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class Persona(StrEnum):
    ORCHESTRATOR = "orchestrator"
    PM = "pm"
    BA = "ba"
    ARCHITECT = "architect"
    DEV = "dev"
    QA = "qa"
    EVALUATOR = "evaluator"


class Handoff(StrEnum):
    PM_TO_BA = "pm_to_ba"
    BA_TO_ARCHITECT = "ba_to_architect"
    ARCHITECT_TO_DEV = "architect_to_dev"
    DEV_TO_QA = "dev_to_qa"

    @property
    def label(self) -> str:
        return {
            Handoff.PM_TO_BA: "PM→BA",
            Handoff.BA_TO_ARCHITECT: "BA→Architect",
            Handoff.ARCHITECT_TO_DEV: "Architect→Dev",
            Handoff.DEV_TO_QA: "Dev→QA",
        }[self]


class ModelTier(StrEnum):
    TIER1 = "tier1"
    TIER2 = "tier2"


class RunStatus(StrEnum):
    RUNNING = "running"
    BLOCKED_FOR_HUMAN = "blocked_for_human"
    COMPLETE = "complete"
    ABORTED = "aborted"
    FAILED = "failed"
