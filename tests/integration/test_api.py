from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sdlc_loop.api.app import create_app
from sdlc_loop.config import Settings
from sdlc_loop.llm.fake_client import ScriptedLLMClient
from sdlc_loop.services import Container, build_container
from tests.conftest import judge

KEYS = "op-key:operator,rev-key:reviewer,aud-key:auditor"
OP, REV, AUD = ({"X-API-Key": k} for k in ("op-key", "rev-key", "aud-key"))


@pytest.fixture
def client_for(
    make_settings: Callable[..., Settings], script: dict[str, list[Any]]
) -> Iterator[Callable[..., TestClient]]:
    containers: list[Container] = []

    def factory(api_keys: str = "", escalate: bool = False) -> TestClient:
        if escalate:
            critical = judge(3, 2, 1, 3, 3)
            script["judge:architect_to_dev:tier1"] = [critical]
            script["judge:architect_to_dev:tier2"] = [critical]
        container = build_container(make_settings(api_keys=api_keys), llm=ScriptedLLMClient(script))
        containers.append(container)
        return TestClient(create_app(container))

    yield factory
    for c in containers:
        c.close()


def test_start_and_inspect_run_open_mode(client_for: Callable[..., TestClient]) -> None:
    client = client_for()
    resp = client.post("/runs", json={"scenario_id": "1"})
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]
    run = client.get(f"/runs/{run_id}").json()  # background task already ran in TestClient
    assert run["status"] == "complete"
    assert len(run["evaluations"]) == 5
    audit = client.get(f"/runs/{run_id}/audit").json()
    assert audit[0]["event_type"] == "run_started"


def test_validation_and_not_found(client_for: Callable[..., TestClient]) -> None:
    client = client_for()
    assert client.post("/runs", json={}).status_code == 422
    assert client.post("/runs", json={"scenario_id": "99"}).status_code == 404
    assert client.get("/runs/nope").status_code == 404


def test_role_based_access_on_resume(client_for: Callable[..., TestClient]) -> None:
    client = client_for(api_keys=KEYS, escalate=True)
    assert client.post("/runs", json={"scenario_id": "1"}).status_code == 401
    assert client.post("/runs", json={"scenario_id": "1"}, headers=AUD).status_code == 403
    run_id = client.post("/runs", json={"scenario_id": "1"}, headers=OP).json()["run_id"]
    run = client.get(f"/runs/{run_id}", headers=AUD).json()
    assert run["status"] == "blocked_for_human"
    assert run["pending_review"]["handoff"] == "architect_to_dev"

    body = {"action": "accept_as_is"}
    assert client.post(f"/runs/{run_id}/resume", json=body, headers=OP).status_code == 403
    assert client.post(f"/runs/{run_id}/resume", json=body, headers=REV).status_code == 202
    run = client.get(f"/runs/{run_id}", headers=REV).json()
    assert run["status"] == "complete"
    assert run["human_decisions"][0]["reviewer"].startswith("reviewer:")
    # resuming a run that is no longer blocked is a conflict
    assert client.post(f"/runs/{run_id}/resume", json=body, headers=REV).status_code == 409


def test_retry_without_guidance_is_rejected(client_for: Callable[..., TestClient]) -> None:
    client = client_for(escalate=True)
    run_id = client.post("/runs", json={"scenario_id": "1"}).json()["run_id"]
    assert client.post(f"/runs/{run_id}/resume", json={"action": "retry"}).status_code == 422
