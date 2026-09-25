"""Evaluator / judge: an independent quality gate on every handoff.

Three layers, cheapest first:

1. **Deterministic pre-gate** (:mod:`sdlc_loop.quality.checks`) — structural
   defects are rejected in code for zero tokens.
2. **Tier 1 judge** (Haiku) scores the five rubric dimensions.
3. **Tier 2 confirmation** (Sonnet) re-scores only when the Tier 1 result is
   borderline (3.0 ≤ overall < 4.0) or would page a human. The Tier 2 verdict is
   authoritative.

The judge returns only scores and feedback; the overall score, critical flag and
verdict are computed in code. If the judge itself cannot produce valid output
the gate fails *closed* (escalates to a human) — an unevaluated artifact never
passes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import BaseModel

from sdlc_loop.agents.base import AgentDeps, CallRecord, call_structured
from sdlc_loop.agents.context import (
    ContextSection,
    acceptance_criteria_section,
    dump,
    full_history_sections,
    render_sections,
    request_section,
)
from sdlc_loop.graph.routing import (
    GatePolicy,
    GateState,
    decide_verdict,
    decide_without_scores,
    is_critical,
    needs_tier2_confirmation,
)
from sdlc_loop.graph.state import SDLCState
from sdlc_loop.llm.token_meter import estimate_tokens
from sdlc_loop.llm.types import CallTags
from sdlc_loop.prompts import persona_prompt
from sdlc_loop.quality import checks
from sdlc_loop.schemas.common import Handoff, ModelTier, Persona
from sdlc_loop.schemas.evaluation import EvaluationRecord, JudgeOutput, JudgeSource, Verdict

JUDGE_MAX_TOKENS = 3000


@dataclass(slots=True)
class GateResult:
    record: EvaluationRecord
    records: list[CallRecord] = field(default_factory=list)
    findings: list[checks.Finding] = field(default_factory=list)


def _upstream_sections(handoff: Handoff, state: SDLCState) -> list[ContextSection]:
    brief, req, design = state.get("problem_brief"), state.get("requirements"), state.get("design")
    match handoff:
        case Handoff.PM_TO_BA:
            return [request_section(state)]
        case Handoff.BA_TO_ARCHITECT:
            assert brief is not None
            return [ContextSection("upstream_problem_brief", dump(brief))]
        case Handoff.ARCHITECT_TO_DEV:
            assert req is not None
            return [ContextSection("upstream_requirements_package", dump(req))]
        case Handoff.DEV_TO_QA:
            assert design is not None and req is not None
            return [
                ContextSection("upstream_technical_design", dump(design)),
                acceptance_criteria_section(req),
            ]


def _deterministic_findings(handoff: Handoff, state: SDLCState) -> list[checks.Finding]:
    brief, req = state.get("problem_brief"), state.get("requirements")
    match handoff:
        case Handoff.PM_TO_BA:
            assert brief is not None
            return checks.check_problem_brief(brief)
        case Handoff.BA_TO_ARCHITECT:
            assert brief is not None and req is not None
            return checks.check_requirements(req, brief)
        case Handoff.ARCHITECT_TO_DEV:
            design = state.get("design")
            assert design is not None
            return checks.check_design(design)
        case Handoff.DEV_TO_QA:
            impl = state.get("implementation")
            assert impl is not None and req is not None
            return checks.check_implementation(impl, req)


class EvaluatorAgent:
    def __init__(self, deps: AgentDeps, policy: GatePolicy) -> None:
        self.deps = deps
        self.policy = policy

    def evaluate(
        self,
        handoff: Handoff,
        artifact: BaseModel,
        state: SDLCState,
        *,
        attempt: int,
        gate: GateState,
    ) -> GateResult:
        findings = _deterministic_findings(handoff, state)
        hard = checks.blocking(findings)
        if self.deps.settings.deterministic_pregate and hard:
            return GateResult(
                record=EvaluationRecord(
                    handoff=handoff,
                    attempt_number=attempt,
                    dimension_scores=None,
                    overall_score=None,
                    critical_flag=False,
                    verdict=decide_without_scores(gate, self.policy),
                    feedback="Deterministic pre-gate rejected the artifact. Fix: "
                    + "; ".join(f.message for f in hard),
                    blocking_issues=[f.message for f in hard],
                    judge="deterministic",
                    model_used=None,
                ),
                findings=findings,
            )

        user_content = self._judge_prompt(handoff, artifact, state, findings)
        naive = estimate_tokens(render_sections(full_history_sections(state))) + estimate_tokens(
            user_content
        )
        records: list[CallRecord] = []

        first_confirm = not self.deps.settings.tiered_evaluator  # baseline: always Tier 2
        first = self._judge(handoff, user_content, state, attempt, naive, confirm=first_confirm)
        records += first.records
        if first.output is None:
            return GateResult(self._judge_failure(handoff, attempt, first), records, findings)

        scores = first.output.dimension_scores
        verdict = decide_verdict(scores, gate, self.policy)
        final, tier1_overall, escalated = first, None, False
        if (
            self.deps.settings.tiered_evaluator
            and first.tier is ModelTier.TIER1
            and needs_tier2_confirmation(scores, verdict, self.policy)
        ):
            second = self._judge(handoff, user_content, state, attempt, naive, confirm=True)
            records += second.records
            if second.output is not None:
                tier1_overall, escalated = scores.mean, True
                final = second
                scores = second.output.dimension_scores
                verdict = decide_verdict(scores, gate, self.policy)

        assert final.output is not None
        record = EvaluationRecord(
            handoff=handoff,
            attempt_number=attempt,
            dimension_scores=scores,
            overall_score=scores.mean,
            critical_flag=is_critical(scores),
            verdict=verdict,
            feedback=final.output.feedback,
            blocking_issues=final.output.blocking_issues,
            judge=final.source,
            model_used=final.model_id,
            tier1_overall_score=tier1_overall,
            judge_escalated=escalated,
        )
        return GateResult(record, records, findings)

    def _judge_prompt(
        self,
        handoff: Handoff,
        artifact: BaseModel,
        state: SDLCState,
        findings: list[checks.Finding],
    ) -> str:
        brief = state.get("problem_brief")
        sections = [ContextSection("handoff_under_review", handoff.label)]
        if brief is not None and handoff is not Handoff.PM_TO_BA:
            sections.append(ContextSection("original_constraints", json.dumps(brief.constraints)))
        sections += _upstream_sections(handoff, state)
        sections.append(ContextSection("artifact_under_review", dump(artifact)))
        if findings:
            sections.append(
                ContextSection(
                    "deterministic_precheck_notes",
                    "\n".join(f"- [{f.severity}] {f.message}" for f in findings),
                )
            )
        return render_sections(sections)

    @dataclass(slots=True)
    class _JudgeCall:
        output: JudgeOutput | None
        records: list[CallRecord]
        tier: ModelTier
        source: JudgeSource
        model_id: str
        error: str | None

    def _judge(
        self,
        handoff: Handoff,
        user_content: str,
        state: SDLCState,
        attempt: int,
        naive: int,
        *,
        confirm: bool,
    ) -> EvaluatorAgent._JudgeCall:
        route = self.deps.router.for_judge(confirm=confirm)
        source: JudgeSource = "tier2" if route.tier is ModelTier.TIER2 else "tier1"
        outcome = call_structured(
            self.deps,
            schema=JudgeOutput,
            system_prompt=persona_prompt(Persona.EVALUATOR),
            user_content=user_content,
            route=route,
            tags=CallTags(
                run_id=state["run_id"],
                persona=Persona.EVALUATOR,
                purpose=f"judge:{handoff.value}:{source}",
                attempt=attempt,
                handoff=handoff.value,
            ),
            max_tokens=JUDGE_MAX_TOKENS,
            naive_context_tokens=naive,
        )
        return EvaluatorAgent._JudgeCall(
            outcome.value, outcome.records, route.tier, source, route.spec.model_id, outcome.error
        )

    @staticmethod
    def _judge_failure(
        handoff: Handoff, attempt: int, call: EvaluatorAgent._JudgeCall
    ) -> EvaluationRecord:
        return EvaluationRecord(
            handoff=handoff,
            attempt_number=attempt,
            dimension_scores=None,
            overall_score=None,
            critical_flag=False,
            verdict=Verdict.ESCALATE,
            feedback=f"Evaluator could not produce a valid judgement ({call.error}); "
            "failing closed to human review.",
            judge=call.source,
            model_used=call.model_id,
        )
