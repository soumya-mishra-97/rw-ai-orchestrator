"""One module per persona plus the evaluator; :func:`build_personas` is the registry."""

from __future__ import annotations

from sdlc_loop.agents.architect import ArchitectAgent
from sdlc_loop.agents.ba import BAAgent
from sdlc_loop.agents.base import AgentDeps, PersonaAgent, PersonaResult, RetryContext
from sdlc_loop.agents.dev import DevAgent
from sdlc_loop.agents.evaluator import EvaluatorAgent, GateResult
from sdlc_loop.agents.pm import PMAgent
from sdlc_loop.agents.qa import QAAgent
from sdlc_loop.schemas.common import Persona
from sdlc_loop.tools.code_verifier import CodeVerifier


def build_personas(deps: AgentDeps, verifier: CodeVerifier) -> dict[Persona, PersonaAgent]:
    return {
        Persona.PM: PMAgent(deps),
        Persona.BA: BAAgent(deps),
        Persona.ARCHITECT: ArchitectAgent(deps),
        Persona.DEV: DevAgent(deps),
        Persona.QA: QAAgent(deps, verifier),
    }


__all__ = [
    "AgentDeps",
    "EvaluatorAgent",
    "GateResult",
    "PersonaAgent",
    "PersonaResult",
    "RetryContext",
    "build_personas",
]
