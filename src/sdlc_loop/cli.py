"""Command-line entry point (installed as ``sdlc``).

    sdlc serve                            # web UI + API on :8000 (auto: live if a key works)
    sdlc serve --mode demo --reload       # development server without an API key
    sdlc run --scenario 1                 # one pipeline run in the terminal (live)
    sdlc run --scenario 1 --offline       # same, replaying the curated Scenario 1 example
    sdlc resume RUN_ID --action retry --guidance "Use the nightly extract"
    sdlc show RUN_ID | sdlc audit RUN_ID | sdlc verify-audit | sdlc scenarios

With no subcommand (e.g. an IDE's plain "Run Python File") it runs the offline
Scenario 1 demo instead of erroring.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sdlc_loop.config import Settings
from sdlc_loop.demo.library import example_client
from sdlc_loop.governance.audit import AuditLog
from sdlc_loop.governance.pii import build_redactor
from sdlc_loop.logging_setup import configure_logging
from sdlc_loop.runtime import load_environment
from sdlc_loop.scenarios import DEFAULT_SCENARIOS, get_scenario, load_scenarios
from sdlc_loop.schemas.common import RunStatus
from sdlc_loop.schemas.review import HumanDecision, ReviewAction
from sdlc_loop.services import RunService, RunSnapshot, build_container

app = typer.Typer(add_completion=False, help="Multi-agent SDLC loop with an evaluator agent.")
console = Console()

OfflineOpt = Annotated[
    bool, typer.Option("--offline", help="Replay the curated Scenario 1 example (no API key).")
]


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Multi-agent SDLC loop with an evaluator agent."""
    if ctx.invoked_subcommand is not None:
        return
    console.print(
        "[yellow]No command given[/] — run e.g. [bold]sdlc run --scenario 1 --offline[/], or "
        "[bold]sdlc --help[/] for the full list. Falling back to the offline demo so you see "
        "some output:\n"
    )
    run(scenario="1", request=None, offline=True, interactive=False, output=None)


def _service(offline: bool) -> RunService:
    load_environment()
    settings = Settings()
    configure_logging(settings.log_level, json_output=settings.log_json)
    return RunService(build_container(settings, llm=example_client() if offline else None))


def _print_snapshot(snap: RunSnapshot) -> None:
    table = Table(title=f"Run {snap.run_id} — {snap.status.value}", show_lines=False)
    for col in ("handoff", "attempt", "judge", "overall", "critical", "verdict"):
        table.add_column(col, no_wrap=True)
    table.add_column("feedback", overflow="ellipsis", no_wrap=True, max_width=70)
    for ev in snap.evaluations:
        table.add_row(
            ev.handoff.label,
            str(ev.attempt_number),
            ev.judge + (" (confirmed)" if ev.judge_escalated else ""),
            f"{ev.overall_score:.2f}" if ev.overall_score is not None else "—",
            "yes" if ev.critical_flag else "",
            ev.verdict.value.upper(),
            ev.feedback[:160],
        )
    console.print(table)
    console.print(
        f"cost ${snap.total_cost_usd:.4f} · input tokens {snap.total_input_tokens:,} · "
        f"output tokens {snap.total_output_tokens:,} · retries {snap.retry_counts}"
    )


def _prompt_review(service: RunService, snap: RunSnapshot) -> RunSnapshot:
    while snap.status is RunStatus.BLOCKED_FOR_HUMAN and snap.pending_review:
        review = snap.pending_review
        console.print(
            Panel(
                f"[bold]{review.handoff.label}[/] escalated after {review.attempts_so_far} "
                f"attempt(s) (score {review.overall_score}, critical={review.critical_flag})\n\n"
                f"{review.evaluator_feedback}\n\n"
                + "\n".join(f"• {i}" for i in review.blocking_issues),
                title="Human review required",
                border_style="yellow",
            )
        )
        action: ReviewAction = typer.prompt("Decision [accept_as_is/retry/abort]", default="abort")
        guidance = typer.prompt("Guidance for the retry") if action == "retry" else None
        snap = service.resume(
            snap.run_id, HumanDecision(action=action, guidance=guidance, reviewer="cli-user")
        )
        _print_snapshot(snap)
    return snap


