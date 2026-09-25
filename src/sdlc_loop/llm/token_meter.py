"""Token accounting and cost computation, including the counterfactuals the cost
write-up needs (what this call would have cost without caching, or on Tier 2).
"""

from __future__ import annotations

from sdlc_loop.llm.models import (
    BATCH_DISCOUNT,
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    ModelSpec,
)
from sdlc_loop.llm.types import TokenUsage
from sdlc_loop.schemas.common import StrictModel

_PER_TOKEN = 1_000_000


class CallCost(StrictModel):
    """Actual cost plus the counterfactuals that make up the cost waterfall:

    ``no_cache_sync_usd`` (routing only) → ``sync_usd`` (+ caching) → ``actual_usd`` (+ batch)
    """

    actual_usd: float
    sync_usd: float  # same tokens, without the batch discount
    no_cache_sync_usd: float  # same tokens, no caching, no batch discount


def compute_cost(usage: TokenUsage, spec: ModelSpec, *, batched: bool = False) -> CallCost:
    inp = spec.input_usd_per_mtok / _PER_TOKEN
    out = spec.output_usd_per_mtok / _PER_TOKEN
    sync = (
        usage.input_tokens * inp
        + usage.cache_creation_input_tokens * inp * CACHE_WRITE_MULTIPLIER
        + usage.cache_read_input_tokens * inp * CACHE_READ_MULTIPLIER
        + usage.output_tokens * out
    )
    no_cache = usage.total_input_tokens * inp + usage.output_tokens * out
    factor = BATCH_DISCOUNT if batched else 1.0
    return CallCost(
        actual_usd=round(sync * factor, 8),
        sync_usd=round(sync, 8),
        no_cache_sync_usd=round(no_cache, 8),
    )


def reprice(usage: TokenUsage, spec: ModelSpec) -> float:
    """Cost of the same token counts on another model, uncached and synchronous.

    Used for the "everything on Tier 2" routing baseline. It is an approximation:
    different model generations can tokenize the same text differently.
    """
    inp = spec.input_usd_per_mtok / _PER_TOKEN
    out = spec.output_usd_per_mtok / _PER_TOKEN
    return round(usage.total_input_tokens * inp + usage.output_tokens * out, 8)


def estimate_tokens(text: str) -> int:
    """Cheap local estimate (~4 chars/token for English + JSON).

    Only used for *counterfactual* context sizes that are never actually sent
    (the naive full-history baseline). Real calls always use API-reported usage.
    """
    return max(1, len(text) // 4)
