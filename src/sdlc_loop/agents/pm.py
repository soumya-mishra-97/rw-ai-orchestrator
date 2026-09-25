"""Product Manager: raw business request -> ProblemBrief."""

from __future__ import annotations

from sdlc_loop.agents.base import PersonaAgent
from sdlc_loop.agents.context import ContextSection, request_section
from sdlc_loop.graph.state import SDLCState
from sdlc_loop.schemas.artifacts import ProblemBrief
from sdlc_loop.schemas.common import Persona


class PMAgent(PersonaAgent):
    persona = Persona.PM
    output_model = ProblemBrief
    max_tokens = 4000

    def context_sections(self, state: SDLCState) -> list[ContextSection]:
        return [request_section(state)]
