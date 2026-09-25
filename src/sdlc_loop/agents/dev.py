"""Developer: TechnicalDesign (+ stories to choose from) -> Implementation."""

from __future__ import annotations

from sdlc_loop.agents.base import PersonaAgent
from sdlc_loop.agents.context import (
    ContextSection,
    acceptance_criteria_section,
    constraints_section,
    dump,
)
from sdlc_loop.graph.state import SDLCState
from sdlc_loop.schemas.artifacts import Implementation
from sdlc_loop.schemas.common import Persona


class DevAgent(PersonaAgent):
    persona = Persona.DEV
    output_model = Implementation
    max_tokens = 32000  # generated code + adaptive thinking; streamed

    def context_sections(self, state: SDLCState) -> list[ContextSection]:
        design, req = state.get("design"), state.get("requirements")
        assert design is not None and req is not None, "Dev requires a design and requirements"
        return [
            ContextSection("technical_design", dump(design)),
            acceptance_criteria_section(req),
            *constraints_section(state),
        ]
