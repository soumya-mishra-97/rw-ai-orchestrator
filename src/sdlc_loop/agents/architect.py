"""Solutions Architect: RequirementsPackage + brief constraints -> TechnicalDesign.

Always routed to Tier 2: a design mistake is the most expensive one to catch
downstream (it propagates into code and tests).
"""

from __future__ import annotations

from sdlc_loop.agents.base import PersonaAgent
from sdlc_loop.agents.context import ContextSection, constraints_section, dump
from sdlc_loop.graph.state import SDLCState
from sdlc_loop.schemas.artifacts import TechnicalDesign
from sdlc_loop.schemas.common import Persona


class ArchitectAgent(PersonaAgent):
    persona = Persona.ARCHITECT
    output_model = TechnicalDesign
    max_tokens = 16000  # includes adaptive-thinking tokens on Tier 2

    def context_sections(self, state: SDLCState) -> list[ContextSection]:
        req = state.get("requirements")
        assert req is not None, "Architect requires a RequirementsPackage"
        return [ContextSection("requirements_package", dump(req)), *constraints_section(state)]
