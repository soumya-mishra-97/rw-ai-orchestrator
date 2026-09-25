"""Prompt files.

Prompts live in version-controlled ``.md`` files (never inline strings) so that
every prompt change shows up as a reviewable diff.
"""

from __future__ import annotations

from functools import cache
from importlib import resources

from sdlc_loop.schemas.common import Persona

_PERSONA_FILES: dict[Persona, str] = {
    Persona.ORCHESTRATOR: "orchestrator.md",
    Persona.PM: "pm.md",
    Persona.BA: "ba.md",
    Persona.ARCHITECT: "architect.md",
    Persona.DEV: "dev.md",
    Persona.QA: "qa.md",
    Persona.EVALUATOR: "evaluator.md",
}


@cache
def _read(name: str) -> str:
    return resources.files(__package__).joinpath(name).read_text(encoding="utf-8").strip()


def persona_prompt(persona: Persona) -> str:
    return _read(_PERSONA_FILES[persona])


@cache
def shared_preamble() -> str:
    """The byte-identical prefix sent first on *every* call.

    Keeping it identical across personas lets one cache entry serve the whole
    pipeline on a given model (caches are model-scoped, so Tier 1 and Tier 2
    each hold their own entry).
    """
    return f"{_read('shared_preamble.md')}\n\n{_read('rubric.md')}"
