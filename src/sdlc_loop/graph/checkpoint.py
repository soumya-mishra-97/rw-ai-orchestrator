"""Checkpointer wiring.

``interrupt()`` requires a checkpointer. SQLite is right for a single-node
exercise; production would use ``PostgresSaver`` (same interface).

The msgpack serializer is given an explicit allowlist: only our own schema
classes can be reconstructed from a checkpoint, so a tampered checkpoint cannot
instantiate arbitrary types.
"""

from __future__ import annotations

import sqlite3
from enum import Enum
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel

from sdlc_loop.schemas import artifacts, common, evaluation, review

_STATE_TYPES: tuple[type[BaseModel] | type[Enum], ...] = (
    artifacts.ProblemBrief,
    artifacts.AcceptanceCriterion,
    artifacts.UserStory,
    artifacts.TraceLink,
    artifacts.RequirementsPackage,
    artifacts.Component,
    artifacts.Risk,
    artifacts.ArchitectureDecisionRecord,
    artifacts.TechnicalDesign,
    artifacts.CodeFile,
    artifacts.StubbedStory,
    artifacts.Implementation,
    artifacts.TestCase,
    artifacts.ExecutionReport,
    artifacts.TestPlan,
    evaluation.DimensionScores,
    evaluation.EvaluationRecord,
    evaluation.Verdict,
    review.HumanDecision,
    common.Handoff,
    common.Persona,
    common.RunStatus,
)


def make_serializer() -> JsonPlusSerializer:
    allowed = [(t.__module__, t.__name__) for t in _STATE_TYPES]
    return JsonPlusSerializer(allowed_msgpack_modules=allowed)


def open_checkpointer(path: Path | str) -> SqliteSaver:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    return SqliteSaver(conn, serde=make_serializer())
