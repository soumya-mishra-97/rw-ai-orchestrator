"""Runtime configuration.

All tunables live here and are read from environment variables (prefix ``SDLC_``)
or a local ``.env`` file. Secrets are deliberately *not* modelled here: the
Anthropic SDK reads ``ANTHROPIC_API_KEY`` itself, so the key never lands in a
settings object that could be logged or serialised.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ContextPolicy(StrEnum):
    """How much upstream state each persona receives."""

    MINIMAL = "minimal"  # only the immediately-relevant upstream artifact(s)
    FULL_HISTORY = "full_history"  # naive baseline: everything produced so far


class RoutingPolicy(StrEnum):
    """Model routing strategy."""

    ROUTED = "routed"  # Tier 1 by default, Tier 2 by risk / retry state
    ALL_TIER2 = "all_tier2"  # baseline: every call on the Tier 2 model


class CriticalPolicy(StrEnum):
    """What a ``critical_flag`` does on a non-final attempt.

    ``STRICT`` escalates to a human immediately (spec Section 5 verbatim).
    ``FAST_TRACK`` spends one retry on the Tier 2 model first and only escalates
    if the critical defect survives it. See docs/architecture.md for the reasoning.
    """

    STRICT = "strict"
    FAST_TRACK = "fast_track"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SDLC_", env_file=".env", extra="ignore")

    # Runtime mode
    # auto: live when Anthropic credentials are found and verified, otherwise demo.
    mode: Literal["auto", "live", "demo"] = "auto"
    verify_credentials: bool = True  # one free models.retrieve call at startup

    # Models
    tier1_model: str = "claude-haiku-4-5-20251001"
    tier2_model: str = "claude-sonnet-5"
    tier2_effort: Literal["low", "medium", "high"] = "medium"
    anthropic_max_retries: int = Field(default=3, ge=0, le=10)
    anthropic_timeout_s: float = Field(default=600.0, gt=0)

    # Reliability engine (Section 5)
    max_retries_per_handoff: int = Field(default=2, ge=0, le=5)
    pass_threshold: float = 4.0
    min_dimension_score: int = 3
    borderline_low: float = 3.0
    critical_policy: CriticalPolicy = CriticalPolicy.FAST_TRACK
    parse_repair_attempts: int = Field(default=1, ge=0, le=3)
    include_previous_attempt_on_retry: bool = True

    # Cost levers (Section 9) — each can be switched off to measure its effect
    context_policy: ContextPolicy = ContextPolicy.MINIMAL
    routing_policy: RoutingPolicy = RoutingPolicy.ROUTED
    tiered_evaluator: bool = True
    prompt_caching: bool = True
    deterministic_pregate: bool = True

    # Offline demo (browser UI)
    # Pause before each replayed call so stages visibly progress in the browser.
    # Not a latency measurement: replayed calls always report latency 0.
    demo_playback_delay_s: float = Field(default=0.6, ge=0, le=5)

    # QA code verification
    execute_generated_code: bool = False  # opt-in: runs LLM-written tests in a subprocess
    code_exec_timeout_s: float = 30.0

    # Storage
    data_dir: Path = Path("var")

    # Governance
    pii_backend: Literal["regex", "presidio"] = "regex"
    # "key:role,key:role" — roles: operator (start runs), reviewer (resume), auditor (read)
    api_keys: str = ""

    log_level: str = "INFO"
    log_json: bool = True

    @property
    def checkpoint_db(self) -> Path:
        return self.data_dir / "checkpoints.db"

    @property
    def audit_db(self) -> Path:
        return self.data_dir / "audit.db"

    @property
    def max_attempts(self) -> int:
        return self.max_retries_per_handoff + 1

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
