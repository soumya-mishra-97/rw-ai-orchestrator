"""Application service shared by the CLI, the API and the eval harness:
start a run, inspect it, resume it after human review."""

from __future__ import annotations

import logging
import threading
import uuid
from collections import defaultdict
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from pydantic import BaseModel

from sdlc_loop.governance.audit import AuditEvent, AuditEventType
from sdlc_loop.governance.hashing import sha256_hex
from sdlc_loop.graph.stages import STAGES
from sdlc_loop.logging_setup import run_id_var
from sdlc_loop.schemas.common import RunStatus, StrictModel
from sdlc_loop.schemas.evaluation import EvaluationRecord
from sdlc_loop.schemas.review import HumanDecision, ReviewRequest
from sdlc_loop.services.container import Container

log = logging.getLogger(__name__)


class RunNotFoundError(LookupError):
    pass


class InvalidRunStateError(RuntimeError):
    pass


class RunState(StrictModel):
    """Checkpoint state only: cheap to read on every poll."""

    run_id: str
    scenario_id: str | None
    request: str  # PII-redacted requirement text
    status: RunStatus
    pending_review: ReviewRequest | None
    artifacts: dict[str, Any]
    evaluations: list[EvaluationRecord]
    human_decisions: list[HumanDecision]
    retry_counts: dict[str, int]


class RunSnapshot(RunState):
    """Checkpoint state plus token and cost totals from the audit log."""

    total_cost_usd: float
    total_input_tokens: int
    total_output_tokens: int


class RunService:
    def __init__(self, container: Container) -> None:
        self._c = container
        self._locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
        self._active: set[str] = set()  # runs whose graph is executing in this process
        self._active_lock = threading.Lock()

    @staticmethod
    def _config(run_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 100}

    def new_run_id(self) -> str:
        return f"run-{uuid.uuid4().hex[:12]}"

    def start(
        self, request: str, *, scenario_id: str | None = None, run_id: str | None = None
    ) -> RunSnapshot:
        run_id = run_id or self.new_run_id()
        redaction = self._c.redactor.redact(request)
        s = self._c.settings
        self._c.audit.append(
            AuditEvent(
                run_id=run_id,
                event_type=AuditEventType.RUN_STARTED,
                input_hash=sha256_hex(request),
                payload={
                    "scenario_id": scenario_id,
                    "tier1_model": s.tier1_model,
                    "tier2_model": s.tier2_model,
                    "levers": {
                        "context_policy": s.context_policy.value,
                        "routing_policy": s.routing_policy.value,
                        "tiered_evaluator": s.tiered_evaluator,
                        "prompt_caching": s.prompt_caching,
                        "deterministic_pregate": s.deterministic_pregate,
                        "critical_policy": s.critical_policy.value,
                    },
                },
            )
        )
        if redaction.redacted:
            self._c.audit.append(
                AuditEvent(
                    run_id=run_id,
                    event_type=AuditEventType.PII_REDACTED,
                    payload={"entity_counts": redaction.counts},
                )
            )
        initial: dict[str, Any] = {
            "run_id": run_id,
            "scenario_id": scenario_id,
            "request": redaction.text,
            "evaluations": [],
            "human_decisions": [],
            "retry_counts": {},
            "tier2_forced": {},
            "critical_retry_used": {},
            "feedback_history": {},
            "human_guidance": {},
            "invalid_output": {},
            "pending_handoff": None,
            "last_verdict": None,
            "status": RunStatus.RUNNING,
        }
        self._invoke(run_id, initial)
        return self.get(run_id)

    def resume(self, run_id: str, decision: HumanDecision) -> RunSnapshot:
        snap = self.state(run_id)
        if snap.status is not RunStatus.BLOCKED_FOR_HUMAN or snap.pending_review is None:
            raise InvalidRunStateError(f"run {run_id} is {snap.status}, not awaiting review")
        if decision.action == "accept_as_is" and not snap.pending_review.artifact:
            raise InvalidRunStateError("nothing to accept: the stage produced no valid artifact")
        self._invoke(run_id, Command(resume=decision.model_dump(mode="json")))
        return self.get(run_id)

    def state(self, run_id: str) -> RunState:
        snapshot = self._c.graph.get_state(self._config(run_id))
        values: dict[str, Any] = dict(snapshot.values or {})
        if not values:
            raise RunNotFoundError(run_id)
        pending: ReviewRequest | None = None
        if snapshot.interrupts:
            pending = ReviewRequest.model_validate(snapshot.interrupts[0].value)
        status = RunStatus(values.get("status", RunStatus.RUNNING))
        if pending is not None:
            status = RunStatus.BLOCKED_FOR_HUMAN
        elif status is RunStatus.BLOCKED_FOR_HUMAN:
            # The gate has decided to escalate but the review node has not paused the
            # graph yet: report "running" until the review request actually exists.
            status = RunStatus.RUNNING
        if status is RunStatus.RUNNING and not snapshot.next and not self.is_active(run_id):
            # Nothing is executing it and nothing is left to run: it stopped mid-way.
            # (While a run executes, LangGraph can briefly expose a checkpoint with no
            # pending node between steps, so this is only trusted for inactive runs.)
            status = RunStatus.FAILED
        artifacts = {
            s.artifact_key: v.model_dump(mode="json")
            for s in STAGES
            if isinstance(v := values.get(s.artifact_key), BaseModel)
        }
        return RunState(
            run_id=run_id,
            scenario_id=values.get("scenario_id"),
            request=values.get("request", ""),
            status=status,
            pending_review=pending,
            artifacts=artifacts,
            evaluations=list(values.get("evaluations") or []),
            human_decisions=list(values.get("human_decisions") or []),
            retry_counts=dict(values.get("retry_counts") or {}),
        )

    def get(self, run_id: str) -> RunSnapshot:
        state = self.state(run_id)
        events = self._c.audit.events(run_id)
        return RunSnapshot(
            **state.model_dump(),
            total_cost_usd=round(sum(e.cost_usd for e in events), 6),
            total_input_tokens=sum(
                e.input_tokens + e.cache_read_tokens + e.cache_write_tokens for e in events
            ),
            total_output_tokens=sum(e.output_tokens for e in events),
        )

    def is_active(self, run_id: str) -> bool:
        with self._active_lock:
            return run_id in self._active

    def _invoke(self, run_id: str, payload: Any) -> None:
        token = run_id_var.set(run_id)
        with self._active_lock:
            self._active.add(run_id)
        try:
            with self._locks[run_id]:
                self._c.graph.invoke(payload, self._config(run_id))
        except Exception as exc:
            log.exception("run failed")
            self._c.audit.append(
                AuditEvent(
                    run_id=run_id,
                    event_type=AuditEventType.RUN_FAILED,
                    payload={"error": f"{type(exc).__name__}: {exc}"},
                )
            )
            raise
        finally:
            with self._active_lock:
                self._active.discard(run_id)
            run_id_var.reset(token)
