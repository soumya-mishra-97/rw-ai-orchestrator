"""Master Orchestrator: the single entry point between a user's feature requirement
and the multi-agent SDLC pipeline.

Responsibilities, in order:

1. **Validate** the requirement — cheap deterministic rules first (0 tokens), then
   a Tier-1 LLM triage in live mode ("is this a software requirement at all?"),
   so an invalid request never costs a full pipeline run (~$0.10-0.30).
2. **Plan** the workflow — which agent runs at each stage, on which model tier,
   what it receives and produces, and which evaluator gate follows it.
3. **Dispatch** — hand the redacted requirement to the LangGraph state machine,
   which invokes PM → BA → Architect → Developer → QA with an Evaluator gate at
   every handoff. The orchestrator never calls agents directly and never
   decides PASS/RETRY/ESCALATE: that is the evaluator gate's job, in code
   (:mod:`sdlc_loop.graph.routing`). Keeping the coordination deterministic is a
   reliability choice — an LLM "supervisor" choosing the next agent would add
   cost and variance to the one part of the system that must never vary.
4. **Record** every decision (validated, rejected, planned) in the audit log, so
   the browser view and the eval metrics read the same source of truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import Field

from sdlc_loop.agents.base import call_structured
from sdlc_loop.agents.context import ContextSection, render_sections
from sdlc_loop.demo.library import SIMULATOR, DemoExample, DemoLLMClient, RunSource
from sdlc_loop.demo.simulator import subject_of
from sdlc_loop.governance.audit import AuditEvent, AuditEventType
from sdlc_loop.graph.stages import STAGES
from sdlc_loop.graph.telemetry import record_calls
from sdlc_loop.llm.model_router import DEFAULT_TIERS
from sdlc_loop.llm.types import CallTags
from sdlc_loop.prompts import persona_prompt
from sdlc_loop.schemas.common import ModelTier, Persona, StrictModel
from sdlc_loop.services.container import Container
from sdlc_loop.services.run_service import RunService

log = logging.getLogger(__name__)

MIN_CHARS, MAX_CHARS, MIN_WORDS = 12, 4000, 3


class OrchestratorMode(StrEnum):
    LIVE = "live"  # Claude API
    OFFLINE = "offline"  # demo: curated examples, simulator for everything else


@dataclass(frozen=True, slots=True)
class AgentProfile:
    short: str  # initials shown in the UI avatar
    name: str
    description: str
    receives: str
    produces: str


AGENT_PROFILES: dict[Persona, AgentProfile] = {
    Persona.PM: AgentProfile(
        "PM",
        "Product Manager",
        "Turns the raw requirement into goals, metrics and constraints",
        "Feature requirement (PII-redacted)",
        "ProblemBrief",
    ),
    Persona.BA: AgentProfile(
        "BA",
        "Business Analyst",
        "Writes user stories with testable acceptance criteria",
        "ProblemBrief",
        "RequirementsPackage",
    ),
    Persona.ARCHITECT: AgentProfile(
        "AR",
        "Solutions Architect",
        "Designs components, integrations, risks and ADRs",
        "RequirementsPackage + constraints",
        "TechnicalDesign",
    ),
    Persona.DEV: AgentProfile(
        "DEV",
        "Developer",
        "Implements 1-2 stories as runnable code, stubs the rest",
        "TechnicalDesign + stories + constraints",
        "Implementation",
    ),
    Persona.QA: AgentProfile(
        "QA",
        "QA Engineer",
        "Maps test cases to acceptance criteria; attaches the measured test run",
        "Design + Implementation + criteria + measured code run",
        "TestPlan",
    ),
    Persona.EVALUATOR: AgentProfile(
        "EV",
        "Evaluator",
        "Scores every handoff on 5 rubric dimensions; decides pass / retry / escalate",
        "Upstream artifact + new artifact + constraints",
        "EvaluationRecord",
    ),
}


class RequirementTriage(StrictModel):
    """What the Tier-1 triage model returns (live mode only)."""

    is_software_requirement: bool
    title: str = Field(description="3-8 word neutral title; empty when rejected.")
    reason: str = Field(description="One sentence the user can act on.")
    clarifications_needed: list[str] = Field(description="Up to 3 questions for the PM.")


class CheckResult(StrictModel):
    name: str
    passed: bool
    detail: str


AssessmentCode = Literal["ok", "invalid_requirement"]


class RequirementAssessment(StrictModel):
    valid: bool
    code: AssessmentCode
    title: str
    reasons: list[str]
    checks: list[CheckResult]
    clarifications: list[str] = Field(default_factory=list)
    mode: OrchestratorMode
    source: RunSource | None = None  # who produces the agents' output for this run
    demo_example_id: str | None = None


class PlanStep(StrictModel):
    order: int
    persona: Persona
    agent: str
    responsibility: str
    receives: str
    produces: str
    model_tier: ModelTier
    model: str
    gate: str | None  # evaluator gate that follows this step
    gate_label: str | None


class WorkflowPlan(StrictModel):
    steps: list[PlanStep]
    evaluator_model: str
    evaluator_confirmation_model: str
    max_attempts_per_handoff: int
    pass_rule: str


class Orchestration(StrictModel):
    run_id: str
    requirement: str  # PII-redacted
    assessment: RequirementAssessment
    plan: WorkflowPlan | None


def rule_checks(text: str) -> list[CheckResult]:
    """Free, deterministic checks that run before any model is involved."""
    words = [w for w in text.split() if any(c.isalpha() for c in w)]
    return [
        CheckResult(
            name="not_empty",
            passed=bool(text),
            detail="Requirement is present." if text else "Requirement is empty.",
        ),
        CheckResult(
            name="length",
            passed=MIN_CHARS <= len(text) <= MAX_CHARS,
            detail=f"{len(text)} characters (allowed {MIN_CHARS}-{MAX_CHARS}).",
        ),
        CheckResult(
            name="word_count",
            passed=len(words) >= MIN_WORDS,
            detail=f"{len(words)} words (at least {MIN_WORDS} needed to describe what to build).",
        ),
    ]


class MasterOrchestrator:
    def __init__(
        self,
        container: Container,
        service: RunService,
        *,
        mode: OrchestratorMode,
        demo: DemoLLMClient | None = None,
    ) -> None:
        if mode is OrchestratorMode.OFFLINE and demo is None:
            raise ValueError("offline mode needs a DemoLLMClient")
        self._c = container
        self._service = service
        self.mode = mode
        self.demo = demo

    def submit(self, requirement: str) -> Orchestration:
        """Validate and plan. Does not run the pipeline (see :meth:`execute`)."""
        run_id = self._service.new_run_id()
        redacted = self._c.redactor.redact(requirement.strip()).text
        assessment = self.assess(run_id, redacted)
        if not assessment.valid:
            self._audit(run_id, AuditEventType.REQUIREMENT_REJECTED, redacted, assessment)
            return Orchestration(
                run_id=run_id, requirement=redacted, assessment=assessment, plan=None
            )
        plan = self.plan()
        self._audit(run_id, AuditEventType.REQUIREMENT_VALIDATED, redacted, assessment)
        self._record(run_id, AuditEventType.WORKFLOW_PLANNED, plan.model_dump(mode="json"))
        if self.demo is not None:
            self.demo.assign(run_id, assessment.demo_example_id or SIMULATOR)
        return Orchestration(run_id=run_id, requirement=redacted, assessment=assessment, plan=plan)

    def execute(self, orchestration: Orchestration) -> None:
        """Dispatch a validated requirement to the agent pipeline (blocking)."""
        if not orchestration.assessment.valid:
            raise ValueError("refusing to execute an invalid requirement")
        self._service.start(
            orchestration.requirement,
            run_id=orchestration.run_id,
            scenario_id=orchestration.assessment.demo_example_id,
        )

    def assess(self, run_id: str, requirement: str) -> RequirementAssessment:
        checks = rule_checks(requirement)
        failed = [c.detail for c in checks if not c.passed]
        if failed:
            return self._assessment(False, "invalid_requirement", "", failed, checks)
        if self.mode is OrchestratorMode.OFFLINE:
            return self._assess_offline(requirement, checks)
        return self._assess_live(run_id, requirement, checks)

    def plan(self) -> WorkflowPlan:
        router = self._c.router
        steps = []
        for i, stage in enumerate(STAGES, start=1):
            tier = DEFAULT_TIERS[stage.persona]
            profile = AGENT_PROFILES[stage.persona]
            steps.append(
                PlanStep(
                    order=i,
                    persona=stage.persona,
                    agent=profile.name,
                    responsibility=profile.description,
                    receives=profile.receives,
                    produces=profile.produces,
                    model_tier=tier,
                    model=router.spec_for(tier).model_id,
                    gate=stage.handoff.value if stage.handoff else None,
                    gate_label=stage.handoff.label if stage.handoff else None,
                )
            )
        s = self._c.settings
        return WorkflowPlan(
            steps=steps,
            evaluator_model=router.tier1.model_id,
            evaluator_confirmation_model=router.tier2.model_id,
            max_attempts_per_handoff=s.max_attempts,
            pass_rule=f"mean score ≥ {s.pass_threshold}, every dimension ≥ "
            f"{s.min_dimension_score}, no dimension scored 1",
        )

    def _assess_offline(self, text: str, checks: list[CheckResult]) -> RequirementAssessment:
        assert self.demo is not None
        example: DemoExample | None = self.demo.library.match(text)
        if example is not None:
            detail = f"Matches the curated example '{example.title}'."
            checks = [*checks, CheckResult(name="demo_source", passed=True, detail=detail)]
            return self._assessment(
                True, "ok", example.title, [], checks, source="example", example_id=example.id
            )
        detail = (
            "No curated example matches, so the local simulator will run the full workflow "
            "(template content, rule-based judge, no model)."
        )
        checks = [*checks, CheckResult(name="demo_source", passed=True, detail=detail)]
        return self._assessment(True, "ok", subject_of(text), [], checks, source="simulator")

    def _assess_live(
        self, run_id: str, text: str, checks: list[CheckResult]
    ) -> RequirementAssessment:
        deps = self._c.agent_deps
        outcome = call_structured(
            deps,
            schema=RequirementTriage,
            system_prompt=persona_prompt(Persona.ORCHESTRATOR),
            user_content=render_sections([ContextSection("untrusted_business_request", text)]),
            route=deps.router.for_persona(Persona.ORCHESTRATOR, 1, force_tier2=False),
            tags=CallTags(
                run_id=run_id, persona=Persona.ORCHESTRATOR, purpose="orchestrator:triage"
            ),
            max_tokens=1000,
            naive_context_tokens=0,
        )
        record_calls(self._c.audit, self._c.router, outcome.records)
        triage = outcome.value
        if triage is None:
            reason = f"The requirement could not be assessed ({outcome.error}); please rephrase."
            return self._assessment(False, "invalid_requirement", "", [reason], checks)
        verdict = CheckResult(
            name="llm_triage", passed=triage.is_software_requirement, detail=triage.reason
        )
        checks = [*checks, verdict]
        if not triage.is_software_requirement:
            return self._assessment(False, "invalid_requirement", "", [triage.reason], checks)
        return self._assessment(
            True,
            "ok",
            triage.title or text[:60],
            [],
            checks,
            source="claude",
            clarifications=triage.clarifications_needed[:3],
        )

    def _assessment(
        self,
        valid: bool,
        code: AssessmentCode,
        title: str,
        reasons: list[str],
        checks: list[CheckResult],
        *,
        source: RunSource | None = None,
        example_id: str | None = None,
        clarifications: list[str] | None = None,
    ) -> RequirementAssessment:
        return RequirementAssessment(
            valid=valid,
            code=code,
            title=title,
            reasons=reasons,
            checks=checks,
            clarifications=clarifications or [],
            mode=self.mode,
            source=source,
            demo_example_id=example_id,
        )

    def _audit(
        self,
        run_id: str,
        event_type: AuditEventType,
        requirement: str,
        assessment: RequirementAssessment,
    ) -> None:
        payload: dict[str, object] = {
            "requirement": requirement,
            "assessment": assessment.model_dump(mode="json"),
        }
        self._record(run_id, event_type, payload)

    def _record(self, run_id: str, event_type: AuditEventType, payload: dict[str, object]) -> None:
        self._c.audit.append(
            AuditEvent(
                run_id=run_id,
                event_type=event_type,
                persona=Persona.ORCHESTRATOR.value,
                actor="master-orchestrator",
                payload=payload,
            )
        )
