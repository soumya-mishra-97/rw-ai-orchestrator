"""Turns agent call records into audit events, with cost and counterfactuals.

Each ``persona_call`` row carries the numbers the cost write-up is built from:
actual cost, cost without caching, cost without the batch discount, cost if
the same tokens had run on Tier 2, and the estimated full-history context size.
"""

from __future__ import annotations

from typing import Any

from sdlc_loop.agents.base import CallRecord
from sdlc_loop.governance.audit import AuditEvent, AuditEventType, AuditLog
from sdlc_loop.llm.model_router import ModelRouter
from sdlc_loop.llm.models import get_model_spec
from sdlc_loop.llm.token_meter import compute_cost, reprice


def record_calls(
    audit: AuditLog,
    router: ModelRouter,
    records: list[CallRecord],
    *,
    handoff: str | None = None,
    output_payload: dict[str, Any] | None = None,
) -> float:
    """Append one audit row per LLM call; return the total actual cost."""
    total = 0.0
    for i, rec in enumerate(records):
        resp = rec.response
        try:
            spec = get_model_spec(resp.model_id)
        except ValueError:
            spec = rec.route.spec  # API returned a dated alias we do not list
        cost = compute_cost(resp.usage, spec, batched=resp.batched)
        total += cost.actual_usd
        is_last = i == len(records) - 1
        payload: dict[str, Any] = {
            "purpose": rec.tags.purpose,
            "tier": rec.route.tier.value,
            "route_reason": rec.route.reason,
            "stop_reason": resp.stop_reason,
            "request_id": resp.request_id,
            "batched": resp.batched,
            "parse_error": rec.parse_error,
            "cache_eligible": rec.cache_eligible,
            "minimal_context_tokens": rec.minimal_context_tokens,
            "naive_context_tokens": rec.naive_context_tokens,
            "cost_sync_usd": cost.sync_usd,
            "cost_no_cache_sync_usd": cost.no_cache_sync_usd,
            "cost_all_tier2_usd": reprice(resp.usage, router.tier2),
        }
        if is_last and output_payload is not None and rec.ok:
            payload["output"] = output_payload
        elif not rec.ok:
            payload["raw_output"] = resp.text[:20000]
        audit.append(
            AuditEvent(
                run_id=rec.tags.run_id,
                event_type=AuditEventType.PERSONA_CALL if rec.ok else AuditEventType.PARSE_FAILURE,
                persona=rec.tags.persona.value,
                handoff=handoff or rec.tags.handoff,
                attempt=rec.tags.attempt,
                model=resp.model_id,
                input_hash=rec.input_hash,
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                cache_write_tokens=resp.usage.cache_creation_input_tokens,
                cache_read_tokens=resp.usage.cache_read_input_tokens,
                cost_usd=cost.actual_usd,
                latency_ms=resp.latency_ms,
                payload=payload,
            )
        )
    return total
