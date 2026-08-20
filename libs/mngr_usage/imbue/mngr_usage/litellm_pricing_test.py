"""Pin mngr_usage's pricing to litellm (the ultimate source), for every provider.

Both the OpenAI and the Anthropic entries are checked directly against litellm's
``model_prices_and_context_window`` map -- editing a price here without matching
litellm fails this test. Anthropic used to be pinned instead to
apps/modal_litellm's inline table, but the proxy no longer carries one: it routes
every Anthropic model through a single catch-all entry and takes pricing from
this same litellm map, so that mirror (and its drift test) is gone and litellm is
the one source both sides answer to. Skipped only if litellm isn't importable (it
is in the monorepo workspace, which is where this runs in CI).
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


def test_anthropic_prices_match_litellm() -> None:
    """Every Anthropic entry must match litellm's map, which is what the proxy bills from.

    mngr_usage keeps its own table because it prices token-only usage sources
    (codex, pi) on machines that never import litellm -- but the numbers must be
    litellm's, since the proxy charges from that map. All four buckets are
    compared: unlike OpenAI, Anthropic bills a cache-write surcharge.
    """
    model_cost = litellm.model_cost
    anthropic_keys = [key for key in MODEL_PRICING if key.startswith("anthropic/")]
    # Guard: an empty list would make the loop vacuously pass.
    assert anthropic_keys, "no anthropic/* entries in MODEL_PRICING"

    for key in anthropic_keys:
        model = key.removeprefix("anthropic/")
        assert model in model_cost, f"{model!r} priced by mngr_usage but absent from litellm's map"
        litellm_entry = model_cost[model]
        prices = MODEL_PRICING[key]
        assert prices.input_cost_per_token == litellm_entry["input_cost_per_token"], f"input price drift for {key}"
        assert prices.output_cost_per_token == litellm_entry["output_cost_per_token"], f"output price drift for {key}"
        assert prices.cache_read_input_token_cost == litellm_entry.get("cache_read_input_token_cost"), (
            f"cache_read price drift for {key}"
        )
        assert prices.cache_creation_input_token_cost == litellm_entry.get("cache_creation_input_token_cost"), (
            f"cache_creation price drift for {key}"
        )
