"""Token -> USD pricing for usage sources that report tokens but not cost.

``mngr_usage`` derives cost centrally (here) rather than on the agent host, so a
token-only writer (e.g. Codex, or pi for a provider where it has no client-side
cost) just emits ``tokens`` + ``model`` and the reader prices it.

The rates live in ``model_prices.toml`` beside this module, which ships inside the
wheel because this table is consulted on agent machines that never import litellm.
``scripts/sync_litellm_prices.py`` moves it in step with litellm's
``model_prices_and_context_window`` map -- the map the LiteLLM proxy bills from --
as a reviewed edit rather than a runtime read. litellm resolves that map by
fetching its own main branch when it is imported, and that file is edited hundreds
of times a month, so a test pinned to it fails on other people's commits.

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

import tomllib
from pathlib import Path
from typing import Final

from pydantic import Field
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import NonNegativeFloat
from imbue.imbue_common.primitives import PositiveFloat
from imbue.imbue_common.pure import pure
from imbue.mngr_usage.data_types import TokenSnapshot


class ModelPricingError(Exception):
    """Raised when the per-token price table cannot be read."""

    ...


class PerTokenPrices(FrozenModel):
    """USD price per single token for each billing bucket of one model."""

    # Positive, not merely non-negative: a zero input or output rate reads as "this
    # model is free", which is how a lost price entry stops counting spend without
    # anything looking wrong.
    input_cost_per_token: PositiveFloat = Field(description="USD per non-cached input token.")
    output_cost_per_token: PositiveFloat = Field(description="USD per output token (incl. reasoning).")
    cache_read_input_token_cost: NonNegativeFloat = Field(
        description="USD per cached input token read from the prompt cache."
    )
    # Anthropic charges a cache write by its TTL: a 5-minute write costs 1.25x an
    # input token, a 1-hour write 2x. This is the 5-minute rate, and it is the only
    # one modeled -- TokenSnapshot carries a single cache_creation bucket with no TTL
    # on it, so a 1-hour write is priced at 62.5% of what it actually cost. Modeling
    # the difference means splitting that bucket in every writer that fills it, not
    # just adding a rate here.
    cache_creation_input_token_cost: NonNegativeFloat = Field(
        description="USD per input token written to the prompt cache; 0 when a model bills no cache-write surcharge."
    )


# Keys are "<provider>/<model>": the provider qualifier disambiguates
# multi-provider harnesses like pi, which report a bare model id for several.
MODEL_PRICES_PATH: Final[Path] = Path(__file__).parent / "model_prices.toml"


def load_model_prices(model_prices_path: Path) -> dict[str, PerTokenPrices]:
    """Read the price table; raises ModelPricingError if it is missing, malformed, or mis-keyed."""
    try:
        raw_toml = model_prices_path.read_text(encoding="utf-8")
    except OSError as e:
        raise ModelPricingError(f"Cannot read the price table at {model_prices_path}") from e
    try:
        price_entry_by_key = tomllib.loads(raw_toml)
    except tomllib.TOMLDecodeError as e:
        raise ModelPricingError(f"Invalid TOML in the price table at {model_prices_path}") from e

    # An unqualified key can never match a lookup, so it would silently price nothing.
    unqualified_keys = sorted(key for key in price_entry_by_key if "/" not in key)
    if unqualified_keys:
        raise ModelPricingError(
            f"Price table keys must be '<provider>/<model>', but {model_prices_path} has {unqualified_keys}"
        )

    try:
        return {key: PerTokenPrices.model_validate(entry) for key, entry in price_entry_by_key.items()}
    except ValidationError as e:
        raise ModelPricingError(f"Invalid price entry in {model_prices_path}") from e


MODEL_PRICING: Final[dict[str, PerTokenPrices]] = load_model_prices(MODEL_PRICES_PATH)


# Fast mode is a per-request tier that returns the same tokens faster for twice the
# model's standard price ($8/$40 per MTok against Opus 5.5's $4/$20), across the full
# context window. It is a flat multiplier rather than a second price table because
# it doubles *every* bucket: the cache multipliers are defined against the input rate
# (a write costs 1.25x an input token, a read a fixed fraction of one), so doubling
# the input rate carries them along.
FAST_MODE_PRICE_MULTIPLIER: Final[float] = 2.0
# Which models can serve a request in fast mode. This is keyed by model id rather
# than carried on PerTokenPrices because the two do not partition the same way: Opus
# models billed at identical rates differ in whether they offer fast mode (4.6 shares
# 4.8's price set). The API rejects ``speed`` outright on Sonnet and Haiku, and runs
# Opus 4.6 and older at standard speed and standard rates.
FAST_MODE_MODELS: Final[frozenset[str]] = frozenset(
    {
        "anthropic/claude-opus-5-5",
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
