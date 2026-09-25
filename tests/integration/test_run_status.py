"""Regression: a run must never be reported "failed" while it is still executing.

Between two graph steps LangGraph can expose a checkpoint with no pending node.
The status logic used to read that as "stopped mid-way", so the browser briefly
saw a healthy run as failed and stopped polling.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from sdlc_loop.config import Settings
from sdlc_loop.demo.library import DemoLibrary, DemoLLMClient
from sdlc_loop.schemas import RunStatus
from sdlc_loop.services import RunService, build_container
from tests.conftest import ServiceFactory


def _between_steps(*_: Any, **__: Any) -> SimpleNamespace:
    return SimpleNamespace(values={"status": RunStatus.RUNNING}, next=(), interrupts=[])


def test_status_rule_distinguishes_active_from_stopped_runs(
    make_service: ServiceFactory, script: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = make_service(script)
    monkeypatch.setattr(service._c.graph, "get_state", _between_steps)
    assert service.state("r1").status is RunStatus.FAILED  # nobody is running it
    service._active.add("r1")
    assert service.state("r1").status is RunStatus.RUNNING  # mid-execution transient


def test_polling_a_live_run_never_sees_failed(make_settings: Callable[..., Settings]) -> None:
    demo = DemoLLMClient(DemoLibrary(), playback_delay_s=0.05)
    container = build_container(make_settings(), llm=demo)
    service = RunService(container)
    demo.assign("r1", "simulator")
    worker = threading.Thread(
        target=lambda: service.start("Build an invoice approval app", run_id="r1")
    )
    worker.start()
    seen: set[RunStatus] = set()
    while worker.is_alive():
        with contextlib.suppress(LookupError):  # no checkpoint yet
            seen.add(service.state("r1").status)
        time.sleep(0.002)
    worker.join()
    final = service.state("r1").status
    container.close()
    assert RunStatus.FAILED not in seen
    assert final is RunStatus.COMPLETE
