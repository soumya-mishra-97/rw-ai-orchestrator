"""Model catalog: capabilities and list prices that routing and costing depend on.

Prices are USD per million tokens (Anthropic first-party list prices).
``cache_min_tokens`` is the minimum cacheable prefix for the model — a shorter
prefix silently does not cache, which matters for the cost write-up: our
shared prefix clears Sonnet 5's 1,024-token minimum but not Haiku 4.5's 4,096.
"""

from __future__ import annotations

from dataclasses import dataclass

CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute TTL write
CACHE_READ_MULTIPLIER = 0.10
BATCH_DISCOUNT = 0.50


@dataclass(frozen=True, slots=True)
class ModelSpec:
    model_id: str
    display_name: str
    input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_min_tokens: int
    supports_effort: bool


_CATALOG: dict[str, ModelSpec] = {
    spec.model_id: spec
    for spec in (
        ModelSpec("claude-haiku-4-5-20251001", "Claude Haiku 4.5", 1.00, 5.00, 4096, False),
        ModelSpec("claude-haiku-4-5", "Claude Haiku 4.5", 1.00, 5.00, 4096, False),
        ModelSpec("claude-sonnet-5", "Claude Sonnet 5", 2.00, 10.00, 1024, True),
        ModelSpec("claude-sonnet-4-6", "Claude Sonnet 4.6", 3.00, 15.00, 1024, True),
        ModelSpec("claude-opus-5", "Claude Opus 5", 5.00, 25.00, 512, True),
        # Demo-mode stand-in (sdlc_loop.demo.simulator): no model, no cost.
        ModelSpec("local-simulator", "Local simulator (no model)", 0.0, 0.0, 0, False),
    )
}


class UnknownModelError(ValueError):
    pass


def get_model_spec(model_id: str) -> ModelSpec:
    try:
        return _CATALOG[model_id]
    except KeyError as exc:
        known = ", ".join(sorted(_CATALOG))
        raise UnknownModelError(
            f"Model {model_id!r} is not in the catalog (known: {known}). "
            "Add it to sdlc_loop/llm/models.py with its prices and cache minimum."
        ) from exc
