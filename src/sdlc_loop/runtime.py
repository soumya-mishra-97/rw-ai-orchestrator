"""Startup mode selection: live (Claude API) or demo (curated examples + simulator).

``SDLC_MODE=auto`` (the default) chooses live when Anthropic credentials are
found *and* accepted by the API, otherwise demo, and records why. Forcing
``live`` fails fast with instructions instead of silently degrading, and
``demo`` never touches the network. Both modes drive the same orchestrator,
graph, evaluator gates and audit log; only the LLM backend differs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from sdlc_loop.config import Settings
from sdlc_loop.demo.library import DemoLibrary, DemoLLMClient
from sdlc_loop.llm.types import LLMClient
from sdlc_loop.orchestrator import OrchestratorMode

CREDENTIAL_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


class LiveModeUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Runtime:
    mode: OrchestratorMode
    reason: str
    llm: LLMClient
    demo: DemoLLMClient | None


def load_environment(env_file: Path = Path(".env")) -> None:
    """Export ``.env`` into the process so the Anthropic SDK can read its key.

    Real environment variables always win (``override=False``).
    """
    if env_file.is_file():
        load_dotenv(env_file, override=False)


def credentials_present() -> bool:
    if any(os.environ.get(name) for name in CREDENTIAL_ENV_VARS):
        return True
    return (Path.home() / ".config" / "anthropic").is_dir()  # `ant auth login` profile


def _demo(settings: Settings, reason: str) -> Runtime:
    demo = DemoLLMClient(DemoLibrary(), playback_delay_s=settings.demo_playback_delay_s)
    return Runtime(OrchestratorMode.OFFLINE, reason, demo, demo)


def resolve_runtime(settings: Settings, requested: str | None = None) -> Runtime:
    requested = requested or settings.mode
    if requested == "demo":
        return _demo(settings, "Demo mode was requested.")
    if not credentials_present():
        if requested == "live":
            raise LiveModeUnavailableError(
                "Live mode needs Anthropic credentials: set ANTHROPIC_API_KEY in the "
                "environment or in .env (or run `ant auth login`)."
            )
        return _demo(settings, "No Anthropic credentials were found (ANTHROPIC_API_KEY is unset).")

    from sdlc_loop.llm.anthropic_client import AnthropicLLMClient

    client = AnthropicLLMClient(
        max_retries=settings.anthropic_max_retries, timeout_s=settings.anthropic_timeout_s
    )
    if settings.verify_credentials and (problem := client.verify(settings.tier1_model)):
        if requested == "live":
            raise LiveModeUnavailableError(f"Anthropic credentials are unusable: {problem}.")
        return _demo(settings, f"Anthropic credentials were found but {problem}.")
    return Runtime(OrchestratorMode.LIVE, "Anthropic credentials found and verified.", client, None)
