"""Pin mngr_usage's pricing to litellm (the ultimate source), for every provider.

Every OpenAI and Anthropic entry that litellm's live ``model_prices_and_context_window``
map lists is checked against it -- editing such a price without matching litellm
fails this test. An entry the live map no longer lists is not price-checked, but it
must be in the map bundled with the pinned litellm, so a made-up or mistyped entry
fails. Skipped only if litellm isn't importable (it is in the monorepo workspace,
which is where this runs in CI).
"""

from __future__ import annotations

import pytest

from imbue.mngr_usage.pricing import MODEL_PRICING

# litellm is the ultimate source for OpenAI prices; skip the module if it's absent
# (it is present in the monorepo workspace, which is where this runs in CI).
litellm = pytest.importorskip("litellm")


def _keys_litellm_still_lists(provider_prefix: str) -> list[str]:
    """Return the MODEL_PRICING keys litellm's live map lists, asserting the rest are in its bundled map."""
    cost_map_module = litellm.litellm_core_utils.get_model_cost_map
    bundled_map = cost_map_module.GetModelCostMap.load_local_model_cost_map()
    loaded_map_source = cost_map_module.get_model_cost_map_source_info()
    keys = [key for key in MODEL_PRICING if key.startswith(provider_prefix)]
    retired_keys = [key for key in keys if key.removeprefix(provider_prefix) not in litellm.model_cost]
    unknown_keys = [key for key in retired_keys if key.removeprefix(provider_prefix) not in bundled_map]
    assert not unknown_keys, (
        f"priced by mngr_usage but in neither the map litellm loaded ({loaded_map_source}) "
        f"nor its bundled map: {unknown_keys}"
    )
    listed_keys = [key for key in keys if key not in retired_keys]
    # Guard: an empty list would make the caller's loop vacuously pass.
    assert listed_keys, f"no {provider_prefix}* entries in MODEL_PRICING that litellm still lists"
    return listed_keys


def test_openai_prices_match_litellm() -> None:
    model_cost = litellm.model_cost
    for key in _keys_litellm_still_lists("openai/"):
        litellm_entry = model_cost[key.removeprefix("openai/")]
        prices = MODEL_PRICING[key]
        assert prices.input_cost_per_token == litellm_entry["input_cost_per_token"], f"input price drift for {key}"
        assert prices.output_cost_per_token == litellm_entry["output_cost_per_token"], f"output price drift for {key}"
        assert prices.cache_read_input_token_cost == litellm_entry.get("cache_read_input_token_cost"), (
            f"cache_read price drift for {key}"
        )
        # litellm omits this bucket for models with no cache-write surcharge,
        # which prices as 0.
        assert prices.cache_creation_input_token_cost == (
            litellm_entry.get("cache_creation_input_token_cost") or 0.0
        ), f"cache_creation price drift for {key}"


def test_anthropic_prices_match_litellm() -> None:
    """Every Anthropic entry litellm still lists must match its map, which is what the proxy bills from.

    mngr_usage keeps its own table because it prices token-only usage sources
    (codex, pi) on machines that never import litellm -- but the numbers must be
    litellm's, since the proxy charges from that map. All four buckets are
    compared, cache-write included, because every Anthropic model bills one.
    """
    model_cost = litellm.model_cost
    for key in _keys_litellm_still_lists("anthropic/"):
        litellm_entry = model_cost[key.removeprefix("anthropic/")]
        prices = MODEL_PRICING[key]
        assert prices.input_cost_per_token == litellm_entry["input_cost_per_token"], f"input price drift for {key}"
        assert prices.output_cost_per_token == litellm_entry["output_cost_per_token"], f"output price drift for {key}"
        assert prices.cache_read_input_token_cost == litellm_entry.get("cache_read_input_token_cost"), (
            f"cache_read price drift for {key}"
        )
        assert prices.cache_creation_input_token_cost == litellm_entry.get("cache_creation_input_token_cost"), (
            f"cache_creation price drift for {key}"
        )
