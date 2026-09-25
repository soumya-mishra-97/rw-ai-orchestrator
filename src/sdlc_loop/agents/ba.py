"""Business Analyst: ProblemBrief -> RequirementsPackage."""

from __future__ import annotations

from sdlc_loop.agents.base import PersonaAgent
from sdlc_loop.agents.context import ContextSection, dump
from sdlc_loop.graph.state import SDLCState
from sdlc_loop.schemas.artifacts import RequirementsPackage
from sdlc_loop.schemas.common import Persona


class BAAgent(PersonaAgent):
    persona = Persona.BA
    output_model = RequirementsPackage
    max_tokens = 8000

    def context_sections(self, state: SDLCState) -> list[ContextSection]:
        brief = state.get("problem_brief")
        assert brief is not None, "BA requires a ProblemBrief"
        return [ContextSection("problem_brief", dump(brief))]
