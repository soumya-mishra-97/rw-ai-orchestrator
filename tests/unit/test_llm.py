"""Routing, caching layout, cost maths and request construction — no network."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from sdlc_loop.config import RoutingPolicy, Settings
from sdlc_loop.llm.anthropic_client import build_params
from sdlc_loop.llm.cache import CachePolicy
from sdlc_loop.llm.model_router import ModelRouter
from sdlc_loop.llm.models import UnknownModelError, get_model_spec
from sdlc_loop.llm.token_meter import compute_cost, reprice
from sdlc_loop.llm.types import CallTags, LLMRequest, TokenUsage
from sdlc_loop.prompts import persona_prompt
from sdlc_loop.schemas import Persona, ProblemBrief
from sdlc_loop.schemas.common import ModelTier

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-5"


@pytest.mark.parametrize(
    ("persona", "attempt", "forced", "tier"),
    [
        (Persona.PM, 1, False, ModelTier.TIER1),
        (Persona.PM, 2, False, ModelTier.TIER1),
        (Persona.PM, 3, False, ModelTier.TIER2),  # final attempt escalates
        (Persona.BA, 1, True, ModelTier.TIER2),  # critical fast-track
        (Persona.QA, 1, False, ModelTier.TIER1),
        (Persona.ARCHITECT, 1, False, ModelTier.TIER2),  # always
        (Persona.DEV, 1, False, ModelTier.TIER2),  # always
    ],
)
def test_router(
    make_settings: Callable[..., Settings],
    persona: Persona,
    attempt: int,
    forced: bool,
    tier: ModelTier,
) -> None:
    router = ModelRouter(make_settings())
    assert router.for_persona(persona, attempt, force_tier2=forced).tier is tier


def test_router_all_tier2_baseline(make_settings: Callable[..., Settings]) -> None:
    router = ModelRouter(make_settings(routing_policy=RoutingPolicy.ALL_TIER2))
    assert router.for_persona(Persona.PM, 1, force_tier2=False).spec.model_id == SONNET
    assert router.for_judge(confirm=False).spec.model_id == SONNET


def test_unknown_model_rejected() -> None:
    with pytest.raises(UnknownModelError):
        get_model_spec("gpt-4o")


def test_cache_layout_and_eligibility() -> None:
    policy = CachePolicy(enabled=True)
    haiku = policy.plan(persona_prompt(Persona.PM), get_model_spec(HAIKU))
    sonnet = policy.plan(persona_prompt(Persona.ARCHITECT), get_model_spec(SONNET))
    assert all(b.cache for b in haiku.blocks)
    # The shared prefix clears Sonnet 5's 1,024-token minimum but not Haiku 4.5's 4,096:
    # documented in docs/cost-optimization.md as a measured limitation, not hidden.
    assert sonnet.eligible
    assert not haiku.eligible
    # identical first block for every persona => one cache entry per model serves all of them
    assert haiku.blocks[0].text == sonnet.blocks[0].text
    assert not CachePolicy(enabled=False).plan("x", get_model_spec(SONNET)).blocks[0].cache


def _request(model: str, effort: str | None) -> LLMRequest:
    plan = CachePolicy(enabled=True).plan(persona_prompt(Persona.PM), get_model_spec(model))
    return LLMRequest(
        model_id=model,
        system=plan.blocks,
        user_content="<untrusted_business_request>x</untrusted_business_request>",
        output_schema=ProblemBrief,
        max_tokens=1000,
        tags=CallTags(run_id="r", persona=Persona.PM, purpose="persona:pm"),
        effort=effort,
    )


def test_build_params_structured_output_and_cache_control() -> None:
    params = build_params(_request(SONNET, "medium"))
    system = list(params["system"])  # type: ignore[arg-type]
    assert all(b.get("cache_control") == {"type": "ephemeral"} for b in system)
    output_config = params["output_config"]
    fmt = output_config.get("format")
    assert fmt is not None and fmt["type"] == "json_schema"
    assert output_config.get("effort") == "medium"
    assert "temperature" not in params


def test_effort_dropped_for_models_without_it() -> None:
    assert "effort" not in build_params(_request(HAIKU, "medium"))["output_config"]


def test_cost_waterfall() -> None:
    sonnet = get_model_spec(SONNET)
    usage = TokenUsage(
        input_tokens=1_000,
        output_tokens=1_000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=9_000,
    )
    cost = compute_cost(usage, sonnet)
    # 1k uncached ($0.002) + 9k cache reads at 10% ($0.0018) + 1k output ($0.01)
    assert cost.sync_usd == pytest.approx(0.0138)
    assert cost.no_cache_sync_usd == pytest.approx(0.03)  # 10k input + 1k output
    batched = compute_cost(usage, sonnet, batched=True)
    assert batched.actual_usd == pytest.approx(cost.sync_usd / 2)
    haiku_usage = TokenUsage(input_tokens=1_000_000, output_tokens=0)
    assert reprice(haiku_usage, sonnet) == pytest.approx(2.0)
    assert compute_cost(haiku_usage, get_model_spec(HAIKU)).actual_usd == pytest.approx(1.0)


def test_cache_write_premium() -> None:
    usage = TokenUsage(cache_creation_input_tokens=1_000_000)
    assert compute_cost(usage, get_model_spec(SONNET)).sync_usd == pytest.approx(2.5)
