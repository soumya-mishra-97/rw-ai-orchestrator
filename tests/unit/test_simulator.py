"""The demo-mode simulator: schema-valid output, real gate behaviour, honest labelling."""

from __future__ import annotations

import pytest

from sdlc_loop.demo.simulator import (
    SIMULATOR_MODEL,
    SimulatedLLMClient,
    subject_of,
)
from sdlc_loop.llm.types import CallTags, LLMRequest, SystemBlock
from sdlc_loop.schemas import JudgeOutput, Persona, ProblemBrief


@pytest.mark.parametrize(
    ("requirement", "subject"),
    [
        ("Build a Leave Management System", "Leave Management System"),
        ("please create an invoice approval workflow.", "Invoice approval workflow"),
        ("Evaluator Rejects PM Output (Retry Logic)", "Evaluator Rejects PM Output (Retry Logic)"),
    ],
)
def test_subject_extraction(requirement: str, subject: str) -> None:
    assert subject_of(requirement) == subject


def _request(persona: Persona, content: str, attempt: int = 1) -> LLMRequest:
    return LLMRequest(
        model_id="claude-haiku-4-5-20251001",
        system=(SystemBlock("system"),),
        user_content=content,
        output_schema=ProblemBrief,
        max_tokens=1000,
        tags=CallTags(run_id="r1", persona=persona, purpose="x", attempt=attempt),
    )


def test_pm_first_draft_is_rejected_and_retry_passes() -> None:
    client = SimulatedLLMClient()
    tag = "untrusted_business_request"
    request = f"<{tag}>\nBuild a Leave Management System\n</{tag}>"
    draft = client.complete(_request(Persona.PM, request))
    revised = client.complete(_request(Persona.PM, request, attempt=2))
    assert draft.model_id == SIMULATOR_MODEL  # never passed off as Claude output

    def judged(text: str) -> JudgeOutput:
        content = (
            "<handoff_under_review>\nPM→BA\n</handoff_under_review>\n\n"
            f"<artifact_under_review>\n{text}\n</artifact_under_review>"
        )
        return JudgeOutput.model_validate_json(
            client.complete(_request(Persona.EVALUATOR, content)).text
        )

    first, second = judged(draft.text), judged(revised.text)
    assert first.dimension_scores.mean == 3.8 and first.blocking_issues
    assert second.dimension_scores.mean == 4.6 and not second.blocking_issues
    assert ProblemBrief.model_validate_json(revised.text).objective
