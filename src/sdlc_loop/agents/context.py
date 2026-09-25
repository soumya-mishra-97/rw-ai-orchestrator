from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from sdlc_loop.graph.state import SDLCState
from sdlc_loop.schemas.artifacts import RequirementsPackage


@dataclass(frozen=True, slots=True)
class ContextSection:
    tag: str
    content: str


def dump(model: BaseModel) -> str:
    return model.model_dump_json()


def render_sections(sections: Iterable[ContextSection]) -> str:
    return "\n\n".join(f"<{s.tag}>\n{s.content}\n</{s.tag}>" for s in sections)


def request_section(state: SDLCState) -> ContextSection:
    return ContextSection("untrusted_business_request", state["request"])


def constraints_section(state: SDLCState) -> list[ContextSection]:
    brief = state.get("problem_brief")
    if brief is None:
        return []
    return [ContextSection("problem_brief_constraints", json.dumps(brief.constraints))]


def acceptance_criteria_section(req: RequirementsPackage) -> ContextSection:
    stories = [
        {
            "id": s.id,
            "i_want": s.i_want,
            "acceptance_criteria": [ac.model_dump() for ac in s.acceptance_criteria],
        }
        for s in req.user_stories
    ]
    return ContextSection("user_stories_and_acceptance_criteria", json.dumps(stories))


def full_history_sections(state: SDLCState) -> list[ContextSection]:
    """Naive baseline: every artifact and every evaluation produced so far."""
    sections = [request_section(state)]
    for key in ("problem_brief", "requirements", "design", "implementation", "test_plan"):
        value = state.get(key)
        if isinstance(value, BaseModel):
            sections.append(ContextSection(key, dump(value)))
    evaluations = state.get("evaluations") or []
    if evaluations:
        sections.append(
            ContextSection(
                "evaluation_history", json.dumps([e.model_dump(mode="json") for e in evaluations])
            )
        )
    return sections


def retry_sections(
    feedback: Sequence[str], guidance: str | None, previous: BaseModel | None
) -> list[ContextSection]:
    sections: list[ContextSection] = []
    if previous is not None and (feedback or guidance):
        sections.append(ContextSection("previous_attempt", dump(previous)))
    for i, item in enumerate(feedback, start=1):
        sections.append(ContextSection(f"evaluator_feedback_{i}", item))
    if guidance:
        sections.append(ContextSection("human_guidance", guidance))
    return sections
