"""Builds the browser-facing view of a run.

Two sources, each used for what it is authoritative for:

* the **audit log** — every attempt of every agent (including failed first
  attempts that the checkpoint state has since overwritten), every evaluation
  with its five scores, every human decision, with timestamps and costs;
* the **checkpoint state** (via :class:`RunService`) — the run's current status
  and the pending human-review request.

The browser polls ``GET /api/runs/{id}/view`` and renders this model; it never
has to understand audit rows or LangGraph state itself.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Literal

from sdlc_loop.governance.audit import AuditEventType, AuditLog, StoredAuditEvent
from sdlc_loop.graph.stages import STAGES, Stage
from sdlc_loop.orchestrator.master import AGENT_PROFILES
from sdlc_loop.schemas.common import Handoff, Persona, RunStatus, StrictModel
from sdlc_loop.schemas.review import ReviewRequest
from sdlc_loop.services.run_service import RunNotFoundError, RunService, RunState

StageStatus = Literal[
    "pending",
    "running",
    "retrying",
    "awaiting_review",
    "passed",
    "accepted_by_human",
    "completed",
    "aborted",
    "failed",
]
Tone = Literal["info", "success", "warning", "danger"]

CALLS = {AuditEventType.PERSONA_CALL, AuditEventType.PARSE_FAILURE}
ORCHESTRATOR_DECISIONS = {
    AuditEventType.REQUIREMENT_VALIDATED,
    AuditEventType.REQUIREMENT_REJECTED,
}
TERMINAL = {RunStatus.COMPLETE.value, RunStatus.ABORTED.value, RunStatus.FAILED.value}
ENDINGS = {AuditEventType.RUN_COMPLETED, AuditEventType.RUN_ABORTED, AuditEventType.RUN_FAILED}
VERDICT_TONE: dict[str, Tone] = {"pass": "success", "retry": "warning", "escalate": "danger"}


class EvaluationView(StrictModel):
    handoff: str
    handoff_label: str
    attempt: int
    verdict: str
    overall_score: float | None
    critical_flag: bool
    judge: str
    model_used: str | None
    dimension_scores: dict[str, int] | None
    feedback: str
    blocking_issues: list[str]
    findings: list[str]
    tier1_overall_score: float | None
    judge_escalated: bool


class AttemptView(StrictModel):
    attempt: int
    model: str | None
    tier: str | None
    route_reason: str | None
    llm_calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    output: dict[str, Any] | None
    parse_error: str | None
    evaluation: EvaluationView | None


class StageView(StrictModel):
    order: int
    persona: str
    agent: str
    description: str
    produces: str
    handoff: str | None
    handoff_label: str | None
    status: StageStatus
    attempts: list[AttemptView]
    retries: int
    latest_score: float | None
    started_at: str | None
    finished_at: str | None
    log: list[str]


class EvaluatorView(StrictModel):
    agent: str
    description: str
    evaluations: list[EvaluationView]
    judge_calls: int
    tier2_confirmations: int
    deterministic_rejections: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class DecisionView(StrictModel):
    """One routing decision: an evaluator verdict or a human reviewer's action."""

    ts: str
    handoff_label: str
    attempt: int
    decision: str  # PASS / RETRY / ESCALATE / ACCEPT_AS_IS / RETRY_WITH_GUIDANCE / ABORT
    decided_by: str
    reason: str
    tone: Tone


class TimelineEntry(StrictModel):
    ts: str
    actor: str
    message: str
    tone: Tone


class Totals(StrictModel):
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    llm_latency_ms: float
    llm_calls: int
    retries: int
    escalations: int
    human_decisions: int


class FinalResult(StrictModel):
    headline: str
    tone: Tone
    gates_passed: int
    gates_total: int
    highlights: list[str]
    test_execution: dict[str, Any] | None


class RunView(StrictModel):
    run_id: str
    version: str  # changes whenever anything shown here changes (see ``view_version``)
    title: str
    requirement: str
    status: str  # RunStatus value, or "queued" / "rejected"
    source: str | None  # "claude" / "example" / "simulator"
    started_at: str | None
    finished_at: str | None
    error: str | None
    assessment: dict[str, Any] | None
    plan: dict[str, Any] | None
    stages: list[StageView]
    evaluator: EvaluatorView
    decisions: list[DecisionView]
    timeline: list[TimelineEntry]
    pending_review: ReviewRequest | None
    totals: Totals
    final_result: FinalResult | None


