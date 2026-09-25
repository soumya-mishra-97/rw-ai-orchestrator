"""The browser surface: page, static assets and the /api endpoints the UI calls."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sdlc_loop.api.app import STATIC_DIR, create_app
from sdlc_loop.config import Settings
from sdlc_loop.demo.library import DemoLibrary, DemoLLMClient
from sdlc_loop.services import Container, build_container

LEAVE = "Build a Leave Management System"


@pytest.fixture
def client_for(make_settings: Callable[..., Settings]) -> Iterator[Callable[..., TestClient]]:
    made: list[Container] = []

    def factory(api_keys: str = "") -> TestClient:
        demo = DemoLLMClient(DemoLibrary())
        container = build_container(make_settings(api_keys=api_keys), llm=demo)
        made.append(container)
        return TestClient(create_app(container, demo=demo))

    yield factory
    for c in made:
        c.close()


def test_page_and_assets_are_served(client_for: Callable[..., TestClient]) -> None:
    client = client_for()
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "Feature Requirement" in page.text and "Generate Intelligent SDLC Blueprint" in page.text
    assert "Enter a feature requirement (e.g., Build a Leave Management System)." in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_config_drives_the_ui(client_for: Callable[..., TestClient]) -> None:
    body = client_for().get("/api/config").json()
    assert body["mode"] == "offline"
    assert body["examples"][0]["requirement"] == LEAVE
    assert body["auth_required"] is False
    assert [a["short"] for a in body["agents"]] == ["PM", "BA", "AR", "DEV", "QA", "EV"]
    assert [h["label"] for h in body["handoffs"]] == [
        "PM→BA",
        "BA→Architect",
        "Architect→Dev",
        "Dev→QA",
    ]
    assert len(body["dimensions"]) == 5
    assert body["tier2_model"] == {"id": "claude-sonnet-5", "name": "Claude Sonnet 5"}
    assert [a["gate"] for a in body["agents"]] == [
        "PM→BA",
        "BA→Architect",
        "Architect→Dev",
        "Dev→QA",
        None,
        None,
    ]


def test_orchestrate_then_view(client_for: Callable[..., TestClient]) -> None:
    client = client_for()
    resp = client.post("/api/orchestrate", json={"requirement": LEAVE})
    assert resp.status_code == 202
    body = resp.json()
    assert body["assessment"]["valid"] and len(body["plan"]["steps"]) == 5
    view = client.get(f"/api/runs/{body['run_id']}/view").json()  # background task already ran
    assert view["status"] == "complete"
    assert len(view["stages"]) == 5 and len(view["evaluator"]["evaluations"]) == 5
    assert view["final_result"]["gates_passed"] == 4
    runs = client.get("/api/runs").json()
    assert runs[0]["run_id"] == body["run_id"] and runs[0]["title"] == "Leave Management System"


def test_orchestrate_rejects_invalid_requirement(client_for: Callable[..., TestClient]) -> None:
    resp = client_for().post("/api/orchestrate", json={"requirement": "hi"})
    assert resp.status_code == 422
    assert resp.json()["assessment"]["code"] == "invalid_requirement"


def test_new_requirement_runs_in_demo_mode(client_for: Callable[..., TestClient]) -> None:
    client = client_for()
    resp = client.post("/api/orchestrate", json={"requirement": "Build an invoice approval app"})
    assert resp.status_code == 202
    assert resp.json()["assessment"]["source"] == "simulator"
    view = client.get(f"/api/runs/{resp.json()['run_id']}/view").json()
    assert view["status"] == "complete" and view["source"] == "simulator"


def test_view_since_returns_204_when_unchanged(client_for: Callable[..., TestClient]) -> None:
    client = client_for()
    run_id = client.post("/api/orchestrate", json={"requirement": LEAVE}).json()["run_id"]
    view = client.get(f"/api/runs/{run_id}/view").json()
    unchanged = client.get(f"/api/runs/{run_id}/view", params={"since": view["version"]})
    assert unchanged.status_code == 204 and unchanged.content == b""
    stale = client.get(f"/api/runs/{run_id}/view", params={"since": "0:running:0"})
    assert stale.status_code == 200


def test_stream_pushes_the_view_then_ends(client_for: Callable[..., TestClient]) -> None:
    client = client_for()
    run_id = client.post("/api/orchestrate", json={"requirement": LEAVE}).json()["run_id"]
    with client.stream("GET", f"/api/runs/{run_id}/stream") as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = "".join(resp.iter_text())
    events = [block for block in body.split("\n\n") if block.strip()]
    assert [e.splitlines()[0] for e in events] == ["event: view", "event: end"]
    payload = json.loads(events[0].split("data: ", 1)[1])
    assert payload["status"] == "complete" and payload["run_id"] == run_id
    assert client.get("/api/runs/nope/stream").status_code == 404


def test_security_headers(client_for: Callable[..., TestClient]) -> None:
    client = client_for()
    page = client.get("/")
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["x-frame-options"] == "DENY"
    docs = client.get("/docs")
    assert docs.status_code == 200 and "content-security-policy" not in docs.headers


def test_view_404_and_auth(client_for: Callable[..., TestClient]) -> None:
    client = client_for(api_keys="op:operator,aud:auditor")
    assert client.get("/api/config").status_code == 200  # public: no secrets in it
    assert client.get("/api/config").json()["auth_required"] is True
    assert client.post("/api/orchestrate", json={"requirement": LEAVE}).status_code == 401
    ok = client.post("/api/orchestrate", json={"requirement": LEAVE}, headers={"X-API-Key": "op"})
    assert ok.status_code == 202
    assert client.get("/api/runs/nope/view", headers={"X-API-Key": "aud"}).status_code == 404


def test_execution_report_pdf(client_for: Callable[..., TestClient]) -> None:
    client = client_for(api_keys="op:operator,aud:auditor")
    run_id = client.post(
        "/api/orchestrate", json={"requirement": LEAVE}, headers={"X-API-Key": "op"}
    ).json()["run_id"]
    url = f"/api/runs/{run_id}/report.pdf"
    assert client.get(url).status_code == 401
    assert client.get("/api/runs/nope/report.pdf", headers={"X-API-Key": "aud"}).status_code == 404
    report = client.get(url, headers={"X-API-Key": "aud"})
    assert report.status_code == 200
    assert report.headers["content-type"] == "application/pdf"
    assert f'filename="{run_id}-sdlc-execution-report.pdf"' in report.headers["content-disposition"]
    assert report.content.startswith(b"%PDF") and report.content.rstrip().endswith(b"%%EOF")
    assert len(re.findall(rb"/Type /Page[^s]", report.content)) >= 5  # cover through timeline


def test_frontend_never_uses_innerhtml() -> None:
    """Model output is untrusted; the UI must build the DOM with textContent only."""
    source = (Path(STATIC_DIR) / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in source.replace("innerHTML is never used", "")
    assert "insertAdjacentHTML" not in source and "document.write" not in source
