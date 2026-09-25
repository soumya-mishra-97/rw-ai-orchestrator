from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from sdlc_loop.config import Settings
from sdlc_loop.demo.library import example_script
from sdlc_loop.llm.fake_client import ScriptedLLMClient
from sdlc_loop.scenarios import get_scenario
from sdlc_loop.services import RunService, build_container

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "scenarios" / "golden_scenarios.jsonl"

KEYS = [
    "completeness",
    "correctness_feasibility",
    "legacy_constraint_awareness",
    "clarity_actionability",
    "traceability",
]


def judge(
    *scores: int, feedback: str = "specific feedback", issues: list[str] | None = None
) -> dict[str, Any]:
    return {
        "dimension_scores": dict(zip(KEYS, scores, strict=True)),
        "blocking_issues": issues or [],
        "feedback": feedback,
    }


@pytest.fixture
def script() -> dict[str, list[Any]]:
    """A fresh copy of the curated Scenario 1 fixture."""
    return example_script("fraud-mainframe")


@pytest.fixture
def scenario1_request() -> str:
    return get_scenario("1", SCENARIOS).request


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def factory(**overrides: Any) -> Settings:
        return Settings(_env_file=None, data_dir=tmp_path / "var", **overrides)  # type: ignore[call-arg]

    return factory


ServiceFactory = Callable[..., tuple[RunService, ScriptedLLMClient]]


@pytest.fixture
def make_service(make_settings: Callable[..., Settings]) -> Iterator[ServiceFactory]:
    containers = []

    def factory(
        script: dict[str, list[Any]], **overrides: Any
    ) -> tuple[RunService, ScriptedLLMClient]:
        llm = ScriptedLLMClient(script)
        container = build_container(make_settings(**overrides), llm=llm)
        containers.append(container)
        return RunService(container), llm

    yield factory
    for c in containers:
        c.close()
