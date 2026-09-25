"""Evaluator discrimination benchmark + full-pipeline anti-pattern check.

    uv run python -m evals.fault_injection judge --repeats 3        # labeled judge benchmark
    uv run python -m evals.fault_injection pipeline                  # inject flawed Architect into a live run

``judge`` measures what matters most about a judge: does it reject seeded
defects (recall on bad artifacts) without rejecting good ones (false-rejection
rate), and how consistent is it across repeats. ``pipeline`` is the spec's
anti-pattern check: a real Scenario 1 run whose Architect attempt 1 is replaced
by a design that assumes a REST API on the mainframe; it passes if the gate
does not PASS it.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import BaseModel

from evals.fault_cases import FaultCase, build_cases, upstream
from evals.report import splice
from sdlc_loop.agents import AgentDeps, EvaluatorAgent
from sdlc_loop.config import Settings
from sdlc_loop.demo.library import example_client
from sdlc_loop.governance.pii import RegexRedactor
from sdlc_loop.graph.routing import GatePolicy, GateState
from sdlc_loop.graph.state import SDLCState
from sdlc_loop.llm.anthropic_client import AnthropicLLMClient
from sdlc_loop.llm.cache import CachePolicy
from sdlc_loop.llm.fake_client import FaultInjectingLLMClient
from sdlc_loop.llm.model_router import ModelRouter
from sdlc_loop.llm.token_meter import compute_cost
from sdlc_loop.llm.types import LLMClient
from sdlc_loop.logging_setup import configure_logging
from sdlc_loop.scenarios import get_scenario
from sdlc_loop.schemas.artifacts import (
    Implementation,
    ProblemBrief,
    RequirementsPackage,
    TechnicalDesign,
)
from sdlc_loop.schemas.common import Handoff, RunStatus
from sdlc_loop.schemas.evaluation import Verdict
from sdlc_loop.schemas.review import HumanDecision
from sdlc_loop.services import RunService, build_container

app = typer.Typer(add_completion=False)
ROOT = Path(__file__).resolve().parent.parent
_MODELS: dict[Handoff, tuple[str, type[BaseModel]]] = {
    Handoff.PM_TO_BA: ("problem_brief", ProblemBrief),
    Handoff.BA_TO_ARCHITECT: ("requirements", RequirementsPackage),
    Handoff.ARCHITECT_TO_DEV: ("design", TechnicalDesign),
    Handoff.DEV_TO_QA: ("implementation", Implementation),
}


def _state_for(case: FaultCase, run_id: str) -> tuple[SDLCState, Any]:
    up = upstream()
    state: SDLCState = {
        "run_id": run_id,
        "request": RegexRedactor()
        .redact(get_scenario("1", ROOT / "scenarios" / "golden_scenarios.jsonl").request)
        .text,
        "problem_brief": ProblemBrief.model_validate(up["brief"]),
        "requirements": RequirementsPackage.model_validate(up["requirements"]),
        "design": TechnicalDesign.model_validate(up["design"]),
        "implementation": Implementation.model_validate(up["implementation"]),
        "evaluations": [],
    }
    key, model = _MODELS[case.handoff]
    artifact = model.model_validate(case.artifact)
    state[key] = artifact  # type: ignore[literal-required]
    return state, artifact


@app.command()
def judge(
    repeats: Annotated[int, typer.Option(min=1)] = 3,
    offline: bool = False,
    out: Path = ROOT / "evals" / "reports",
    update_docs: bool = False,
) -> None:
    """Run every labeled case through the evaluator ``repeats`` times."""
    configure_logging("WARNING", json_output=False)
    settings = Settings()
    llm: LLMClient = example_client() if offline else AnthropicLLMClient()
    deps = AgentDeps(llm=llm, router=ModelRouter(settings), cache=CachePolicy(enabled=True),
                     settings=settings)  # fmt: skip
    evaluator = EvaluatorAgent(deps, GatePolicy.from_settings(settings))
    rows: list[dict[str, Any]] = []
    for case in build_cases():
        for r in range(repeats):
            state, artifact = _state_for(case, f"judge-{case.id}-{r}")
            result = evaluator.evaluate(case.handoff, artifact, state, attempt=1, gate=GateState())
            rec = result.record
            rows.append({
                "case": case.id, "expected": case.expected, "handoff": case.handoff.label,
                "verdict": rec.verdict.value, "judge": rec.judge,
                "overall": rec.overall_score, "critical": rec.critical_flag,
                "legacy_score": rec.dimension_scores.legacy_constraint_awareness
                if rec.dimension_scores else None,
                "correct": (rec.verdict is Verdict.PASS) == (case.expected == "pass"),
                "judge_calls": len(result.records),
                "cost_usd": round(sum(
                    compute_cost(c.response.usage, c.route.spec, batched=c.response.batched).actual_usd
                    for c in result.records
                ), 6),
                "feedback": rec.feedback[:300],
            })  # fmt: skip
            typer.echo(f"{case.id:32} run {r + 1}: {rec.verdict.value:8} ({rec.judge})")

    bad = [r for r in rows if r["expected"] == "fail"]
    good = [r for r in rows if r["expected"] == "pass"]
    per_case: dict[str, Counter[str]] = {}
    for row in rows:
        per_case.setdefault(row["case"], Counter())[row["verdict"]] += 1
    unstable = sum(1 for c in per_case.values() if len(c) > 1)
    summary = {
        "mode": "offline (scripted — NOT a measurement)" if offline else "live",
        "repeats": repeats,
        "defect_detection_rate": round(sum(r["correct"] for r in bad) / len(bad), 3),
        "false_rejection_rate": round(sum(not r["correct"] for r in good) / len(good), 3),
        "accuracy": round(sum(r["correct"] for r in rows) / len(rows), 3),
        "cases_with_inconsistent_verdicts": unstable,
        "total_judge_cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "by_case": {k: dict(v) for k, v in per_case.items()},
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = out / f"{stamp}-judge-benchmark{'-offline' if offline else ''}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2), encoding="utf-8")
    typer.echo(json.dumps(summary, indent=2))
    typer.echo(f"written {target}")
    if update_docs and not offline:
        rel = target.relative_to(ROOT) if target.is_relative_to(ROOT) else target
        splice(ROOT / "docs" / "evaluation.md", "JUDGE_RESULTS", _judge_markdown(summary, rel))
        typer.echo("docs/evaluation.md updated")


def _judge_markdown(summary: dict[str, Any], source: Path) -> str:
    cases = {c.id: c for c in build_cases()}
    lines = [
        f"Source: `{source}` — {summary['repeats']} repeats per case.",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Defect detection rate (bad artifacts not passed) | {summary['defect_detection_rate']:.0%} |",
        f"| False-rejection rate (good artifacts not passed) | {summary['false_rejection_rate']:.0%} |",
        f"| Accuracy | {summary['accuracy']:.0%} |",
        f"| Cases with inconsistent verdicts across repeats | {summary['cases_with_inconsistent_verdicts']} |",
        f"| Judge cost for the whole benchmark | ${summary['total_judge_cost_usd']:.4f} |",
        "",
        "| Case | Expected | Verdicts | Seeded defect |",
        "|---|---|---|---|",
    ]
    for case_id, verdicts in summary["by_case"].items():
        shown = ", ".join(f"{v}×{n}" for v, n in verdicts.items())
        lines.append(
            f"| {case_id} | {cases[case_id].expected} | {shown} | {cases[case_id].defect} |"
        )
    return "\n".join(lines) + "\n"


@app.command()
def pipeline(offline: bool = False) -> None:
    """Inject the flawed Architect design into attempt 1 of a real Scenario 1 run."""
    configure_logging("WARNING", json_output=False)
    flawed = next(c for c in build_cases() if c.id == "F3-design-rest-webhook").artifact
    inner: LLMClient = example_client() if offline else AnthropicLLMClient()
    llm = FaultInjectingLLMClient(inner, {("persona:architect", 1): flawed})
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    settings = Settings(data_dir=ROOT / "evals" / "reports" / f"{stamp}-anti-pattern")
    service = RunService(build_container(settings, llm=llm))
    scenario = get_scenario("1", ROOT / "scenarios" / "golden_scenarios.jsonl")
    snap = service.start(scenario.request, scenario_id="1", run_id=f"anti-pattern-{stamp}")
    while snap.status is RunStatus.BLOCKED_FOR_HUMAN:
        snap = service.resume(snap.run_id, HumanDecision(action="abort", reviewer="eval-harness"))
    arch = [e for e in snap.evaluations if e.handoff is Handoff.ARCHITECT_TO_DEV]
    first = arch[0] if arch else None
    caught = first is not None and first.verdict is not Verdict.PASS
    typer.echo(f"injected: {llm.injected}")
    for e in arch:
        typer.echo(f"attempt {e.attempt_number}: {e.verdict.value} overall={e.overall_score} "
                   f"critical={e.critical_flag} judge={e.judge}\n  {e.feedback[:300]}")  # fmt: skip
    typer.echo(
        f"ANTI-PATTERN CHECK: {'CAUGHT' if caught else 'MISSED'}; final status {snap.status}"
    )
    if not caught:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
