"""Golden scenario loading (scenarios/golden_scenarios.jsonl)."""

from __future__ import annotations

from pathlib import Path

from sdlc_loop.schemas.common import StrictModel

DEFAULT_SCENARIOS = Path("scenarios/golden_scenarios.jsonl")


class Scenario(StrictModel):
    id: str
    title: str
    request: str


def load_scenarios(path: Path = DEFAULT_SCENARIOS) -> list[Scenario]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [Scenario.model_validate_json(line) for line in lines if line.strip()]


def get_scenario(scenario_id: str, path: Path = DEFAULT_SCENARIOS) -> Scenario:
    for scenario in load_scenarios(path):
        if scenario.id == scenario_id:
            return scenario
    raise KeyError(f"unknown scenario {scenario_id!r} in {path}")
