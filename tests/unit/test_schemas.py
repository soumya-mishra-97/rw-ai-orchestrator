from __future__ import annotations

from typing import Any

import pytest
from anthropic import transform_schema
from pydantic import BaseModel, ValidationError

from sdlc_loop.schemas import (
    HumanDecision,
    Implementation,
    JudgeOutput,
    ProblemBrief,
    RequirementsPackage,
    TechnicalDesign,
    TestPlanDraft,
)

OUTPUT_MODELS: list[type[BaseModel]] = [
    ProblemBrief,
    RequirementsPackage,
    TechnicalDesign,
    Implementation,
    TestPlanDraft,
    JudgeOutput,
]


def _walk(node: Any) -> list[dict[str, Any]]:
    found = []
    if isinstance(node, dict):
        found.append(node)
        for v in node.values():
            found += _walk(v)
    elif isinstance(node, list):
        for v in node:
            found += _walk(v)
    return found


@pytest.mark.parametrize("model", OUTPUT_MODELS, ids=lambda m: m.__name__)
def test_output_schemas_are_structured_output_compatible(model: type[BaseModel]) -> None:
    """Every object must be closed and free of constraints the API rejects."""
    schema = transform_schema(model)
    for node in _walk(schema):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, node
        for banned in ("minimum", "maximum", "minLength", "maxItems"):
            assert banned not in node, (banned, node)
        # the API accepts minItems only as 0 or 1 (the SDK moves anything else to the description)
        assert node.get("minItems", 0) in (0, 1), node


def test_fixture_artifacts_validate(script: dict[str, list[Any]]) -> None:
    ProblemBrief.model_validate(script["persona:pm"][0])
    RequirementsPackage.model_validate(script["persona:ba"][0])
    for design in script["persona:architect"]:
        TechnicalDesign.model_validate(design)
    Implementation.model_validate(script["persona:dev"][0])
    TestPlanDraft.model_validate(script["persona:qa"][0])


def test_duplicate_story_ids_rejected(script: dict[str, list[Any]]) -> None:
    reqs = script["persona:ba"][0]
    reqs["user_stories"][1]["id"] = reqs["user_stories"][0]["id"]
    with pytest.raises(ValidationError, match="unique"):
        RequirementsPackage.model_validate(reqs)


def test_extra_fields_forbidden(script: dict[str, list[Any]]) -> None:
    brief = script["persona:pm"][0] | {"approved_by_model": True}
    with pytest.raises(ValidationError):
        ProblemBrief.model_validate(brief)


def test_judge_scores_out_of_range_rejected() -> None:
    bad = {
        "dimension_scores": {
            "completeness": 6,
            "correctness_feasibility": 4,
            "legacy_constraint_awareness": 4,
            "clarity_actionability": 4,
            "traceability": 0,
        },
        "blocking_issues": [],
        "feedback": "x",
    }
    with pytest.raises(ValidationError):
        JudgeOutput.model_validate(bad)


def test_human_retry_requires_guidance() -> None:
    with pytest.raises(ValidationError):
        HumanDecision(action="retry")
    assert HumanDecision(action="retry", guidance="use the batch extract").guidance
