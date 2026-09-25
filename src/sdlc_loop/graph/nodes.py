"""LangGraph node factories: persona nodes, evaluator gates, human review, finalize.

Nodes are thin: they read bookkeeping from state, delegate to an agent, write
audit rows and return a *partial* state update. All decision logic lives in
:mod:`sdlc_loop.graph.routing` so it can be unit-tested without a graph.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langgraph.types import interrupt

from sdlc_loop.agents.base import PersonaAgent, RetryContext
from sdlc_loop.agents.evaluator import EvaluatorAgent, GateResult
from sdlc_loop.config import CriticalPolicy, Settings
from sdlc_loop.governance.audit import AuditEvent, AuditEventType, AuditLog
from sdlc_loop.graph.routing import (
    HUMAN_ABORT,
    HUMAN_ACCEPT,
    HUMAN_RETRY,
    GatePolicy,
    GateState,
    decide_without_scores,
)
from sdlc_loop.graph.stages import Stage, stage_for_handoff
from sdlc_loop.graph.state import SDLCState
from sdlc_loop.graph.telemetry import record_calls
from sdlc_loop.llm.model_router import ModelRouter
from sdlc_loop.schemas.artifacts import ExecutionReport
from sdlc_loop.schemas.common import Persona, RunStatus
from sdlc_loop.schemas.evaluation import EvaluationRecord, Verdict
from sdlc_loop.schemas.review import HumanDecision, ReviewRequest

Node = Callable[[SDLCState], dict[str, Any]]


class AgentOutputError(RuntimeError):
    """A persona without a gate (QA) produced no valid output after repair attempts."""


@dataclass(frozen=True, slots=True)
class GraphDeps:
    personas: dict[Persona, PersonaAgent]
    evaluator: EvaluatorAgent
    audit: AuditLog
    router: ModelRouter
    settings: Settings
    policy: GatePolicy


def make_persona_node(stage: Stage, deps: GraphDeps) -> Node:
    agent = deps.personas[stage.persona]

    def persona_node(state: SDLCState) -> dict[str, Any]:
        key = stage.key
        attempt = (state.get("retry_counts") or {}).get(key, 0) + 1
        retry = RetryContext(
            attempt=attempt,
            force_tier2=(state.get("tier2_forced") or {}).get(key, False),
            feedback=tuple((state.get("feedback_history") or {}).get(key, [])),
            human_guidance=(state.get("human_guidance") or {}).get(key),
            previous=state.get(stage.artifact_key) if attempt > 1 else None,
        )
        result = agent.run(state, retry)
        output = result.artifact.model_dump(mode="json") if result.artifact else None
        if isinstance(result.measured, ExecutionReport):
            deps.audit.append(
                AuditEvent(
                    run_id=state["run_id"],
                    event_type=AuditEventType.CODE_VERIFICATION,
                    persona=stage.persona.value,
                    payload=result.measured.model_dump(mode="json"),
                )
            )
        record_calls(
            deps.audit,
            deps.router,
            result.records,
            handoff=stage.handoff.value if stage.handoff else None,
            output_payload=output,
        )

        invalid = dict(state.get("invalid_output") or {})
        update: dict[str, Any] = {"status": RunStatus.RUNNING}
        if result.artifact is None:
            if stage.handoff is None:
                raise AgentOutputError(f"{stage.persona} produced no valid output: {result.error}")
            invalid[key] = result.error or "no valid output"
        else:
            invalid.pop(key, None)
            update[stage.artifact_key] = result.artifact
        update["invalid_output"] = invalid
        return update

    persona_node.__name__ = f"{stage.node}_node"
    return persona_node


def make_gate_node(stage: Stage, deps: GraphDeps) -> Node:
    handoff = stage.handoff
    assert handoff is not None

    def gate_node(state: SDLCState) -> dict[str, Any]:
        key = stage.key
        retry_counts = dict(state.get("retry_counts") or {})
        critical_used = dict(state.get("critical_retry_used") or {})
        tier2_forced = dict(state.get("tier2_forced") or {})
        feedback = {k: list(v) for k, v in (state.get("feedback_history") or {}).items()}
        retries = retry_counts.get(key, 0)
        attempt = retries + 1
        gate = GateState(retries_used=retries, critical_retry_used=critical_used.get(key, False))

        invalid = (state.get("invalid_output") or {}).get(key)
        artifact = state.get(stage.artifact_key)
        if invalid is not None or artifact is None:
            result = GateResult(
                EvaluationRecord(
                    handoff=handoff,
                    attempt_number=attempt,
                    dimension_scores=None,
                    overall_score=None,
                    critical_flag=False,
                    verdict=decide_without_scores(gate, deps.policy),
                    feedback=f"Output did not match the required schema: {invalid}",
                    blocking_issues=[f"schema validation: {invalid}"],
                    judge="deterministic",
                    model_used=None,
                )
            )
        else:
            result = deps.evaluator.evaluate(handoff, artifact, state, attempt=attempt, gate=gate)

        record_calls(deps.audit, deps.router, result.records, handoff=handoff.value)
        rec = result.record
        deps.audit.append(
            AuditEvent(
                run_id=state["run_id"],
                event_type=AuditEventType.EVALUATION,
                persona=stage.persona.value,
                handoff=handoff.value,
                attempt=attempt,
                model=rec.model_used,
                verdict=rec.verdict.value,
                payload={
                    **rec.model_dump(mode="json"),
                    "findings": [f"[{f.severity}] {f.message}" for f in result.findings],
                },
            )
        )

        update: dict[str, Any] = {"evaluations": [rec], "last_verdict": rec.verdict.value}
        if rec.verdict is Verdict.RETRY:
            retry_counts[key] = retries + 1
            issues = "\n".join(f"- {i}" for i in rec.blocking_issues)
            feedback[key] = [*feedback.get(key, []), f"{rec.feedback}\n{issues}".strip()]
            if rec.critical_flag and deps.policy.critical_policy is CriticalPolicy.FAST_TRACK:
                critical_used[key] = True
                tier2_forced[key] = True
            update |= {"pending_handoff": None, "status": RunStatus.RUNNING}
        elif rec.verdict is Verdict.ESCALATE:
            update |= {"pending_handoff": handoff, "status": RunStatus.BLOCKED_FOR_HUMAN}
            deps.audit.append(
                AuditEvent(
                    run_id=state["run_id"],
                    event_type=AuditEventType.ESCALATION,
                    persona=stage.persona.value,
                    handoff=handoff.value,
                    attempt=attempt,
                    verdict=rec.verdict.value,
                    payload={"reason": rec.feedback, "critical_flag": rec.critical_flag},
                )
            )
        else:
            update |= {"pending_handoff": None, "status": RunStatus.RUNNING}
        update |= {
            "retry_counts": retry_counts,
            "critical_retry_used": critical_used,
            "tier2_forced": tier2_forced,
            "feedback_history": feedback,
        }
        return update

    gate_node.__name__ = f"{stage.gate_node}_node"
    return gate_node


def make_human_review_node(deps: GraphDeps) -> Node:
    def human_review_node(state: SDLCState) -> dict[str, Any]:
        handoff = state.get("pending_handoff")
        assert handoff is not None, "human review reached without a pending handoff"
        stage = stage_for_handoff(handoff)
        key = stage.key
        last = state["evaluations"][-1]
        artifact = state.get(stage.artifact_key)
        retry_counts = dict(state.get("retry_counts") or {})
        request = ReviewRequest(
            handoff=handoff,
            persona=stage.persona,
            artifact=artifact.model_dump(mode="json") if artifact is not None else {},
            evaluator_feedback=last.feedback,
            blocking_issues=last.blocking_issues,
            overall_score=last.overall_score,
            critical_flag=last.critical_flag,
            attempts_so_far=retry_counts.get(key, 0) + 1,
        )
        # Pauses the graph; the checkpointer persists state until a reviewer resumes
        # with Command(resume=HumanDecision). Code above re-runs on resume, so it
        # must stay side-effect free — the escalation row was written by the gate.
        decision = HumanDecision.model_validate(interrupt(request.model_dump(mode="json")))
        if decision.action == "accept_as_is" and artifact is None:
            decision = HumanDecision(
                action="abort", reviewer=decision.reviewer, guidance="no valid artifact to accept"
            )
        deps.audit.append(
            AuditEvent(
                run_id=state["run_id"],
                event_type=AuditEventType.HUMAN_DECISION,
                persona=stage.persona.value,
                handoff=handoff.value,
                attempt=retry_counts.get(key, 0) + 1,
                actor=decision.reviewer,
                verdict=decision.action,
                payload=decision.model_dump(mode="json"),
            )
        )
        update: dict[str, Any] = {"human_decisions": [decision], "status": RunStatus.RUNNING}
        if decision.action == "accept_as_is":
            update["last_verdict"] = HUMAN_ACCEPT
        elif decision.action == "retry":
            guidance = dict(state.get("human_guidance") or {})
            guidance[key] = decision.guidance or ""
            tier2_forced = dict(state.get("tier2_forced") or {})
            tier2_forced[key] = True
            retry_counts[key] = retry_counts.get(key, 0) + 1
            feedback = {k: list(v) for k, v in (state.get("feedback_history") or {}).items()}
            feedback[key] = [*feedback.get(key, []), last.feedback]
            update |= {
                "last_verdict": HUMAN_RETRY,
                "human_guidance": guidance,
                "tier2_forced": tier2_forced,
                "retry_counts": retry_counts,
                "feedback_history": feedback,
            }
        else:
            update |= {"last_verdict": HUMAN_ABORT, "status": RunStatus.ABORTED}
            deps.audit.append(
                AuditEvent(
                    run_id=state["run_id"],
                    event_type=AuditEventType.RUN_ABORTED,
                    actor=decision.reviewer,
                    handoff=handoff.value,
                    payload={"guidance": decision.guidance},
                )
            )
        return update

    return human_review_node


def make_finalize_node(deps: GraphDeps) -> Node:
    def finalize_node(state: SDLCState) -> dict[str, Any]:
        evaluations = state.get("evaluations") or []
        deps.audit.append(
            AuditEvent(
                run_id=state["run_id"],
                event_type=AuditEventType.RUN_COMPLETED,
                payload={
                    "evaluations": len(evaluations),
                    "human_decisions": len(state.get("human_decisions") or []),
                    "retry_counts": state.get("retry_counts") or {},
                },
            )
        )
        return {"status": RunStatus.COMPLETE, "pending_handoff": None}

    return finalize_node
