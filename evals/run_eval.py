"""Run the golden scenarios N times and report Section 8/9 metrics.

    uv run python -m evals.run_eval --trials 3                       # live, synchronous
    uv run python -m evals.run_eval --trials 3 --batch               # live, Batch API (50% off)
    uv run python -m evals.run_eval --offline --trials 2             # scripted, no API key
    uv run python -m evals.run_eval --label no-cache --no-prompt-caching   # ablation

Each invocation writes a self-contained directory under ``evals/reports/``:
``audit.db`` (the evidence), ``metrics.json``, ``runs.json`` and ``report.md``.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from evals.metrics import compute_metrics
from evals.report import render_cost, render_eval, render_header, splice
from sdlc_loop.config import ContextPolicy, CriticalPolicy, RoutingPolicy, Settings
from sdlc_loop.demo.library import example_client
from sdlc_loop.llm.batch_client import BatchingLLMClient
from sdlc_loop.llm.types import LLMClient
from sdlc_loop.logging_setup import configure_logging
from sdlc_loop.scenarios import Scenario, load_scenarios
from sdlc_loop.schemas.common import RunStatus
from sdlc_loop.schemas.review import HumanDecision, ReviewAction
from sdlc_loop.services import RunService, build_container

app = typer.Typer(add_completion=False)
log = logging.getLogger("evals")
ROOT = Path(__file__).resolve().parent.parent


def _run_one(
    service: RunService, scenario: Scenario, trial: int, prefix: str, on_escalation: ReviewAction
) -> dict[str, Any]:
    run_id = f"{prefix}-s{scenario.id}-t{trial}"
    try:
        snap = service.start(scenario.request, scenario_id=scenario.id, run_id=run_id)
        escalations = 0
        while snap.status is RunStatus.BLOCKED_FOR_HUMAN:
            escalations += 1
            # Stand-in reviewer so downstream stages are still measured; the run
            # still counts as "escalated" in every metric.
            snap = service.resume(
                run_id, HumanDecision(action=on_escalation, reviewer="eval-harness")
            )
        return {
            "run_id": run_id,
            "scenario": scenario.id,
            "trial": trial,
            "status": snap.status.value,
            "escalations": escalations,
            "retry_counts": snap.retry_counts,
            "cost_usd": snap.total_cost_usd,
        }
    except Exception as exc:
        log.exception("run %s failed", run_id)
        return {"run_id": run_id, "scenario": scenario.id, "trial": trial, "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"}  # fmt: skip


@app.command()
def main(
    trials: Annotated[int, typer.Option(min=1)] = 3,
    scenarios: Annotated[str, typer.Option(help="Comma-separated scenario ids.")] = "1,2,3",
    offline: Annotated[bool, typer.Option(help="Scripted backend; Scenario 1 only.")] = False,
    batch: Annotated[bool, typer.Option(help="Route every call through the Batch API.")] = False,
    concurrency: Annotated[int, typer.Option(min=1)] = 3,
    label: str = "default",
    context_policy: ContextPolicy = ContextPolicy.MINIMAL,
    routing_policy: RoutingPolicy = RoutingPolicy.ROUTED,
    critical_policy: CriticalPolicy = CriticalPolicy.FAST_TRACK,
    prompt_caching: bool = True,
    tiered_evaluator: bool = True,
    pregate: bool = True,
    execute_code: Annotated[bool, typer.Option(help="Run generated tests (sandbox!).")] = False,
    on_escalation: Annotated[str, typer.Option(help="accept_as_is | abort")] = "accept_as_is",
    out: Path = ROOT / "evals" / "reports",
    update_docs: Annotated[bool, typer.Option(help="Splice results into docs/*.md")] = False,
) -> None:
    configure_logging("INFO", json_output=False)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    mode = "offline" if offline else "batch" if batch else "live"
    out_dir = out / f"{stamp}-{mode}-{label}"
    settings = Settings(
        data_dir=out_dir,
        context_policy=context_policy,
        routing_policy=routing_policy,
        critical_policy=critical_policy,
        prompt_caching=prompt_caching,
        tiered_evaluator=tiered_evaluator,
        deterministic_pregate=pregate,
        execute_generated_code=execute_code,
    )
    selected = [s for s in load_scenarios(ROOT / "scenarios" / "golden_scenarios.jsonl")
                if s.id in scenarios.split(",")]  # fmt: skip
    if offline and any(s.id != "1" for s in selected):
        typer.echo("offline script only covers Scenario 1 — restricting to it")
        selected = [s for s in selected if s.id == "1"]

    batch_client = BatchingLLMClient() if batch and not offline else None
    llm: LLMClient | None = example_client() if offline else batch_client
    container = build_container(settings, llm=llm)
    service = RunService(container)
    jobs = [(s, t) for s in selected for t in range(1, trials + 1)]
    workers = len(jobs) if batch_client else concurrency
    action: ReviewAction = "abort" if on_escalation == "abort" else "accept_as_is"

    def job(item: tuple[Scenario, int]) -> dict[str, Any]:
        ctx = batch_client.worker() if batch_client else nullcontext()
        with ctx:
            return _run_one(service, item[0], item[1], f"eval-{stamp}", action)

    typer.echo(f"running {len(jobs)} pipeline runs ({mode}) → {out_dir}")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(job, jobs))
    if batch_client:
        batch_client.close()

    metrics = compute_metrics(container.audit.events())
    meta = {
        "Date (UTC)": stamp,
        "Mode": mode.upper() + (" — scripted responses, NOT model quality" if offline else ""),
        "Scenarios × trials": f"{','.join(s.id for s in selected)} × {trials}",
        "Tier 1 / Tier 2": f"`{settings.tier1_model}` / `{settings.tier2_model}`",
        "Levers": f"context={context_policy.value}, routing={routing_policy.value}, "
        f"caching={prompt_caching}, tiered_judge={tiered_evaluator}, pregate={pregate}, "
        f"critical={critical_policy.value}",
        "Evidence": f"`{out_dir.relative_to(ROOT) if out_dir.is_relative_to(ROOT) else out_dir}/audit.db`",
    }
    eval_md = render_header(meta) + "\n" + render_eval(metrics)
    cost_md = render_header(meta) + "\n" + render_cost(metrics.cost)
    (out_dir / "metrics.json").write_text(metrics.model_dump_json(indent=2), encoding="utf-8")
    (out_dir / "runs.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    (out_dir / "report.md").write_text(
        f"# Eval report\n\n{eval_md}\n## Cost\n\n{cost_md}", encoding="utf-8"
    )
    typer.echo(eval_md)
    typer.echo(cost_md)

    if update_docs:
        if offline:
            typer.echo("refusing to write scripted (offline) numbers into the docs")
            raise typer.Exit(1)
        splice(ROOT / "docs" / "evaluation.md", "EVAL_RESULTS", eval_md)
        splice(ROOT / "docs" / "cost-optimization.md", "COST_RESULTS", cost_md)
        splice(ROOT / "README.md", "HEADLINE", render_eval(metrics).split("#### Per handoff")[0])
        typer.echo("docs updated")


if __name__ == "__main__":
    app()
