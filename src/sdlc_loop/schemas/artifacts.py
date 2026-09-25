"""SDLC handoff artifacts (spec Section 3).

Deliberate deviations from the spec's sketch, all in service of the Claude
structured-outputs API (which requires ``additionalProperties: false`` and so
rejects free-form ``dict`` fields) and of traceability:

* ``dict``/``list[dict]`` fields became typed sub-models (``Component``, ``Risk``,
  ``CodeFile``, ``TraceLink`` …).
* Acceptance criteria carry IDs so QA test cases can reference them exactly.
* ``TestPlan.execution`` is filled in by the harness from a real code run, never
  by the model — QA cannot claim a pass it did not observe.

Field descriptions end up in the JSON schema the model sees, so they double as
field-level instructions without editing the persona prompts.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from sdlc_loop.schemas.common import StrictModel


class ProblemBrief(StrictModel):
    objective: str = Field(description="The business objective restated in one sentence.")
    business_goals: list[str] = Field(
        min_length=1, description="3-5 measurable business goals. Referenced by index (0-based)."
    )
    success_metrics: list[str] = Field(
        min_length=1, description="2-4 trackable metrics with a number and a unit."
    )
    constraints: list[str] = Field(
        min_length=1,
        description="Every stated or implied constraint, legacy/vendor limitations first. "
        "Never soften or drop one.",
    )
    out_of_scope: list[str]
    stakeholders: list[str]


class AcceptanceCriterion(StrictModel):
    id: str = Field(description="Unique id of the form '<story_id>-AC<n>', e.g. 'US-1-AC2'.")
    description: str = Field(description="A concrete, testable condition.")


class UserStory(StrictModel):
    id: str = Field(description="Unique id of the form 'US-<n>'.")
    as_a: str
    i_want: str
    so_that: str
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)


class TraceLink(StrictModel):
    story_id: str
    business_goal_indices: list[int] = Field(
        min_length=1, description="0-based indices into ProblemBrief.business_goals."
    )


class RequirementsPackage(StrictModel):
    user_stories: list[UserStory] = Field(min_length=1)
    non_functional_requirements: list[str]
    open_questions: list[str]
    traceability: list[TraceLink] = Field(description="One entry per user story.")

    @model_validator(mode="after")
    def _unique_ids(self) -> RequirementsPackage:
        story_ids = [s.id for s in self.user_stories]
        if len(story_ids) != len(set(story_ids)):
            raise ValueError("user story ids must be unique")
        ac_ids = [ac.id for s in self.user_stories for ac in s.acceptance_criteria]
        if len(ac_ids) != len(set(ac_ids)):
            raise ValueError("acceptance criterion ids must be unique")
        return self

    def acceptance_criterion_ids(self) -> set[str]:
        return {ac.id for s in self.user_stories for ac in s.acceptance_criteria}


class Component(StrictModel):
    name: str
    responsibility: str


class Risk(StrictModel):
    description: str
    mitigation: str = Field(description="A concrete mitigation. A risk without one is incomplete.")


class ArchitectureDecisionRecord(StrictModel):
    title: str
    decision: str
    alternatives_considered: list[str] = Field(min_length=1)
    rationale: str


class TechnicalDesign(StrictModel):
    components: list[Component] = Field(min_length=1)
    integration_approach: str = Field(
        description="Must name exactly how each legacy/vendor system from the constraints is "
        "integrated (adapter, batch export polling, strangler fig, queued writes …)."
    )
    data_flow: str
    risks: list[Risk] = Field(min_length=1)
    adrs: list[ArchitectureDecisionRecord] = Field(min_length=1)


class CodeFile(StrictModel):
    path: str = Field(description="Relative POSIX path, e.g. 'fraud/adapter.py'.")
    language: str = Field(description="e.g. 'python'.")
    content: str


class StubbedStory(StrictModel):
    story_id: str
    plan: str = Field(description="What would be implemented and how it maps to the design.")


class Implementation(StrictModel):
    files: list[CodeFile] = Field(min_length=1)
    implemented_story_ids: list[str] = Field(
        min_length=1, description="The 1-2 stories actually implemented."
    )
    stubbed_stories: list[StubbedStory]
    deviations: list[str] = Field(description="Every deviation from the design, stated explicitly.")
    summary: str


class TestCase(StrictModel):
    __test__ = False  # not a pytest test class
    id: str = Field(description="Unique id of the form 'TC-<n>'.")
    story_id: str
    acceptance_criterion_ids: list[str] = Field(min_length=1)
    type: Literal["unit", "integration", "e2e"]
    steps: list[str] = Field(min_length=1)
    expected_result: str


class ExecutionReport(StrictModel):
    """Produced by :mod:`sdlc_loop.tools.code_verifier`, never by the model."""

    files_checked: int
    syntax_errors: list[str]
    tests_executed: bool
    tests_passed: int
    tests_failed: int
    skipped_reason: str | None
    output_tail: str


class TestPlanDraft(StrictModel):
    """What the QA model is asked to produce."""

    __test__ = False  # not a pytest test class

    test_cases: list[TestCase] = Field(min_length=1)
    coverage_summary: str
    gaps: list[str]
    untestable_criteria: list[str] = Field(
        description="Acceptance criterion ids that cannot be tested given what was implemented."
    )


class TestPlan(TestPlanDraft):
    """The QA draft plus the harness-attached, measured execution report."""

    __test__ = False  # not a pytest test class

    execution: ExecutionReport | None = None