@app.command()
def run(
    scenario: Annotated[str, typer.Option(help="Scenario id from the golden set.")] = "1",
    request: Annotated[str | None, typer.Option(help="Free-text request instead.")] = None,
    offline: OfflineOpt = False,
    interactive: Annotated[bool, typer.Option(help="Prompt for human review.")] = True,
    output: Annotated[Path | None, typer.Option(help="Write final artifacts JSON here.")] = None,
) -> None:
    """Run one scenario through PM → BA → Architect → Dev → QA."""
    service = _service(offline)
    text = request or get_scenario(scenario).request
    try:
        snap = service.start(text, scenario_id=None if request else scenario)
    except Exception as exc:
        console.print(f"[red]Run failed:[/] {type(exc).__name__}: {exc}")
        console.print(
            "Live runs need Claude API credentials (ANTHROPIC_API_KEY in the environment or "
            ".env, or `ant auth login`). Use --offline to replay the curated Scenario 1 example."
        )
        raise typer.Exit(1) from exc
    _print_snapshot(snap)
    if interactive:
        snap = _prompt_review(service, snap)
    elif snap.status is RunStatus.BLOCKED_FOR_HUMAN:
        console.print(f"[yellow]Blocked for human review. Resume with: sdlc resume {snap.run_id}")
    if output:
        output.write_text(json.dumps(snap.model_dump(mode="json"), indent=2), encoding="utf-8")
        console.print(f"artifacts written to {output}")


@app.command()
def resume(
    run_id: str,
    action: Annotated[str, typer.Option(help="accept_as_is | retry | abort")],
    guidance: Annotated[str | None, typer.Option()] = None,
    reviewer: Annotated[str, typer.Option()] = "cli-user",
    offline: OfflineOpt = False,
) -> None:
    """Resume a run that is blocked for human review."""
    service = _service(offline)
    decision = HumanDecision.model_validate(
        {"action": action, "guidance": guidance, "reviewer": reviewer}
    )
    _print_snapshot(service.resume(run_id, decision))


@app.command()
def show(run_id: str, offline: OfflineOpt = False) -> None:
    """Show a run's evaluations and cost."""
    _print_snapshot(_service(offline).get(run_id))


def _audit_log() -> AuditLog:
    settings = Settings()
    return AuditLog(settings.audit_db, build_redactor(settings.pii_backend))


@app.command()
def audit(run_id: str) -> None:
    """Print the audit trail of a run."""
    table = Table(title=f"Audit trail {run_id}")
    for col in ("id", "event", "persona", "handoff", "att", "model", "verdict", "tokens", "$"):
        table.add_column(col)
    for e in _audit_log().events(run_id):
        table.add_row(
            str(e.id), e.event_type.value, e.persona or "", e.handoff or "",
            str(e.attempt or ""), e.model or "", e.verdict or "",
            f"{e.input_tokens + e.cache_read_tokens + e.cache_write_tokens}/{e.output_tokens}",
            f"{e.cost_usd:.4f}",
        )  # fmt: skip
    console.print(table)


@app.command("verify-audit")
def verify_audit() -> None:
    """Verify the audit log's hash chain has not been tampered with."""
    ok, bad = _audit_log().verify_chain()
    if ok:
        console.print("[green]audit chain intact")
    else:
        console.print(f"[red]audit chain broken at row {bad}")
        raise typer.Exit(1)


@app.command()
def scenarios(path: Path = DEFAULT_SCENARIOS) -> None:
    """List the golden scenarios."""
    for s in load_scenarios(path):
        console.print(f"[bold]{s.id}[/] {s.title}")


def port_in_use(host: str, port: int) -> bool:
    """True if something is already listening on ``host:port``."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    mode: Annotated[
        str, typer.Option(help="auto (live if Anthropic credentials work) | live | demo")
    ] = "auto",
    offline: Annotated[bool, typer.Option("--offline", help="Alias for --mode demo.")] = False,
    reload: Annotated[bool, typer.Option(help="Restart on code changes (development).")] = False,
) -> None:
    """Start the web UI and API, then open http://HOST:PORT in a browser."""
    import os

    import uvicorn

    if mode not in {"auto", "live", "demo"}:
        raise typer.BadParameter("mode must be auto, live or demo")
    if port_in_use(host, port):
        console.print(
            f"[red]Port {port} is already in use.[/] Another copy of the server is probably "
            f"running.\n  Stop it:      lsof -ti tcp:{port} | xargs kill\n"
            f"  Or use another port:  sdlc serve --port {port + 1}   (make start PORT={port + 1})"
        )
        raise typer.Exit(1)
    # The factory reads the mode from the environment so --reload workers see it too.
    os.environ["SDLC_MODE"] = "demo" if offline else mode
    console.print(
        f"[bold]Robert Walters AI-Orchestrator:[/] http://{host}:{port}   API docs: /docs"
    )
    uvicorn.run(
        "sdlc_loop.api.main:app_factory",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        reload_dirs=["src"] if reload else None,
    )


if __name__ == "__main__":
    app()