class RunSummary(StrictModel):
    run_id: str
    title: str
    created: str
    status: str


def _input_tokens(e: StoredAuditEvent) -> int:
    return e.input_tokens + e.cache_read_tokens + e.cache_write_tokens


def _label(handoff: str | None) -> str:
    return Handoff(handoff).label if handoff else ""


def _score(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "n/a"


def _evaluation(e: StoredAuditEvent) -> EvaluationView:
    p = e.payload
    handoff = Handoff(e.handoff or p["handoff"])
    return EvaluationView(
        handoff=handoff.value,
        handoff_label=handoff.label,
        attempt=e.attempt or p.get("attempt_number", 1),
        verdict=e.verdict or "",
        overall_score=p.get("overall_score"),
        critical_flag=bool(p.get("critical_flag")),
        judge=p.get("judge", ""),
        model_used=p.get("model_used"),
        dimension_scores=p.get("dimension_scores"),
        feedback=p.get("feedback", ""),
        blocking_issues=list(p.get("blocking_issues") or []),
        findings=list(p.get("findings") or []),
        tier1_overall_score=p.get("tier1_overall_score"),
        judge_escalated=bool(p.get("judge_escalated")),
    )


def _attempts(stage: Stage, events: Sequence[StoredAuditEvent]) -> list[AttemptView]:
    persona = stage.persona.value
    calls = [e for e in events if e.event_type in CALLS and e.persona == persona]
    handoff = stage.handoff.value if stage.handoff else None
    evals = {
        e.attempt: _evaluation(e)
        for e in events
        if e.event_type is AuditEventType.EVALUATION and handoff and e.handoff == handoff
    }
    out: list[AttemptView] = []
    for n in sorted({c.attempt or 1 for c in calls}):
        group = [c for c in calls if (c.attempt or 1) == n]
        ok = [c for c in group if c.event_type is AuditEventType.PERSONA_CALL]
        failed = [c for c in group if c.event_type is AuditEventType.PARSE_FAILURE]
        last = (ok or group)[-1]
        out.append(
            AttemptView(
                attempt=n,
                model=last.model,
                tier=last.payload.get("tier"),
                route_reason=last.payload.get("route_reason"),
                llm_calls=len(group),
                input_tokens=sum(_input_tokens(c) for c in group),
                output_tokens=sum(c.output_tokens for c in group),
                cost_usd=round(sum(c.cost_usd for c in group), 6),
                latency_ms=round(sum(c.latency_ms or 0.0 for c in group), 1),
                output=ok[-1].payload.get("output") if ok else None,
                parse_error=None if ok or not failed else failed[-1].payload.get("parse_error"),
                evaluation=evals.get(n),
            )
        )
    return out


def _done_status(stage: Stage, events: Sequence[StoredAuditEvent]) -> StageStatus | None:
    """``passed`` / ``accepted_by_human`` / ``completed`` if the stage is finished."""
    if stage.handoff is None:
        produced = any(
            e.event_type is AuditEventType.PERSONA_CALL and e.persona == stage.persona.value
            for e in events
        )
        return "completed" if produced else None
    decisive = [
        e
        for e in events
        if e.handoff == stage.handoff.value
        and e.event_type in {AuditEventType.EVALUATION, AuditEventType.HUMAN_DECISION}
    ]
    if not decisive:
        return None
    last = decisive[-1]
    if last.event_type is AuditEventType.EVALUATION and last.verdict == "pass":
        return "passed"
    if last.event_type is AuditEventType.HUMAN_DECISION and last.verdict == "accept_as_is":
        return "accepted_by_human"
    return None


def _active_status(
    stage: Stage, attempts: list[AttemptView], run_status: str, state: RunState | None
) -> StageStatus:
    """Status of the first unfinished stage — the one the run is currently on."""
    review = state.pending_review if state else None
    if review is not None and stage.handoff is review.handoff:
        return "awaiting_review"
    if run_status == RunStatus.ABORTED.value:
        return "aborted"
    if run_status == RunStatus.FAILED.value:
        return "failed"
    if run_status != RunStatus.RUNNING.value:
        return "pending"
    last_eval = attempts[-1].evaluation if attempts else None
    return "retrying" if last_eval and last_eval.verdict == "retry" else "running"


def _stage_log(stage: Stage, attempts: list[AttemptView], status: StageStatus) -> list[str]:
    lines: list[str] = []
    for a in attempts:
        lines.append(
            f"Attempt {a.attempt} on {a.model} ({a.tier}): {a.input_tokens:,} in / "
            f"{a.output_tokens:,} out tokens, ${a.cost_usd:.4f}"
        )
        if a.parse_error:
            lines.append(f"Output failed schema validation: {a.parse_error[:120]}")
        elif a.output is not None:
            lines.append(f"Produced {AGENT_PROFILES[stage.persona].produces}")
        if ev := a.evaluation:
            judge = "Tier-2 judge (confirmed)" if ev.judge_escalated else f"{ev.judge} judge"
            flag = " · CRITICAL" if ev.critical_flag else ""
            lines.append(
                f"Gate {ev.handoff_label}: {ev.verdict.upper()} · score "
                f"{_score(ev.overall_score)} · {judge}{flag}"
            )
    status_line = {
        "running": "Working…",
        "retrying": "Retrying with evaluator feedback…",
        "awaiting_review": "Blocked: waiting for a human reviewer",
        "accepted_by_human": "Accepted by a human reviewer",
        "aborted": "Run aborted",
        "failed": "Run failed at this stage",
    }.get(status)
    return [*lines, status_line] if status_line else lines


def _stage_times(
    stage: Stage, events: Sequence[StoredAuditEvent], status: StageStatus
) -> tuple[str | None, str | None]:
    persona = stage.persona.value
    calls = [e.ts for e in events if e.event_type in CALLS and e.persona == persona]
    if not calls:
        return None, None
    if status not in {"passed", "accepted_by_human", "completed"}:
        return calls[0], None
    if stage.handoff is None:
        return calls[0], calls[-1]
    decisive = [
        e.ts
        for e in events
        if e.handoff == stage.handoff.value
        and e.event_type in {AuditEventType.EVALUATION, AuditEventType.HUMAN_DECISION}
    ]
    return calls[0], decisive[-1] if decisive else calls[-1]


def _stages(
    events: Sequence[StoredAuditEvent], run_status: str, state: RunState | None
) -> list[StageView]:
    views: list[StageView] = []
    active_seen = False
    for order, stage in enumerate(STAGES, start=1):
        attempts = _attempts(stage, events)
        status = _done_status(stage, events)
        if status is None:
            status = (
                "pending" if active_seen else _active_status(stage, attempts, run_status, state)
            )
            active_seen = True
        profile = AGENT_PROFILES[stage.persona]
        started_at, finished_at = _stage_times(stage, events, status)
        scores = [
            a.evaluation.overall_score
            for a in attempts
            if a.evaluation and a.evaluation.overall_score is not None
        ]
        views.append(
            StageView(
                order=order,
                persona=stage.persona.value,
                agent=profile.name,
                description=profile.description,
                produces=profile.produces,
                handoff=stage.handoff.value if stage.handoff else None,
                handoff_label=stage.handoff.label if stage.handoff else None,
                status=status,
                attempts=attempts,
                retries=max(0, len(attempts) - 1),
                latest_score=scores[-1] if scores else None,
                started_at=started_at,
                finished_at=finished_at,
                log=_stage_log(stage, attempts, status),
            )
        )
    return views


_Entry = tuple[str, str, Tone] | None  # (actor, message, tone)
ORCH, ENGINE = "Master Orchestrator", "Workflow engine"


def _assessment(e: StoredAuditEvent) -> dict[str, Any]:
    value = e.payload.get("assessment")
    return value if isinstance(value, dict) else {}


def _agent_call(e: StoredAuditEvent) -> _Entry:
    persona = Persona(e.persona) if e.persona else None
    if persona is None:
        return None
    if persona is Persona.EVALUATOR:
        tier = "Tier-2 confirmation" if e.payload.get("tier") == "tier2" else "Tier-1 judge"
        where = f"{_label(e.handoff)} attempt {e.attempt}"
        return "Evaluator", f"{tier} scored {where} on {e.model}", "info"
    if persona is Persona.ORCHESTRATOR:
        return ORCH, f"LLM triage of the requirement on {e.model}", "info"
    profile = AGENT_PROFILES[persona]
    return profile.name, f"Attempt {e.attempt} produced {profile.produces} on {e.model}", "info"


def _parse_failure(e: StoredAuditEvent) -> _Entry:
    message = f"{e.persona} attempt {e.attempt}: output failed validation; repairing"
    return "Schema validation", message, "warning"


def _evaluation_entry(e: StoredAuditEvent) -> _Entry:
    ev = _evaluation(e)
    score = _score(ev.overall_score) if ev.judge != "deterministic" else "pre-gate"
    critical = ", critical" if ev.critical_flag else ""
    verdict = f"{ev.verdict.upper()} (score {score}{critical})"
    return (
        "Evaluator",
        f"Gate {ev.handoff_label} attempt {ev.attempt}: {verdict}",
        VERDICT_TONE.get(ev.verdict, "danger"),
    )


def _code_entry(e: StoredAuditEvent) -> _Entry:
    p = e.payload
    if p.get("tests_executed"):
        message = (
            f"Ran generated tests: {p.get('tests_passed')} passed, {p.get('tests_failed')} failed"
        )
    else:
        message = (
            f"Syntax-checked {p.get('files_checked')} files; tests not run "
            f"({p.get('skipped_reason')})"
        )
    return "Code verifier", message, "danger" if p.get("syntax_errors") else "success"


def _validated(e: StoredAuditEvent) -> _Entry:
    return ORCH, f"Requirement validated: “{_assessment(e).get('title', '')}”", "success"


def _rejected(e: StoredAuditEvent) -> _Entry:
    reasons = "; ".join(_assessment(e).get("reasons", []))
    return ORCH, f"Requirement rejected: {reasons}", "danger"


def _planned(e: StoredAuditEvent) -> _Entry:
    n = len(e.payload.get("steps", []))
    message = f"Workflow planned: {n} agents with an evaluator gate after each handoff"
    return ORCH, f"{message}; dispatching to the Product Manager", "info"


def _pii(e: StoredAuditEvent) -> _Entry:
    return (
        "Governance",
        f"PII redacted before any model call: {e.payload.get('entity_counts')}",
        "info",
    )


def _escalation(e: StoredAuditEvent) -> _Entry:
    return "Evaluator", f"Escalated {_label(e.handoff)} to a human reviewer", "danger"


def _human(e: StoredAuditEvent) -> _Entry:
    return f"Reviewer ({e.actor})", f"Decision: {e.verdict}", "warning"


def _failed(e: StoredAuditEvent) -> _Entry:
    return ENGINE, f"Run failed: {e.payload.get('error')}", "danger"


_STATIC: dict[AuditEventType, tuple[str, str, Tone]] = {
    AuditEventType.RUN_STARTED: (ENGINE, "Pipeline started (LangGraph)", "info"),
    AuditEventType.RUN_COMPLETED: (ENGINE, "Pipeline complete", "success"),
    AuditEventType.RUN_ABORTED: (ENGINE, "Run aborted by reviewer", "danger"),
}
_DYNAMIC: dict[AuditEventType, Callable[[StoredAuditEvent], _Entry]] = {
    AuditEventType.REQUIREMENT_VALIDATED: _validated,
    AuditEventType.REQUIREMENT_REJECTED: _rejected,
    AuditEventType.WORKFLOW_PLANNED: _planned,
    AuditEventType.PII_REDACTED: _pii,
    AuditEventType.PERSONA_CALL: _agent_call,
    AuditEventType.PARSE_FAILURE: _parse_failure,
    AuditEventType.EVALUATION: _evaluation_entry,
    AuditEventType.ESCALATION: _escalation,
    AuditEventType.HUMAN_DECISION: _human,
    AuditEventType.CODE_VERIFICATION: _code_entry,
    AuditEventType.RUN_FAILED: _failed,
}


def _timeline(events: Sequence[StoredAuditEvent]) -> list[TimelineEntry]:
    out: list[TimelineEntry] = []
    for e in events:
        handler = _DYNAMIC.get(e.event_type)
        entry = handler(e) if handler else _STATIC.get(e.event_type)
        if entry is not None:
            actor, message, tone = entry
            out.append(TimelineEntry(ts=e.ts, actor=actor, message=message, tone=tone))
    return out


_HUMAN_DECISION = {
    "accept_as_is": "ACCEPT_AS_IS",
    "retry": "RETRY_WITH_GUIDANCE",
    "abort": "ABORT",
}


def _decisions(events: Sequence[StoredAuditEvent]) -> list[DecisionView]:
    out: list[DecisionView] = []
    for e in events:
        if e.event_type is AuditEventType.EVALUATION:
            ev = _evaluation(e)
            judge = "deterministic pre-gate" if ev.judge == "deterministic" else f"{ev.judge} judge"
            score = f" (score {_score(ev.overall_score)})" if ev.overall_score is not None else ""
            out.append(
                DecisionView(
                    ts=e.ts,
                    handoff_label=ev.handoff_label,
                    attempt=ev.attempt,
                    decision=ev.verdict.upper(),
                    decided_by=f"Evaluator, {judge}{score}",
                    reason=ev.feedback,
                    tone=VERDICT_TONE.get(ev.verdict, "danger"),
                )
            )
        elif e.event_type is AuditEventType.HUMAN_DECISION:
            action = e.verdict or ""
            out.append(
                DecisionView(
                    ts=e.ts,
                    handoff_label=_label(e.handoff),
                    attempt=e.attempt or 1,
                    decision=_HUMAN_DECISION.get(action, action.upper()),
                    decided_by=f"Human reviewer ({e.actor})",
                    reason=str(e.payload.get("guidance") or "No guidance given."),
                    tone="danger" if action == "abort" else "warning",
                )
            )
    return out


def _run_status(
    events: Sequence[StoredAuditEvent], decision: StoredAuditEvent | None, state: RunState | None
) -> str:
    if any(e.event_type is AuditEventType.RUN_FAILED for e in events):
        return RunStatus.FAILED.value
    if state is not None:
        return state.status.value
    if decision is not None and decision.event_type is AuditEventType.REQUIREMENT_REJECTED:
        return "rejected"
    return "queued"  # validated and planned; the background task has not started yet


def _evaluator(events: Sequence[StoredAuditEvent]) -> EvaluatorView:
    evals = [_evaluation(e) for e in events if e.event_type is AuditEventType.EVALUATION]
    judge_calls = [e for e in events if e.event_type in CALLS and e.persona == "evaluator"]
    profile = AGENT_PROFILES[Persona.EVALUATOR]
    return EvaluatorView(
        agent=profile.name,
        description=profile.description,
        evaluations=evals,
        judge_calls=len(judge_calls),
        tier2_confirmations=sum(e.judge_escalated for e in evals),
        deterministic_rejections=sum(e.judge == "deterministic" for e in evals),
        input_tokens=sum(_input_tokens(e) for e in judge_calls),
        output_tokens=sum(e.output_tokens for e in judge_calls),
        cost_usd=round(sum(e.cost_usd for e in judge_calls), 6),
    )


def _totals(events: Sequence[StoredAuditEvent]) -> Totals:
    evaluations = [e for e in events if e.event_type is AuditEventType.EVALUATION]
    calls = [e for e in events if e.event_type in CALLS]
    return Totals(
        cost_usd=round(sum(e.cost_usd for e in events), 6),
        input_tokens=sum(_input_tokens(e) for e in events),
        output_tokens=sum(e.output_tokens for e in events),
        cache_read_tokens=sum(e.cache_read_tokens for e in calls),
        cache_write_tokens=sum(e.cache_write_tokens for e in calls),
        llm_latency_ms=round(sum(e.latency_ms or 0.0 for e in calls), 1),
        llm_calls=len(calls),
        retries=sum(e.verdict == "retry" for e in evaluations),
        escalations=sum(e.event_type is AuditEventType.ESCALATION for e in events),
        human_decisions=sum(e.event_type is AuditEventType.HUMAN_DECISION for e in events),
    )


def _highlights(artifacts: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if req := artifacts.get("requirements"):
        n_ac = sum(len(s["acceptance_criteria"]) for s in req["user_stories"])
        out.append(f"{len(req['user_stories'])} user stories with {n_ac} acceptance criteria")
    if design := artifacts.get("design"):
        out.append(
            f"{len(design['components'])} components, {len(design['risks'])} risks with "
            f"mitigations, {len(design['adrs'])} architecture decision records"
        )
    if impl := artifacts.get("implementation"):
        stories = ", ".join(impl["implemented_story_ids"])
        out.append(f"{len(impl['files'])} code files implementing {stories}")
    if plan := artifacts.get("test_plan"):
        out.append(f"{len(plan['test_cases'])} test cases mapped to acceptance criteria")
    return out


def _final(
    status: str, stages: list[StageView], state: RunState | None, evaluator: EvaluatorView
) -> FinalResult | None:
    if status not in TERMINAL:
        return None
    gated = [s for s in stages if s.handoff]
    overridden = [s.handoff_label or "" for s in gated if s.status == "accepted_by_human"]
    artifacts = state.artifacts if state else {}
    retries = sum(s.retries for s in stages)
    highlights = [
        *_highlights(artifacts),
        f"{retries} retr{'y' if retries == 1 else 'ies'} triggered by the evaluator; "
        f"{evaluator.tier2_confirmations} Tier-2 judge confirmation(s)",
    ]
    tone: Tone
    if status == RunStatus.COMPLETE.value and not overridden:
        headline, tone = f"SDLC plan complete: all {len(gated)} evaluator gates passed", "success"
    elif status == RunStatus.COMPLETE.value:
        headline = f"SDLC plan complete with a human override at {', '.join(overridden)}"
        tone = "warning"
    elif status == RunStatus.ABORTED.value:
        headline, tone = "Run aborted by a human reviewer", "danger"
    else:
        headline, tone = "Run failed; see the timeline for the error", "danger"
    test_plan = artifacts.get("test_plan") or {}
    return FinalResult(
        headline=headline,
        tone=tone,
        gates_passed=sum(s.status == "passed" for s in gated),
        gates_total=len(gated),
        highlights=highlights,
        test_execution=test_plan.get("execution"),
    )


def is_settled(view: RunView) -> bool:
    """True when the run will not change again without a human action."""
    if view.status == RunStatus.BLOCKED_FOR_HUMAN.value:
        return view.pending_review is not None
    return view.status in TERMINAL or view.status == "rejected"


def view_version(last_event_id: int, state: RunState | None) -> str:
    """Changes whenever the rendered view would change.

    New audit rows cover almost every transition; the status and pending review
    are included for the one transition that writes no row (the graph pausing
    for human review after the escalation row was written).
    """
    status = state.status.value if state else "none"
    return f"{last_event_id}:{status}:{int(bool(state and state.pending_review))}"


def load_state(service: RunService, run_id: str) -> RunState | None:
    try:
        return service.state(run_id)
    except RunNotFoundError:
        return None


def build_view(
    run_id: str, audit: AuditLog, service: RunService, state: RunState | None = None
) -> RunView:
    events = audit.events(run_id)
    if not events:
        raise RunNotFoundError(run_id)
    decision = next((e for e in events if e.event_type in ORCHESTRATOR_DECISIONS), None)
    planned = next((e for e in events if e.event_type is AuditEventType.WORKFLOW_PLANNED), None)
    state = state or load_state(service, run_id)
    status = _run_status(events, decision, state)
    stages = _stages(events, status, state)
    evaluator = _evaluator(events)
    assessment = _assessment(decision) if decision else None
    requirement = str(decision.payload.get("requirement", "")) if decision else ""
    requirement = requirement or (state.request if state else "")
    failure = next((e for e in events if e.event_type is AuditEventType.RUN_FAILED), None)
    started = next((e.ts for e in events if e.event_type is AuditEventType.RUN_STARTED), None)
    ended = next((e.ts for e in reversed(events) if e.event_type in ENDINGS), None)
    return RunView(
        run_id=run_id,
        version=view_version(events[-1].id, state),
        title=(assessment or {}).get("title") or requirement[:60],
        requirement=requirement,
        status=status,
        source=(assessment or {}).get("source"),
        started_at=started,
        finished_at=ended,
        error=str(failure.payload.get("error")) if failure else None,
        assessment=assessment,
        plan=planned.payload if planned else None,
        stages=stages,
        evaluator=evaluator,
        decisions=_decisions(events),
        timeline=_timeline(events),
        pending_review=state.pending_review if state else None,
        totals=_totals(events),
        final_result=_final(status, stages, state, evaluator),
    )


def list_runs(audit: AuditLog, service: RunService, limit: int = 20) -> list[RunSummary]:
    out: list[RunSummary] = []
    for entry in audit.recent_runs(limit):
        state = None if entry.rejected else load_state(service, entry.run_id)
        if state is not None:
            status = state.status.value
        else:
            status = "rejected" if entry.rejected else "queued"
        out.append(
            RunSummary(
                run_id=entry.run_id,
                title=entry.title or (state.request[:60] if state else entry.run_id),
                created=entry.created,
                status=status,
            )
        )
    return out
