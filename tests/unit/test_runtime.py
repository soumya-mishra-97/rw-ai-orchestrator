"""Startup mode selection: live only with working credentials, never a silent surprise."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from sdlc_loop.config import Settings
from sdlc_loop.llm import anthropic_client
from sdlc_loop.orchestrator import OrchestratorMode
from sdlc_loop.runtime import LiveModeUnavailableError, load_environment, resolve_runtime


@pytest.fixture
def no_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)  # no `ant auth login` profile


def _verify_returns(problem: str | None) -> Callable[..., str | None]:
    return lambda self, model_id: problem


def test_auto_without_credentials_is_demo(
    make_settings: Callable[..., Settings], no_credentials: None
) -> None:
    runtime = resolve_runtime(make_settings())
    assert runtime.mode is OrchestratorMode.OFFLINE
    assert runtime.demo is not None and "No Anthropic credentials" in runtime.reason


def test_forced_live_without_credentials_fails_fast(
    make_settings: Callable[..., Settings], no_credentials: None
) -> None:
    with pytest.raises(LiveModeUnavailableError, match="ANTHROPIC_API_KEY"):
        resolve_runtime(make_settings(), requested="live")


def test_auto_with_verified_key_is_live(
    make_settings: Callable[..., Settings],
    no_credentials: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(anthropic_client.AnthropicLLMClient, "verify", _verify_returns(None))
    runtime = resolve_runtime(make_settings())
    assert runtime.mode is OrchestratorMode.LIVE and runtime.demo is None


def test_auto_with_rejected_key_falls_back_to_demo_and_says_why(
    make_settings: Callable[..., Settings],
    no_credentials: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-bad")
    rejected = _verify_returns("the API key was rejected (401)")
    monkeypatch.setattr(anthropic_client.AnthropicLLMClient, "verify", rejected)
    runtime = resolve_runtime(make_settings())
    assert runtime.mode is OrchestratorMode.OFFLINE
    assert "rejected (401)" in runtime.reason
    with pytest.raises(LiveModeUnavailableError, match="401"):
        resolve_runtime(make_settings(), requested="live")


def test_demo_request_never_touches_credentials(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-anything")
    assert resolve_runtime(make_settings(), requested="demo").mode is OrchestratorMode.OFFLINE


def test_dotenv_key_reaches_the_process_but_never_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    load_environment(env)
    import os

    assert os.environ["ANTHROPIC_API_KEY"] == "from-dotenv"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-shell")
    load_environment(env)
    assert os.environ["ANTHROPIC_API_KEY"] == "from-shell"
