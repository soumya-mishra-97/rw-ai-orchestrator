"""QA: design + implementation + acceptance criteria -> TestPlan.

Before the model is called, the Dev agent's code is verified for real
(:class:`~sdlc_loop.tools.code_verifier.CodeVerifier`). The measured report is
shown to the model *and* attached to the final TestPlan by code, so pass/fail
numbers can never be invented by the model.
"""

from __future__ import annotations

from pydantic import BaseModel

from sdlc_loop.agents.base import AgentDeps, PersonaAgent
from sdlc_loop.agents.context import ContextSection, acceptance_criteria_section, dump
from sdlc_loop.graph.state import SDLCState
from sdlc_loop.schemas.artifacts import ExecutionReport, TestPlan, TestPlanDraft
from sdlc_loop.schemas.common import Persona
from sdlc_loop.tools.code_verifier import CodeVerifier


class QAAgent(PersonaAgent):
    persona = Persona.QA
    output_model = TestPlanDraft
    max_tokens = 12000

    def __init__(self, deps: AgentDeps, verifier: CodeVerifier) -> None:
        super().__init__(deps)
        self._verifier = verifier

    def context_sections(self, state: SDLCState) -> list[ContextSection]:
        design, impl, req = (
            state.get("design"),
            state.get("implementation"),
            state.get("requirements"),
        )
        assert design and impl and req, "QA requires design, implementation and requirements"
        return [
            ContextSection("technical_design", dump(design)),
            ContextSection("implementation", dump(impl)),
            acceptance_criteria_section(req),
        ]

    def prepare(self, state: SDLCState) -> tuple[list[ContextSection], BaseModel | None]:
        impl = state.get("implementation")
        assert impl is not None, "QA requires an implementation"
        report = self._verifier.verify(impl)
        return [ContextSection("measured_execution_report", dump(report))], report

    def finalize(self, draft: BaseModel, measured: BaseModel | None) -> BaseModel:
        assert isinstance(draft, TestPlanDraft)
        report = measured if isinstance(measured, ExecutionReport) else None
        return TestPlan(**draft.model_dump(), execution=report)
