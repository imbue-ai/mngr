"""Token -> USD pricing for usage sources that report tokens but not cost.

``mngr_usage`` derives cost centrally (here) rather than on the agent host, so a
token-only writer (e.g. Codex, or pi for a provider where it has no client-side
cost) just emits ``tokens`` + ``model`` and the reader prices it.

The numbers are **human-curated**, mirrored from litellm's
``model_prices_and_context_window`` map -- not read from litellm at runtime --
matching the established posture in ``apps/modal_litellm/app.py`` (inline pricing
so cost stays correct even on a litellm version whose bundled map predates a
model). The Anthropic entries below are byte-for-byte the modal_litellm values,
which a live pi session independently confirmed to the digit;
``apps/modal_litellm/mngr_usage_pricing_drift_test.py`` enforces that they stay
in sync (changing a price on either side without the other fails that test).

Cost is ``input*p_in + cache_read*p_cr + cache_creation*p_cw + output*p_out``,
relying on ``TokenSnapshot``'s non-overlapping buckets (see its docstring). An
unknown model resolves to ``None`` -- never ``$0`` -- so a brand-new model is
visibly unpriced rather than silently free.
"""

from __future__ import annotations

from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_usage.data_types import TokenSnapshot


class PerTokenPrices(FrozenModel):
    """USD price per single token for each billing bucket of one model.

    Field names match litellm's ``model_prices_and_context_window`` schema so
    entries are directly comparable to ``apps/modal_litellm``'s inline pricing.
    """

    input_cost_per_token: float = Field(description="USD per non-cached input token.")
    output_cost_per_token: float = Field(description="USD per output token (incl. reasoning).")
    cache_read_input_token_cost: float = Field(description="USD per cached input token read from the prompt cache.")
    cache_creation_input_token_cost: float = Field(
        description="USD per input token written to the prompt cache; 0 for providers with no cache-write surcharge."
    )


# Anthropic per-token pricing, mirrored verbatim from apps/modal_litellm/app.py
# (which itself mirrors litellm's map). Grouped by tier so the "same price"
# relationship across model ids stays explicit, exactly as modal_litellm does.
_OPUS_PRICES: Final[PerTokenPrices] = PerTokenPrices(
    input_cost_per_token=0.000005,
    output_cost_per_token=0.000025,
    cache_creation_input_token_cost=0.00000625,
    cache_read_input_token_cost=0.0000005,
)
# Opus 4.1 and the original Opus 4 predate the Opus price drop and cost 3x.
_OPUS_LEGACY_PRICES: Final[PerTokenPrices] = PerTokenPrices(
    input_cost_per_token=0.000015,
    output_cost_per_token=0.000075,
    cache_creation_input_token_cost=0.00001875,
    cache_read_input_token_cost=0.0000015,
)
_SONNET_PRICES: Final[PerTokenPrices] = PerTokenPrices(
    input_cost_per_token=0.000003,
    output_cost_per_token=0.000015,
    cache_creation_input_token_cost=0.00000375,
    cache_read_input_token_cost=0.0000003,
)
_HAIKU_PRICES: Final[PerTokenPrices] = PerTokenPrices(
    input_cost_per_token=0.000001,
    output_cost_per_token=0.000005,
    cache_creation_input_token_cost=0.00000125,
    cache_read_input_token_cost=0.0000001,
)

# Canonical pricing key is "<provider>/<model>" (the provider qualifier
# disambiguates multi-provider harnesses like pi, where the same model name can
# route through different providers). Seeded with the Anthropic set; OpenAI /
# Gemini / etc. entries are added when their harnesses (Codex, pi fallback) are
# wired, each guarded by the accuracy + drift tests.
MODEL_PRICING: Final[dict[str, PerTokenPrices]] = {
    "anthropic/claude-opus-4-8": _OPUS_PRICES,
    "anthropic/claude-opus-4-7": _OPUS_PRICES,
    "anthropic/claude-opus-4-6": _OPUS_PRICES,
    "anthropic/claude-opus-4-5": _OPUS_PRICES,
    "anthropic/claude-opus-4-1": _OPUS_LEGACY_PRICES,
    "anthropic/claude-opus-4-20250514": _OPUS_LEGACY_PRICES,
    "anthropic/claude-sonnet-4-6": _SONNET_PRICES,
    "anthropic/claude-sonnet-4-5": _SONNET_PRICES,
    "anthropic/claude-sonnet-4-20250514": _SONNET_PRICES,
    "anthropic/claude-haiku-4-5": _HAIKU_PRICES,
    "anthropic/claude-haiku-4-5-20251001": _HAIKU_PRICES,
}


@pure
def compute_cost(model: str, tokens: TokenSnapshot) -> float | None:
    """Return the USD cost for ``tokens`` under ``model``'s pricing, or None if unpriced.

    ``model`` is the canonical ``"<provider>/<model>"`` key. None means the model
    is not in the table -- the caller surfaces that (a WARNING) rather than
    treating an unpriced model as free.
    """
    prices = MODEL_PRICING.get(model)
    if prices is None:
        return None
    return (
        (tokens.input or 0) * prices.input_cost_per_token
        + (tokens.cache_read or 0) * prices.cache_read_input_token_cost
        + (tokens.cache_creation or 0) * prices.cache_creation_input_token_cost
        + (tokens.output or 0) * prices.output_cost_per_token
    )
