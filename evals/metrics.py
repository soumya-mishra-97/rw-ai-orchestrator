"""Section 8 / Section 9 metrics, computed purely from audit-log rows.

Nothing here talks to a model: the audit trail is the dataset, so every number
in the docs can be recomputed from the committed ``audit.db`` of an eval run.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from sdlc_loop.governance.audit import AuditEventType, StoredAuditEvent
from sdlc_loop.schemas.common import Handoff

CALL_EVENTS = {AuditEventType.PERSONA_CALL, AuditEventType.PARSE_FAILURE}


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile (no interpolation — honest for small samples)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return round(ordered[rank - 1], 1)


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


class HandoffStats(BaseModel):
    handoff: str
    runs_reaching_gate: int
    attempt1_pass_rate: float | None
    retry_rate: float | None  # runs with >= 1 RETRY at this gate
    escalation_rate: float | None
    eventual_pass_rate_without_human: float | None
    mean_attempts: float | None
    critical_flags: int
    deterministic_rejections: int
    judge_confirmed_by_tier2: int


class PersonaStats(BaseModel):
    persona: str
    calls: int
    tier2_calls: int
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    mean_input_tokens_per_run: float
    mean_output_tokens_per_run: float
    mean_cost_per_run_usd: float


class CostSummary(BaseModel):
    runs: int
    # Cost waterfall: all_tier2 → no_cache_sync (routing) → sync (+cache) → actual (+batch)
    all_tier2_usd: float  # same tokens re-priced on Tier 2, uncached, synchronous
    no_cache_sync_usd: float  # routed, uncached, synchronous
    sync_usd: float  # routed + cached, synchronous
    actual_usd: float  # what was billed
    mean_cost_per_run_usd: float
    cache_read_tokens: int
    cache_write_tokens: int
    total_input_tokens: int
    cache_hit_rate: float | None  # cache-read tokens / all input tokens
    minimal_context_tokens: int  # estimated, user-message part only
    naive_context_tokens: int  # estimated full-history counterfactual
    persona_calls: int
    parse_failures: int
    parse_failure_rate: float | None
    persona_calls_tier1: int
    persona_calls_tier2: int
    judge_evaluations: int
    judge_resolved_tier1_only: int
    judge_confirmed_tier2: int
    deterministic_gate_rejections: int
    batched_calls: int


class EvalMetrics(BaseModel):
    runs: int
    completed_without_human: int
    completion_rate_without_human: float | None
    escalated_runs: int
    failed_runs: int
    wall_clock_p50_s: float | None
    wall_clock_p95_s: float | None
    mean_tokens_per_run: float
    handoffs: list[HandoffStats]
    personas: list[PersonaStats]
    cost: CostSummary
    qa_uncovered_criteria_per_run: float | None


def _by_run(events: Iterable[StoredAuditEvent]) -> dict[str, list[StoredAuditEvent]]:
    runs: dict[str, list[StoredAuditEvent]] = defaultdict(list)
    for e in events:
        runs[e.run_id].append(e)
    return runs


def _handoff_stats(runs: dict[str, list[StoredAuditEvent]]) -> list[HandoffStats]:
    out: list[HandoffStats] = []
    for handoff in Handoff:
        reaching = attempt1_pass = retried = escalated = passed_no_human = 0
        attempts: list[int] = []
        critical = deterministic = confirmed = 0
        for events in runs.values():
            evals = [
                e for e in events
                if e.event_type is AuditEventType.EVALUATION and e.handoff == handoff.value
            ]  # fmt: skip
            if not evals:
                continue
            reaching += 1
            first = evals[0]
            attempt1_pass += first.verdict == "pass"
            retried += any(e.verdict == "retry" for e in evals)
            esc = any(e.verdict == "escalate" for e in evals)
            escalated += esc
            passed_no_human += (not esc) and evals[-1].verdict == "pass"
            attempts.append(len(evals))
            critical += sum(bool(e.payload.get("critical_flag")) for e in evals)
            deterministic += sum(e.payload.get("judge") == "deterministic" for e in evals)
            confirmed += sum(bool(e.payload.get("judge_escalated")) for e in evals)
        out.append(
            HandoffStats(
                handoff=handoff.label,
                runs_reaching_gate=reaching,
                attempt1_pass_rate=_rate(attempt1_pass, reaching),
                retry_rate=_rate(retried, reaching),
                escalation_rate=_rate(escalated, reaching),
                eventual_pass_rate_without_human=_rate(passed_no_human, reaching),
                mean_attempts=round(sum(attempts) / len(attempts), 2) if attempts else None,
                critical_flags=critical,
                deterministic_rejections=deterministic,
                judge_confirmed_by_tier2=confirmed,
            )
        )
    return out


def _persona_stats(runs: dict[str, list[StoredAuditEvent]]) -> list[PersonaStats]:
    calls: dict[str, list[StoredAuditEvent]] = defaultdict(list)
    for events in runs.values():
        for e in events:
            if e.event_type in CALL_EVENTS and e.persona:
                calls[e.persona].append(e)
    n_runs = max(1, len(runs))
    order = ["pm", "ba", "architect", "dev", "qa", "evaluator"]
    out = []
    for persona in sorted(calls, key=lambda p: order.index(p) if p in order else 99):
        cs = calls[persona]
        lat = [
            e.latency_ms for e in cs if e.latency_ms is not None and not e.payload.get("batched")
        ]
        out.append(
            PersonaStats(
                persona=persona,
                calls=len(cs),
                tier2_calls=sum(e.payload.get("tier") == "tier2" for e in cs),
                latency_p50_ms=percentile(lat, 50),
                latency_p95_ms=percentile(lat, 95),
                mean_input_tokens_per_run=round(
                    sum(e.input_tokens + e.cache_read_tokens + e.cache_write_tokens for e in cs)
                    / n_runs,
                    1,
                ),
                mean_output_tokens_per_run=round(sum(e.output_tokens for e in cs) / n_runs, 1),
                mean_cost_per_run_usd=round(sum(e.cost_usd for e in cs) / n_runs, 5),
            )
        )
    return out


def _cost(runs: dict[str, list[StoredAuditEvent]]) -> CostSummary:
    calls = [e for ev in runs.values() for e in ev if e.event_type in CALL_EVENTS]
    evals = [e for ev in runs.values() for e in ev if e.event_type is AuditEventType.EVALUATION]
    persona_calls = [c for c in calls if c.persona != "evaluator"]
    total_input = sum(c.input_tokens + c.cache_read_tokens + c.cache_write_tokens for c in calls)
    cache_read = sum(c.cache_read_tokens for c in calls)
    actual = sum(c.cost_usd for c in calls)

    def p(key: str) -> float:
        return round(sum(float(c.payload.get(key) or 0.0) for c in calls), 6)

    return CostSummary(
        runs=len(runs),
        all_tier2_usd=p("cost_all_tier2_usd"),
        no_cache_sync_usd=p("cost_no_cache_sync_usd"),
        sync_usd=p("cost_sync_usd"),
        actual_usd=round(actual, 6),
        mean_cost_per_run_usd=round(actual / max(1, len(runs)), 5),
        cache_read_tokens=cache_read,
        cache_write_tokens=sum(c.cache_write_tokens for c in calls),
        total_input_tokens=total_input,
        cache_hit_rate=_rate(cache_read, total_input),
        minimal_context_tokens=int(p("minimal_context_tokens")),
        naive_context_tokens=int(p("naive_context_tokens")),
        persona_calls=len(calls),
        parse_failures=sum(c.event_type is AuditEventType.PARSE_FAILURE for c in calls),
        parse_failure_rate=_rate(
            sum(c.event_type is AuditEventType.PARSE_FAILURE for c in calls), len(calls)
        ),
        persona_calls_tier1=sum(c.payload.get("tier") == "tier1" for c in persona_calls),
        persona_calls_tier2=sum(c.payload.get("tier") == "tier2" for c in persona_calls),
        judge_evaluations=sum(e.payload.get("judge") != "deterministic" for e in evals),
        judge_resolved_tier1_only=sum(
            e.payload.get("judge") == "tier1" and not e.payload.get("judge_escalated")
            for e in evals
        ),
        judge_confirmed_tier2=sum(bool(e.payload.get("judge_escalated")) for e in evals),
        deterministic_gate_rejections=sum(e.payload.get("judge") == "deterministic" for e in evals),
        batched_calls=sum(bool(c.payload.get("batched")) for c in calls),
    )


def _wall_clock(events: list[StoredAuditEvent]) -> float | None:
    start = next((e for e in events if e.event_type is AuditEventType.RUN_STARTED), None)
    end = next(
        (e for e in reversed(events)
         if e.event_type in {AuditEventType.RUN_COMPLETED, AuditEventType.RUN_ABORTED}),
        None,
    )  # fmt: skip
    if not start or not end:
        return None
    return (datetime.fromisoformat(end.ts) - datetime.fromisoformat(start.ts)).total_seconds()


def _last_output(events: list[StoredAuditEvent], persona: str) -> dict[str, Any] | None:
    for e in reversed(events):
        if e.event_type is AuditEventType.PERSONA_CALL and e.persona == persona:
            out = e.payload.get("output")
            if isinstance(out, dict):
                return out
    return None


def _uncovered_criteria(events: list[StoredAuditEvent]) -> int | None:
    ba, qa = _last_output(events, "ba"), _last_output(events, "qa")
    if ba is None or qa is None:
        return None
    acs = {ac["id"] for s in ba.get("user_stories", []) for ac in s.get("acceptance_criteria", [])}
    covered = {a for tc in qa.get("test_cases", []) for a in tc.get("acceptance_criterion_ids", [])}
    return len(acs - covered - set(qa.get("untestable_criteria", [])))


def compute_metrics(events: Iterable[StoredAuditEvent]) -> EvalMetrics:
    runs = _by_run(events)
    completed = escalated = failed = 0
    clocks: list[float] = []
    uncovered: list[int] = []
    tokens: list[int] = []
    for evs in runs.values():
        types = {e.event_type for e in evs}
        esc = AuditEventType.ESCALATION in types
        escalated += esc
        failed += AuditEventType.RUN_FAILED in types
        completed += (AuditEventType.RUN_COMPLETED in types) and not esc
        if (wc := _wall_clock(evs)) is not None:
            clocks.append(wc)
        if (u := _uncovered_criteria(evs)) is not None:
            uncovered.append(u)
        tokens.append(
            sum(
                e.input_tokens + e.cache_read_tokens + e.cache_write_tokens + e.output_tokens
                for e in evs
            )
        )
    return EvalMetrics(
        runs=len(runs),
        completed_without_human=completed,
        completion_rate_without_human=_rate(completed, len(runs)),
        escalated_runs=escalated,
        failed_runs=failed,
        wall_clock_p50_s=percentile(clocks, 50),
        wall_clock_p95_s=percentile(clocks, 95),
        mean_tokens_per_run=round(sum(tokens) / max(1, len(tokens)), 1),
        handoffs=_handoff_stats(runs),
        personas=_persona_stats(runs),
        cost=_cost(runs),
        qa_uncovered_criteria_per_run=round(sum(uncovered) / len(uncovered), 2)
        if uncovered
        else None,
    )
