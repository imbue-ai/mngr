"""Pin mngr_usage's OpenAI pricing to litellm (the ultimate source).

Anthropic prices are pinned to apps/modal_litellm (its drift test); OpenAI prices
have no such mirror, so they're pinned directly to litellm's
``model_prices_and_context_window`` map here -- editing an OpenAI price without
matching litellm fails this test. Skipped only if litellm isn't importable (it is
in the monorepo workspace, which is where this runs in CI).
"""

from __future__ import annotations

import pytest

from imbue.mngr_usage.pricing import MODEL_PRICING

# litellm is the ultimate source for OpenAI prices; skip the module if it's absent
# (it is present in the monorepo workspace, which is where this runs in CI).
litellm = pytest.importorskip("litellm")


def test_openai_prices_match_litellm() -> None:
    model_cost = litellm.model_cost
    openai_keys = [key for key in MODEL_PRICING if key.startswith("openai/")]
    # Guard: an empty list would make the loop vacuously pass.
    assert openai_keys, "no openai/* entries in MODEL_PRICING"

    for key in openai_keys:
        model = key.removeprefix("openai/")
        assert model in model_cost, f"{model!r} priced by mngr_usage but absent from litellm's map"
        litellm_entry = model_cost[model]
        prices = MODEL_PRICING[key]
        assert prices.input_cost_per_token == litellm_entry["input_cost_per_token"], f"input price drift for {key}"
        assert prices.output_cost_per_token == litellm_entry["output_cost_per_token"], f"output price drift for {key}"
        assert prices.cache_read_input_token_cost == litellm_entry.get("cache_read_input_token_cost"), (
            f"cache_read price drift for {key}"
        )
        # OpenAI has no cache-write surcharge; caching is automatic (read discount only).
        assert prices.cache_creation_input_token_cost == 0.0, f"{key} should have no cache-creation cost"
