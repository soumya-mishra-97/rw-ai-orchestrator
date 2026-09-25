"""Demo-mode content: curated example runs, and the client that routes each run
to either its curated fixture or the simulator.

The curated fixtures are hand-authored in the exact shape of model output; they
exercise specific evaluator behaviours (a critical legacy-constraint violation,
a borderline score confirmed by the Tier-2 judge). Every other requirement is
handled by :class:`~sdlc_loop.demo.simulator.SimulatedLLMClient`.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Any, Literal

from sdlc_loop.demo.simulator import SimulatedLLMClient
from sdlc_loop.llm.fake_client import ScriptedLLMClient
from sdlc_loop.llm.types import LLMClient, LLMRequest, LLMResponse

SIMULATOR = "simulator"
RunSource = Literal["claude", "example", "simulator"]

SCENARIO_1_REQUEST = (
    "Add real-time fraud scoring to our card payments flow. Problem: our core payments engine "
    "is a 15-year-old COBOL system running on a mainframe, processing transactions in nightly "
    "batch settlement windows only. There is no public API, no webhook mechanism, and no "
    "real-time hooks anywhere in the system. The vendor no longer offers custom development on "
    "this platform. Compliance requires every fraud decision to be logged and explainable. We "
    "need a way to score transactions for fraud risk before they settle, without a multi-year "
    "mainframe replacement project."
)


@dataclass(frozen=True, slots=True)
class DemoExample:
    id: str
    title: str
    requirement: str
    script_file: str
    highlights: str


EXAMPLES: tuple[DemoExample, ...] = (
    DemoExample(
        id="leave-management",
        title="Leave Management System",
        requirement="Build a Leave Management System",
        script_file="leave_management_script.json",
        highlights="BA attempt 1 scores borderline (3.2); the Tier-2 judge confirms and the "
        "gate retries; attempt 2 passes.",
    ),
    DemoExample(
        id="fraud-mainframe",
        title="Fraud scoring on a mainframe (golden scenario 1)",
        requirement=SCENARIO_1_REQUEST,
        script_file="scenario1_script.json",
        highlights="Architect attempt 1 assumes a webhook and REST API on a batch-only "
        "mainframe; the evaluator raises a critical flag and forces a Tier-2 retry.",
    ),
)


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().rstrip(".").lower()


@cache
def _fixture_text(file_name: str) -> str:
    return resources.files("sdlc_loop.demo").joinpath(file_name).read_text("utf-8")


def example_script(example_id: str) -> dict[str, list[Any]]:
    """A fresh copy of a curated fixture (keys starting with ``_`` are notes)."""
    example = next(e for e in EXAMPLES if e.id == example_id)
    data: dict[str, list[Any]] = json.loads(_fixture_text(example.script_file))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def example_client(example_id: str = "fraud-mainframe") -> ScriptedLLMClient:
    return ScriptedLLMClient(example_script(example_id))


class DemoLibrary:
    def __init__(self, examples: tuple[DemoExample, ...] = EXAMPLES) -> None:
        self.examples = examples

    def match(self, requirement: str) -> DemoExample | None:
        wanted = normalise(requirement)
        return next((e for e in self.examples if normalise(e.requirement) == wanted), None)


class DemoLLMClient:
    """Routes each run to the curated fixture or the simulator the orchestrator chose.

    ``playback_delay_s`` pauses before each response so the browser can show the
    stages progressing; replayed calls still report a latency of 0.
    """

    def __init__(self, library: DemoLibrary, *, playback_delay_s: float = 0.0) -> None:
        self.library = library
        self._delay = playback_delay_s
        self._clients: dict[str, LLMClient] = {
            e.id: ScriptedLLMClient(example_script(e.id)) for e in library.examples
        }
        self._clients[SIMULATOR] = SimulatedLLMClient()
        self._assigned: dict[str, str] = {}
        self._lock = threading.Lock()

    def assign(self, run_id: str, source_id: str) -> None:
        """``source_id`` is an example id or :data:`SIMULATOR`."""
        if source_id not in self._clients:
            raise KeyError(f"unknown demo source {source_id!r}")
        with self._lock:
            self._assigned[run_id] = source_id

    def complete(self, request: LLMRequest) -> LLMResponse:
        with self._lock:
            source_id = self._assigned.get(request.tags.run_id)
        if source_id is None:
            raise LookupError(f"run {request.tags.run_id} has no demo source assigned")
        if self._delay:
            time.sleep(self._delay)
        return self._clients[source_id].complete(request)
