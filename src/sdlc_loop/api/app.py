"""FastAPI application: the browser UI, its JSON API and the programmatic run API.

Browser UI (same origin, so no CORS):

    GET  /                          the single-page UI (src/sdlc_loop/web/static)
    GET  /api/config                mode, agents, gates, rubric, models, example requirements
    POST /api/orchestrate           feature requirement → Master Orchestrator (operator)
                                    202 dispatched · 422 invalid (with reasons)
    GET  /api/runs                  run history (auditor)
    GET  /api/runs/{id}/view        everything the UI renders; ?since=<version> → 204 if unchanged
    GET  /api/runs/{id}/stream      the same view pushed as Server-Sent Events on every change
    GET  /api/runs/{id}/report.pdf  SDLC Execution Report as a formatted PDF (auditor)

Programmatic API:

    POST /runs                      start a golden scenario / raw request (operator)
    GET  /runs/{id}                 run state and totals (auditor)
    GET  /runs/{id}/audit           audit trail (auditor)
    POST /runs/{id}/resume          human-in-the-loop decision (reviewer)

Swagger UI is at /docs and ReDoc at /redoc.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, model_validator

from sdlc_loop.api.auth import RequireReviewer, parse_keys, require
from sdlc_loop.demo.library import EXAMPLES, DemoLLMClient
from sdlc_loop.graph.stages import STAGES
from sdlc_loop.llm.types import LLMError
from sdlc_loop.orchestrator import (
    AGENT_PROFILES,
    MasterOrchestrator,
    OrchestratorMode,
    RunSummary,
    RunView,
    build_view,
    list_runs,
)
from sdlc_loop.orchestrator.view import is_settled, load_state, view_version
from sdlc_loop.reports import render_run_report, report_filename
from sdlc_loop.scenarios import get_scenario
from sdlc_loop.schemas.common import Persona, StrictModel
from sdlc_loop.schemas.evaluation import Dimension
from sdlc_loop.schemas.review import HumanDecision, ReviewAction
from sdlc_loop.services import Container, RunNotFoundError, RunService, RunSnapshot

log = logging.getLogger(__name__)
STATIC_DIR = Path(str(resources.files("sdlc_loop.web").joinpath("static")))

# The UI loads nothing from other origins and runs no inline script. Swagger and
# ReDoc load their assets from a CDN, so those pages are exempt.
_UI_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


class StartRunRequest(StrictModel):
    scenario_id: str | None = None
    request: str | None = None

    @model_validator(mode="after")
    def _one_of(self) -> StartRunRequest:
        if (self.scenario_id is None) == (self.request is None):
            raise ValueError("provide exactly one of scenario_id or request")
        return self


class OrchestrateRequest(StrictModel):
    requirement: str = Field(max_length=10_000)


class ResumeRequest(StrictModel):
    action: ReviewAction
    guidance: str | None = None


class Accepted(StrictModel):
    run_id: str
    status_url: str


class ExampleOut(StrictModel):
    id: str
    title: str
    requirement: str
    highlights: str


class AgentOut(StrictModel):
    persona: str
    short: str
    name: str
    description: str
    receives: str
    produces: str
    gate: str | None  # evaluator gate that scores this agent's output


class LabelOut(StrictModel):
    id: str
    label: str


class ModelOut(StrictModel):
    id: str
    name: str


class ConfigOut(StrictModel):
    mode: OrchestratorMode
    mode_reason: str
    auth_required: bool
    examples: list[ExampleOut]
    agents: list[AgentOut]
    handoffs: list[LabelOut]
    dimensions: list[LabelOut]
    tier1_model: ModelOut
    tier2_model: ModelOut
    pass_rule: str
    max_attempts_per_handoff: int


def _run_safely(fn: Callable[[], object]) -> None:
    """Background-task wrapper: failures are already audited as ``run_failed``."""
    try:
        fn()
    except Exception:  # noqa: BLE001 - never crash the worker; the audit log has the error
        log.warning("background run task ended with an error (see audit log)")


def create_app(
    container: Container, *, demo: DemoLLMClient | None = None, mode_note: str | None = None
) -> FastAPI:
    """``demo`` given → demo mode (curated examples + simulator); otherwise live Claude calls."""
    service = RunService(container)
    mode = OrchestratorMode.OFFLINE if demo is not None else OrchestratorMode.LIVE
    orchestrator = MasterOrchestrator(container, service, mode=mode, demo=demo)
    app = FastAPI(
        title="Robert Walters AI-Orchestrator",
        version="0.3.0",
        description="Multi-agent SDLC pipeline (PM → BA → Architect → Developer → QA) "
        "with an evaluator gate at every handoff.",
    )
    app.state.api_keys = parse_keys(container.settings.api_keys)
    app.state.mode_note = mode_note or (
        "Demo mode." if demo is not None else "Live mode (Claude API)."
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if not request.url.path.startswith(_DOC_PATHS):
            response.headers.setdefault("Content-Security-Policy", _UI_CSP)
        return response

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        return {"status": "ok", "mode": mode.value}

    _register_ui_routes(app, container, service, orchestrator)
    _register_run_routes(app, container, service)
    return app


def _config(app: FastAPI, container: Container, orchestrator: MasterOrchestrator) -> ConfigOut:
    router, settings = container.router, container.settings
    gates = {s.persona: s.handoff.label if s.handoff else None for s in STAGES}
    personas = [s.persona for s in STAGES] + [Persona.EVALUATOR]
    agents = []
    for persona in personas:
        p = AGENT_PROFILES[persona]
        agents.append(
            AgentOut(
                persona=persona.value,
                short=p.short,
                name=p.name,
                description=p.description,
                receives=p.receives,
                produces=p.produces,
                gate=gates.get(persona),
            )
        )
    return ConfigOut(
        mode=orchestrator.mode,
        mode_reason=app.state.mode_note,
        auth_required=bool(app.state.api_keys),
        examples=[
            ExampleOut(id=e.id, title=e.title, requirement=e.requirement, highlights=e.highlights)
            for e in EXAMPLES
        ],
        agents=agents,
        handoffs=[LabelOut(id=s.handoff.value, label=s.handoff.label) for s in STAGES if s.handoff],
        dimensions=[LabelOut(id=d.value, label=d.label) for d in Dimension],
        tier1_model=ModelOut(id=router.tier1.model_id, name=router.tier1.display_name),
        tier2_model=ModelOut(id=router.tier2.model_id, name=router.tier2.display_name),
        pass_rule=f"mean ≥ {settings.pass_threshold}, every dimension ≥ "
        f"{settings.min_dimension_score}, no dimension scored 1",
        max_attempts_per_handoff=settings.max_attempts,
    )


def _register_ui_routes(
    app: FastAPI, container: Container, service: RunService, orchestrator: MasterOrchestrator
) -> None:
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/config", tags=["ui"])
    def config() -> ConfigOut:
        return _config(app, container, orchestrator)

    @app.post("/api/orchestrate", tags=["ui"], dependencies=[Depends(require("operator"))])
    def orchestrate(body: OrchestrateRequest, background: BackgroundTasks) -> JSONResponse:
        try:
            result = orchestrator.submit(body.requirement)
        except LLMError as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, f"requirement triage unavailable: {exc}"
            ) from exc
        payload = result.model_dump(mode="json")
        if not result.assessment.valid:
            return JSONResponse(payload, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)
        background.add_task(_run_safely, lambda: orchestrator.execute(result))
        return JSONResponse(payload, status_code=status.HTTP_202_ACCEPTED)

    @app.get("/api/runs", tags=["ui"], dependencies=[Depends(require("auditor"))])
    def runs() -> list[RunSummary]:
        return list_runs(container.audit, service)

    @app.get(
        "/api/runs/{run_id}/view",
        tags=["ui"],
        dependencies=[Depends(require("auditor"))],
        responses={
            200: {"model": RunView},
            204: {"description": "Unchanged since the given version"},
        },
    )
    def run_view(run_id: str, since: str | None = None) -> Response:
        _require_run(container, run_id)
        _, view = _view_if_changed(container, service, run_id, since)
        if view is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return JSONResponse(view.model_dump(mode="json"), headers={"Cache-Control": "no-store"})

    @app.get("/api/runs/{run_id}/stream", tags=["ui"], dependencies=[Depends(require("auditor"))])
    async def run_stream(request: Request, run_id: str) -> StreamingResponse:
        """Server-Sent Events: one ``view`` event per change, ``end`` once the run settles."""
        await run_in_threadpool(_require_run, container, run_id)

        async def events() -> AsyncIterator[str]:
            since: str | None = None
            quiet = 0.0
            while not await request.is_disconnected():
                version, view = await run_in_threadpool(
                    _view_if_changed, container, service, run_id, since
                )
                if view is not None:
                    since, quiet = version, 0.0
                    yield f"event: view\nid: {version}\ndata: {view.model_dump_json()}\n\n"
                    if is_settled(view):
                        yield "event: end\ndata: {}\n\n"
                        return
                elif quiet >= _KEEPALIVE_S:
                    quiet = 0.0
                    yield ": keep-alive\n\n"
                await asyncio.sleep(_STREAM_TICK_S)
                quiet += _STREAM_TICK_S

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get(
        "/api/runs/{run_id}/report.pdf",
        tags=["ui"],
        dependencies=[Depends(require("auditor"))],
        response_class=Response,
        responses={200: {"content": {"application/pdf": {}}, "description": "PDF report"}},
    )
    def run_report(run_id: str) -> Response:
        """SDLC Execution Report: the run view rendered as a print-ready PDF."""
        _require_run(container, run_id)
        view = build_view(run_id, container.audit, service)
        return Response(
            render_run_report(view),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{report_filename(run_id)}"',
                "Cache-Control": "no-store",
            },
        )


_STREAM_TICK_S = 0.25
_KEEPALIVE_S = 15.0


def _require_run(container: Container, run_id: str) -> None:
    if container.audit.last_event_id(run_id) == 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"run {run_id} not found")


def _view_if_changed(
    container: Container, service: RunService, run_id: str, since: str | None
) -> tuple[str, RunView | None]:
    """Cheap change check (one indexed query + checkpoint read); full view only if changed."""
    state = load_state(service, run_id)
    version = view_version(container.audit.last_event_id(run_id), state)
    if since is not None and since == version:
        return version, None
    return version, build_view(run_id, container.audit, service, state)


def _register_run_routes(app: FastAPI, container: Container, service: RunService) -> None:
    @app.post(
        "/runs",
        tags=["runs"],
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require("operator"))],
    )
    def start_run(body: StartRunRequest, background: BackgroundTasks) -> Accepted:
        if body.scenario_id is not None:
            try:
                text = get_scenario(body.scenario_id).request
            except KeyError as exc:
                raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        else:
            assert body.request is not None
            text = body.request
        run_id = service.new_run_id()
        background.add_task(
            _run_safely, lambda: service.start(text, scenario_id=body.scenario_id, run_id=run_id)
        )
        return Accepted(run_id=run_id, status_url=f"/runs/{run_id}")

    @app.get("/runs/{run_id}", tags=["runs"], dependencies=[Depends(require("auditor"))])
    def get_run(run_id: str) -> RunSnapshot:
        try:
            return service.get(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"run {run_id} not found") from exc

    @app.get("/runs/{run_id}/audit", tags=["runs"], dependencies=[Depends(require("auditor"))])
    def get_audit(run_id: str) -> list[dict[str, Any]]:
        return [e.model_dump(mode="json") for e in container.audit.events(run_id)]

    @app.post("/runs/{run_id}/resume", tags=["runs"], status_code=status.HTTP_202_ACCEPTED)
    def resume_run(
        run_id: str, body: ResumeRequest, background: BackgroundTasks, principal: RequireReviewer
    ) -> Accepted:
        try:
            decision = HumanDecision(
                action=body.action, guidance=body.guidance, reviewer=principal.name
            )
            state = service.state(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"run {run_id} not found") from exc
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        if state.pending_review is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"run is {state.status}, not awaiting review"
            )
        if decision.action == "accept_as_is" and not state.pending_review.artifact:
            raise HTTPException(status.HTTP_409_CONFLICT, "nothing to accept")

        background.add_task(_run_safely, lambda: service.resume(run_id, decision))
        return Accepted(run_id=run_id, status_url=f"/runs/{run_id}")
