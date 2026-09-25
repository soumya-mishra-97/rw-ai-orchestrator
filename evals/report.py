"""Markdown rendering of eval metrics, and splicing results into the docs.

Docs contain marker pairs such as ``<!-- BEGIN:EVAL_RESULTS -->`` …
``<!-- END:EVAL_RESULTS -->``; only the text between them is regenerated, so the
hand-written analysis around the numbers is never overwritten.
"""

from __future__ import annotations

import re
from pathlib import Path

from evals.metrics import CostSummary, EvalMetrics


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def _num(v: float | None, fmt: str = "{:.1f}") -> str:
    return "—" if v is None else fmt.format(v)


def _usd(v: float) -> str:
    return f"${v:.4f}"


def _saving(before: float, after: float) -> str:
    if before <= 0:
        return "—"
    return f"{(before - after) / before * 100:.0f}%"


def render_header(meta: dict[str, str]) -> str:
    rows = "\n".join(f"| {k} | {v} |" for k, v in meta.items())
    return f"| Run property | Value |\n|---|---|\n{rows}\n"


def render_eval(m: EvalMetrics) -> str:
    c = m.cost
    headline = f"""| Metric | Value |
|---|---|
| Pipeline runs | {m.runs} |
| End-to-end completion without human escalation | {_pct(m.completion_rate_without_human)} ({m.completed_without_human}/{m.runs}) |
| Runs escalated to a human at least once | {m.escalated_runs} |
| Runs failed (infrastructure/parse) | {m.failed_runs} |
| Wall-clock per full run, p50 / p95 | {_num(m.wall_clock_p50_s)} s / {_num(m.wall_clock_p95_s)} s |
| Mean tokens per run (input + output) | {m.mean_tokens_per_run:,.0f} |
| Mean cost per run | {_usd(c.mean_cost_per_run_usd)} |
| Schema parse-failure rate (per LLM call) | {_pct(c.parse_failure_rate)} ({c.parse_failures}/{c.persona_calls}) |
| QA: acceptance criteria with no test and not declared untestable (mean per run) | {_num(m.qa_uncovered_criteria_per_run, "{:.2f}")} |
"""
    handoffs = "\n".join(
        f"| {h.handoff} | {h.runs_reaching_gate} | {_pct(h.attempt1_pass_rate)} | "
        f"{_pct(h.retry_rate)} | {_pct(h.escalation_rate)} | "
        f"{_pct(h.eventual_pass_rate_without_human)} | {_num(h.mean_attempts, '{:.2f}')} | "
        f"{h.critical_flags} | {h.deterministic_rejections} | {h.judge_confirmed_by_tier2} |"
        for h in m.handoffs
    )
    personas = "\n".join(
        f"| {p.persona} | {p.calls} | {p.tier2_calls} | {_num(p.latency_p50_ms, '{:,.0f}')} | "
        f"{_num(p.latency_p95_ms, '{:,.0f}')} | {p.mean_input_tokens_per_run:,.0f} | "
        f"{p.mean_output_tokens_per_run:,.0f} | {_usd(p.mean_cost_per_run_usd)} |"
        for p in m.personas
    )
    return (
        "#### Headline\n\n" + headline + "\n#### Per handoff (evaluator gate)\n\n"
        "| Handoff | Runs reaching gate | Pass @ attempt 1 | Retry rate | Escalation rate | "
        "Eventual pass w/o human | Mean attempts | Critical flags | Pre-gate rejections | "
        "Tier-2 judge confirmations |\n|---|---|---|---|---|---|---|---|---|---|\n"
        + handoffs
        + "\n\n#### Per persona (latency is per LLM call; batched calls excluded)\n\n"
        "| Persona | Calls | Tier-2 calls | p50 ms | p95 ms | Input tok / run | "
        "Output tok / run | $ / run |\n|---|---|---|---|---|---|---|---|\n" + personas + "\n"
    )


def render_cost(c: CostSummary) -> str:
    rows = [
        (
            "Model routing (Tier 1 default, Tier 2 by risk)",
            f"All on Tier 2: {_usd(c.all_tier2_usd)}",
            f"Routed: {_usd(c.no_cache_sync_usd)}",
            _saving(c.all_tier2_usd, c.no_cache_sync_usd),
            f"{c.persona_calls_tier1} Tier-1 / {c.persona_calls_tier2} Tier-2 persona calls; "
            "counterfactual re-prices the same tokens",
        ),
        (
            "Prompt caching (shared preamble + persona prompt)",
            f"Uncached: {_usd(c.no_cache_sync_usd)}",
            f"Cached: {_usd(c.sync_usd)}",
            _saving(c.no_cache_sync_usd, c.sync_usd),
            f"cache hit rate {_pct(c.cache_hit_rate)} of input tokens "
            f"({c.cache_read_tokens:,} read / {c.cache_write_tokens:,} written)",
        ),
        (
            "Batch API (eval runs only)",
            f"Synchronous: {_usd(c.sync_usd)}",
            f"Billed: {_usd(c.actual_usd)}",
            _saving(c.sync_usd, c.actual_usd),
            f"{c.batched_calls} of {c.persona_calls} calls batched",
        ),
        (
            "Minimal context passing (tokens, estimated)",
            f"Full history: {c.naive_context_tokens:,} tok",
            f"Minimal: {c.minimal_context_tokens:,} tok",
            _saving(c.naive_context_tokens, c.minimal_context_tokens),
            "user-message tokens; ~4 chars/token estimate for both sides",
        ),
        (
            "Tiered evaluator",
            f"{c.judge_evaluations} judged handoffs on Tier 2",
            f"{c.judge_resolved_tier1_only} resolved on Tier 1 alone",
            _pct(c.judge_resolved_tier1_only / c.judge_evaluations) if c.judge_evaluations else "—",
            f"{c.judge_confirmed_tier2} borderline/pre-escalation re-scores on Tier 2",
        ),
        (
            "Deterministic pre-gate",
            "judge call per failing handoff",
            f"{c.deterministic_gate_rejections} rejections in code",
            f"{c.deterministic_gate_rejections} judge calls avoided",
            "structural defects rejected for 0 tokens",
        ),
        (
            "Structured JSON outputs",
            "free-form JSON",
            f"parse failures {c.parse_failures}/{c.persona_calls}",
            _pct(c.parse_failure_rate) + " wasted calls",
            "each parse failure = one repair call",
        ),
    ]
    body = "\n".join(f"| {a} | {b} | {d} | {e} | {f} |" for a, b, d, e, f in rows)
    return (
        "| Lever | Before | After | Saving | Evidence |\n|---|---|---|---|---|\n"
        + body
        + f"\n\n**Net:** {_usd(c.all_tier2_usd)} (naive: everything on Tier 2, no cache, "
        f"synchronous) → {_usd(c.actual_usd)} billed "
        f"({_saving(c.all_tier2_usd, c.actual_usd)} lower) across {c.runs} runs; "
        f"{_usd(c.mean_cost_per_run_usd)} per run.\n"
    )


def splice(path: Path, marker: str, content: str) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"(<!-- BEGIN:{marker} -->\n).*?(\n<!-- END:{marker} -->)", flags=re.DOTALL
    )
    if not pattern.search(text):
        raise ValueError(f"marker {marker} not found in {path}")
    path.write_text(
        pattern.sub(lambda m: m.group(1) + content + m.group(2), text), encoding="utf-8"
    )
