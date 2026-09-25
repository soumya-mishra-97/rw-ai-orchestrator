"""Master Orchestrator + browser view model, end to end through the real graph."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from sdlc_loop.config import Settings
from sdlc_loop.demo.library import DemoLibrary, DemoLLMClient
from sdlc_loop.governance.audit import AuditEventType
from sdlc_loop.llm.fake_client import ScriptedLLMClient
from sdlc_loop.orchestrator import MasterOrchestrator, OrchestratorMode, build_view
from sdlc_loop.orchestrator.master import rule_checks
from sdlc_loop.reports import render_run_report
from sdlc_loop.reports.fonts import BUILTIN_FONTS
from sdlc_loop.schemas import HumanDecision, Persona
from sdlc_loop.services import Container, RunService, build_container
from tests.conftest import judge

LEAVE = "Build a Leave Management System"
TRIAGE_OK = {
    "is_software_requirement": True,
    "title": "Inventory Tracking System",
    "reason": "Asks for a software system to be built.",
    "clarifications_needed": ["Which warehouses?", "Barcode or RFID?"],
}
TRIAGE_NO = {
    "is_software_requirement": False,
    "title": "",
    "reason": "This is a question about the weather, not a software requirement.",
    "clarifications_needed": [],
}


@pytest.fixture
def containers() -> Iterator[list[Container]]:
    made: list[Container] = []
    yield made
    for c in made:
        c.close()


@pytest.fixture
def offline(
    make_settings: Callable[..., Settings], containers: list[Container]
) -> tuple[MasterOrchestrator, RunService, Container]:
    demo = DemoLLMClient(DemoLibrary())
    container = build_container(make_settings(), llm=demo)
    containers.append(container)
    service = RunService(container)
    return (
        MasterOrchestrator(container, service, mode=OrchestratorMode.OFFLINE, demo=demo),
        service,
        container,
    )


@pytest.fixture
def live(
    make_settings: Callable[..., Settings],
    containers: list[Container],
    script: dict[str, list[Any]],
) -> Callable[..., tuple[MasterOrchestrator, RunService, Container]]:
    """'Live' mode with a scripted model, so triage and escalation paths are deterministic."""

    def factory(**overrides: list[Any]) -> tuple[MasterOrchestrator, RunService, Container]:
        s = copy.deepcopy(script) | {"orchestrator:triage": [TRIAGE_OK]} | overrides
        container = build_container(make_settings(), llm=ScriptedLLMClient(s))
        containers.append(container)
        service = RunService(container)
        return (
            MasterOrchestrator(container, service, mode=OrchestratorMode.LIVE),
            service,
            container,
        )

    return factory


def test_rule_checks() -> None:
    assert all(c.passed for c in rule_checks(LEAVE))
    failed = {c.name for c in rule_checks("hi") if not c.passed}
    assert failed == {"length", "word_count"}
    assert {c.name for c in rule_checks("") if not c.passed} == {
        "not_empty",
        "length",
        "word_count",
    }


def test_plan_routes_architect_and_dev_to_tier2(
    offline: tuple[MasterOrchestrator, RunService, Container],
) -> None:
    plan = offline[0].plan()
    assert [s.persona for s in plan.steps] == [
        Persona.PM, Persona.BA, Persona.ARCHITECT, Persona.DEV, Persona.QA,
    ]  # fmt: skip
    models = {s.persona: s.model for s in plan.steps}
    assert models[Persona.ARCHITECT] == models[Persona.DEV] == "claude-sonnet-5"
    assert models[Persona.PM] == "claude-haiku-4-5-20251001"
    assert [s.gate_label for s in plan.steps] == [
        "PM→BA", "BA→Architect", "Architect→Dev", "Dev→QA", None,
    ]  # fmt: skip


def test_leave_management_end_to_end_in_demo_mode(
    offline: tuple[MasterOrchestrator, RunService, Container],
) -> None:
    orchestrator, service, container = offline
    result = orchestrator.submit("  build a leave management system.  ")  # normalised match
    assert result.assessment.valid and result.assessment.demo_example_id == "leave-management"
    assert result.assessment.source == "example"
    assert result.plan is not None
    orchestrator.execute(result)

    view = build_view(result.run_id, container.audit, service)
    assert view.status == "complete"
    assert view.title == "Leave Management System"
    assert [s.status for s in view.stages] == ["passed", "passed", "passed", "passed", "completed"]
    ba = view.stages[1]
    assert [a.evaluation.verdict for a in ba.attempts if a.evaluation] == ["retry", "pass"]
    first = ba.attempts[0].evaluation
    assert first is not None and first.judge_escalated and first.tier1_overall_score == 3.2
    assert ba.attempts[0].output is not None  # the rejected attempt is still visible
    assert view.totals.retries == 1 and view.evaluator.tier2_confirmations == 1
    assert view.final_result is not None
    assert view.final_result.headline == "SDLC plan complete: all 4 evaluator gates passed"
    assert [t.actor for t in view.timeline[:3]] == [
        "Master Orchestrator", "Master Orchestrator", "Workflow engine",
    ]  # fmt: skip
    assert view.timeline[-1].message == "Pipeline complete"
    assert "<PHONE" not in json.dumps(view.model_dump(mode="json"))  # model ids intact


def test_demo_mode_simulates_any_new_requirement(
    offline: tuple[MasterOrchestrator, RunService, Container],
) -> None:
    orchestrator, service, container = offline
    result = orchestrator.submit("Evaluator Rejects PM Output (Retry Logic)")
    assert result.assessment.valid
    assert result.assessment.source == "simulator"
    orchestrator.execute(result)

    view = build_view(result.run_id, container.audit, service)
    assert view.status == "complete" and view.source == "simulator"
    assert [s.status for s in view.stages] == ["passed", "passed", "passed", "passed", "completed"]
    pm = view.stages[0]
    verdicts = [a.evaluation.verdict for a in pm.attempts if a.evaluation]
    assert verdicts == ["retry", "pass"]  # the evaluator rejects the thin first PM draft
    first = pm.attempts[0].evaluation
    assert first is not None and "Expected 3-5 measurable business goals" in first.feedback
    assert [d.decision for d in view.decisions][:2] == ["RETRY", "PASS"]
    assert view.totals.cost_usd == 0  # no model was called
    assert {a.model for s in view.stages for a in s.attempts} == {"local-simulator"}
    assert view.final_result is not None and view.final_result.test_execution is not None


def test_view_version_changes_only_when_the_run_changes(
    offline: tuple[MasterOrchestrator, RunService, Container],
) -> None:
    orchestrator, service, container = offline
    result = orchestrator.submit(LEAVE)
    first = build_view(result.run_id, container.audit, service)
    assert first.status == "queued"
    assert build_view(result.run_id, container.audit, service).version == first.version
    orchestrator.execute(result)
    done = build_view(result.run_id, container.audit, service)
    assert done.version != first.version
    assert done.started_at is not None and done.finished_at is not None
    assert all(s.started_at and s.finished_at for s in done.stages)


def test_live_triage_accepts_and_is_audited(
    live: Callable[..., tuple[MasterOrchestrator, RunService, Container]],
) -> None:
    orchestrator, _, container = live()
    result = orchestrator.submit("Build an inventory tracking system for our warehouse")
    assert result.assessment.valid
    assert result.assessment.title == "Inventory Tracking System"
    assert result.assessment.clarifications == ["Which warehouses?", "Barcode or RFID?"]
    triage = [e for e in container.audit.events(result.run_id) if e.persona == "orchestrator"
              and e.event_type is AuditEventType.PERSONA_CALL]  # fmt: skip
    assert len(triage) == 1 and triage[0].model == "claude-haiku-4-5-20251001"


def test_live_triage_rejects_non_requirements_before_any_agent_runs(
    live: Callable[..., tuple[MasterOrchestrator, RunService, Container]],
) -> None:
    orchestrator, _, container = live(**{"orchestrator:triage": [TRIAGE_NO]})
    result = orchestrator.submit("What is the weather like in Pune today?")
    assert not result.assessment.valid and result.assessment.code == "invalid_requirement"
    assert "weather" in result.assessment.reasons[0]
    with pytest.raises(ValueError, match="invalid"):
        orchestrator.execute(result)
    personas = {e.persona for e in container.audit.events(result.run_id)}
    assert personas == {"orchestrator"}  # no PM/BA/... call was ever made


def test_view_shows_escalation_then_human_override(
    live: Callable[..., tuple[MasterOrchestrator, RunService, Container]],
    script: dict[str, list[Any]],
) -> None:
    critical = judge(3, 2, 1, 3, 3, feedback="assumes a REST API on the mainframe")
    orchestrator, service, container = live(**{
        "persona:architect": [script["persona:architect"][0]],
        "judge:architect_to_dev:tier1": [critical],
        "judge:architect_to_dev:tier2": [critical],
    })  # fmt: skip
    result = orchestrator.submit("Add real-time fraud scoring to our card payments flow")
    orchestrator.execute(result)

    view = build_view(result.run_id, container.audit, service)
    assert view.status == "blocked_for_human"
    assert [s.status for s in view.stages] == [
        "passed", "passed", "awaiting_review", "pending", "pending",
    ]  # fmt: skip
    assert view.pending_review is not None
    assert view.final_result is None
    assert render_run_report(view).startswith(b"%PDF")  # a paused run still has a report

    service.resume(result.run_id, HumanDecision(action="accept_as_is", reviewer="alice"))
    view = build_view(result.run_id, container.audit, service)
    assert view.stages[2].status == "accepted_by_human"
    assert view.final_result is not None
    assert "human override at Architect→Dev" in view.final_result.headline
    assert any(t.actor == "Reviewer (alice)" for t in view.timeline)
    assert render_run_report(view, fonts=BUILTIN_FONTS).startswith(b"%PDF")
