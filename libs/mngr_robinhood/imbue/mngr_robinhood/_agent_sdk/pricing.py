"""Approximate per-model cost computation for the mngr-backed Agent SDK.

claude's native session JSONL carries per-message ``usage`` token counts but not the
stream-json ``result`` event's ``total_cost_usd``. To surface a non-null
``ResultMessage.total_cost_usd``, the driver computes an approximate cost from the turn's
accumulated token usage multiplied by a static per-model price table (public list prices).
The value is approximate -- it tracks list pricing and does not account for negotiated rates
or future price changes -- and is ``None`` for any model not present in the table.
"""

from collections.abc import Mapping
from collections.abc import Sequence
from decimal import Decimal
from typing import Any
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

# Anthropic public list prices, USD per million tokens. Cache-write is the 5-minute-TTL rate
# (1.25x base input) and cache-read is the 0.1x base input rate. Keep this in sync with the
# Anthropic pricing page (see the ``claude-api`` skill for current values).
_TOKENS_PER_MILLION: Final[Decimal] = Decimal(1_000_000)


class ModelPricing(FrozenModel):
    """Per-million-token USD list prices for one Claude model family."""

    input_usd_per_million: Decimal = Field(description="Price per 1M uncached input tokens")
    output_usd_per_million: Decimal = Field(description="Price per 1M output tokens")
    cache_write_usd_per_million: Decimal = Field(description="Price per 1M cache-creation (write) input tokens")
    cache_read_usd_per_million: Decimal = Field(description="Price per 1M cache-read input tokens")


_HAIKU_PRICING: Final[ModelPricing] = ModelPricing(
    input_usd_per_million=Decimal("1.00"),
    output_usd_per_million=Decimal("5.00"),
    cache_write_usd_per_million=Decimal("1.25"),
    cache_read_usd_per_million=Decimal("0.10"),
)
_SONNET_PRICING: Final[ModelPricing] = ModelPricing(
    input_usd_per_million=Decimal("3.00"),
    output_usd_per_million=Decimal("15.00"),
    cache_write_usd_per_million=Decimal("3.75"),
    cache_read_usd_per_million=Decimal("0.30"),
)
_OPUS_PRICING: Final[ModelPricing] = ModelPricing(
    input_usd_per_million=Decimal("5.00"),
    output_usd_per_million=Decimal("25.00"),
    cache_write_usd_per_million=Decimal("6.25"),
    cache_read_usd_per_million=Decimal("0.50"),
)

# Model-id substrings mapped to their pricing, checked in order. claude model ids look like
# ``claude-haiku-4-5-20251001`` / ``claude-sonnet-4-6`` / ``claude-opus-4-8``, so a family
# substring match resolves both bare aliases and dated full ids.
_PRICING_BY_FAMILY_SUBSTRING: Final[Sequence[tuple[str, ModelPricing]]] = (
    ("haiku", _HAIKU_PRICING),
    ("sonnet", _SONNET_PRICING),
    ("opus", _OPUS_PRICING),
)


@pure
def resolve_model_pricing(model: str) -> ModelPricing | None:
    """Resolve a claude model id to its pricing by family substring, or ``None`` if unknown."""
    normalized = model.lower()
    for family_substring, pricing in _PRICING_BY_FAMILY_SUBSTRING:
        if family_substring in normalized:
            return pricing
    return None


@pure
def _usage_token_count(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and value >= 0 else 0


@pure
def compute_total_cost_usd(model: str, usage: Mapping[str, Any] | None) -> float | None:
    """Compute approximate USD cost for a turn from its accumulated token usage.

    Returns ``None`` when the model is not in the price table or no usage is available, so the
    caller leaves ``ResultMessage.total_cost_usd`` unset rather than reporting a misleading zero.
    """
    if not usage:
        return None
    pricing = resolve_model_pricing(model)
    if pricing is None:
        return None
    input_tokens = _usage_token_count(usage, "input_tokens")
    output_tokens = _usage_token_count(usage, "output_tokens")
    cache_write_tokens = _usage_token_count(usage, "cache_creation_input_tokens")
    cache_read_tokens = _usage_token_count(usage, "cache_read_input_tokens")
    total_usd = (
        pricing.input_usd_per_million * input_tokens
        + pricing.output_usd_per_million * output_tokens
        + pricing.cache_write_usd_per_million * cache_write_tokens
        + pricing.cache_read_usd_per_million * cache_read_tokens
    ) / _TOKENS_PER_MILLION
    return float(total_usd)


@pure
def accumulate_usage_totals(running_totals: Mapping[str, int], new_usage: Mapping[str, Any]) -> dict[str, int]:
    """Sum the integer token fields of ``new_usage`` into ``running_totals``, returning a new dict.

    Only the four cost-relevant token fields are accumulated; non-integer / nested usage fields
    (e.g. ``service_tier``, ``server_tool_use``) are ignored.
    """
    accumulated = dict(running_totals)
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = new_usage.get(key)
        if isinstance(value, int) and value >= 0:
            accumulated[key] = accumulated.get(key, 0) + value
    return accumulated
