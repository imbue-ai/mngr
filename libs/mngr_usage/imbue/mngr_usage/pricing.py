"""Token -> USD pricing for usage sources that report tokens but not cost.

``mngr_usage`` derives cost centrally (here) rather than on the agent host, so a
token-only writer (e.g. Codex, or pi for a provider where it has no client-side
cost) just emits ``tokens`` + ``model`` and the reader prices it.

The numbers are litellm's, copied here rather than read at runtime: this table is
consulted on agent machines that never import litellm (it ships in the ``mngr``
wheel, litellm does not). ``litellm_pricing_test`` pins every entry -- OpenAI and
Anthropic alike -- against litellm's ``model_prices_and_context_window`` map, so
the copy cannot drift from the source that the LiteLLM proxy actually bills from.

This table is a *fallback*, not the main cost path: ``api.py`` prefers a
harness-reported ``total_cost_usd`` and only prices tokens when the harness does
not report dollars. Claude Code reports its own cost, so in practice these
entries serve the token-only sources (codex, pi).

Cost is ``input*p_in + cache_read*p_cr + cache_creation*p_cw + output*p_out``,
relying on ``TokenSnapshot``'s non-overlapping buckets (see its docstring). An
unknown model resolves to ``None`` -- never ``$0`` -- so a brand-new model is
visibly unpriced rather than silently free.

Rates depend on more than the model id. Fast mode bills the same tokens at twice
the standard rate and is chosen per *request*, so it is a multiplier
(``FAST_MODE_PRICE_MULTIPLIER``, applied for the models in ``FAST_MODE_MODELS``)
selected by ``compute_cost``'s ``is_fast_mode`` rather than more entries in the
table. A caller that cannot observe which tier served a request necessarily
prices it standard, which is a floor rather than a figure -- so a usage source
that wants an exact cost has to carry the tier through with the tokens.
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
    # Anthropic charges a cache write by its TTL: a 5-minute write costs 1.25x an
    # input token, a 1-hour write 2x. This is the 5-minute rate, and it is the only
    # one modeled -- TokenSnapshot carries a single cache_creation bucket with no TTL
    # on it, so a 1-hour write is priced at 62.5% of what it actually cost. Modeling
    # the difference means splitting that bucket in every writer that fills it, not
    # just adding a rate here.
    cache_creation_input_token_cost: float = Field(
        description="USD per input token written to the prompt cache; 0 for providers with no cache-write surcharge."
    )


# Anthropic per-token pricing, mirrored verbatim from apps/modal_litellm/app.py
# (which itself mirrors litellm's map). Grouped by tier so the "same price"
# relationship across model ids stays explicit, exactly as modal_litellm does.
_FABLE_PRICES: Final[PerTokenPrices] = PerTokenPrices(
    input_cost_per_token=0.00001,
    output_cost_per_token=0.00005,
    cache_creation_input_token_cost=0.0000125,
    cache_read_input_token_cost=0.000001,
)
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

# OpenAI per-token pricing, mirrored verbatim from litellm's
# model_prices_and_context_window map (the ultimate source). OpenAI has no
# cache-*write* surcharge -- caching is automatic, only reads are discounted --
# so cache_creation_input_token_cost is 0 for every entry. Codex reports tokens
# (not dollars), so these drive its estimated cost; mngr_usage's
# litellm_pricing_test enforces that they stay in sync with litellm.
_GPT5_PRICES: Final[PerTokenPrices] = PerTokenPrices(
    input_cost_per_token=0.00000125,
    output_cost_per_token=0.00001,
    cache_read_input_token_cost=0.000000125,
    cache_creation_input_token_cost=0.0,
)
_GPT52_PRICES: Final[PerTokenPrices] = PerTokenPrices(
    input_cost_per_token=0.00000175,
    output_cost_per_token=0.000014,
    cache_read_input_token_cost=0.000000175,
    cache_creation_input_token_cost=0.0,
)
_GPT5_MINI_PRICES: Final[PerTokenPrices] = PerTokenPrices(
    input_cost_per_token=0.00000025,
    output_cost_per_token=0.000002,
    cache_read_input_token_cost=0.000000025,
    cache_creation_input_token_cost=0.0,
)
_CODEX_MINI_PRICES: Final[PerTokenPrices] = PerTokenPrices(
    input_cost_per_token=0.0000015,
    output_cost_per_token=0.000006,
    cache_read_input_token_cost=0.000000375,
    cache_creation_input_token_cost=0.0,
)
_O3_PRICES: Final[PerTokenPrices] = PerTokenPrices(
    input_cost_per_token=0.000002,
    output_cost_per_token=0.000008,
    cache_read_input_token_cost=0.0000005,
    cache_creation_input_token_cost=0.0,
)
_O4_MINI_PRICES: Final[PerTokenPrices] = PerTokenPrices(
    input_cost_per_token=0.0000011,
    output_cost_per_token=0.0000044,
    cache_read_input_token_cost=0.000000275,
    cache_creation_input_token_cost=0.0,
)

# Canonical pricing key is "<provider>/<model>" (the provider qualifier
# disambiguates multi-provider harnesses like pi). Every entry stays in sync with
# litellm's map directly (litellm_pricing_test).
MODEL_PRICING: Final[dict[str, PerTokenPrices]] = {
    "anthropic/claude-fable-5": _FABLE_PRICES,
    "anthropic/claude-opus-5": _OPUS_PRICES,
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
    # OpenAI / Codex models (codex reports model ids like "gpt-5.2-codex").
    "openai/gpt-5": _GPT5_PRICES,
    "openai/gpt-5.1": _GPT5_PRICES,
    "openai/gpt-5-codex": _GPT5_PRICES,
    "openai/gpt-5.1-codex": _GPT5_PRICES,
    "openai/gpt-5.1-codex-max": _GPT5_PRICES,
    "openai/gpt-5.2": _GPT52_PRICES,
    "openai/gpt-5.2-codex": _GPT52_PRICES,
    "openai/gpt-5.3-codex": _GPT52_PRICES,
    "openai/gpt-5-mini": _GPT5_MINI_PRICES,
    "openai/gpt-5.1-codex-mini": _GPT5_MINI_PRICES,
    "openai/codex-mini-latest": _CODEX_MINI_PRICES,
    "openai/o3": _O3_PRICES,
    "openai/o4-mini": _O4_MINI_PRICES,
}


# Fast mode is a per-request tier that returns the same tokens faster for twice the
# price ($10/$50 per MTok against $5/$25), across the full context window. It is a
# flat multiplier rather than a second price table because it doubles *every*
# bucket: the cache multipliers are defined against the input rate (a write costs
# 1.25x an input token, a read 0.1x), so doubling the input rate carries them along.
FAST_MODE_PRICE_MULTIPLIER: Final[float] = 2.0
# Which models can serve a request in fast mode. This is keyed by model id rather
# than carried on PerTokenPrices because the two do not partition the same way: one
# price set is shared across the whole Opus generation, but only these members of it
# offer fast mode. The API rejects ``speed`` outright on Sonnet and Haiku, and runs
# Opus 4.6 and older at standard speed and standard rates.
FAST_MODE_MODELS: Final[frozenset[str]] = frozenset(
    {
        "anthropic/claude-opus-5",
        "anthropic/claude-opus-4-8",
    }
)


@pure
def compute_cost(model: str, tokens: TokenSnapshot, *, is_fast_mode: bool = False) -> float | None:
    """Return the USD cost for ``tokens`` under ``model``'s pricing, or None if unpriced.

    ``model`` is the canonical ``"<provider>/<model>"`` key. None means the model
    is not in the table -- the caller surfaces that (a WARNING) rather than
    treating an unpriced model as free.

    ``is_fast_mode`` prices the tokens at the fast-mode rate, which is what the
    request was billed at when it asked for ``speed: "fast"``. It must be passed
    per request rather than per model: the same model bills at either rate. A
    model that cannot serve fast mode is reported unpriced rather than falling
    back to the standard rate, because that rate is known to be the wrong one --
    silently halving a fast-mode bill is worse than admitting the number is
    unavailable.
    """
    prices = MODEL_PRICING.get(model)
    if prices is None:
        return None
    if is_fast_mode and model not in FAST_MODE_MODELS:
        return None
    cost = (
        (tokens.input or 0) * prices.input_cost_per_token
        + (tokens.cache_read or 0) * prices.cache_read_input_token_cost
        + (tokens.cache_creation or 0) * prices.cache_creation_input_token_cost
        + (tokens.output or 0) * prices.output_cost_per_token
    )
    return cost * FAST_MODE_PRICE_MULTIPLIER if is_fast_mode else cost
