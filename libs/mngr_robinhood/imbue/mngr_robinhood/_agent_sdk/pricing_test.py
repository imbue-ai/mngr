from imbue.mngr_robinhood._agent_sdk.pricing import accumulate_usage_totals
from imbue.mngr_robinhood._agent_sdk.pricing import compute_total_cost_usd
from imbue.mngr_robinhood._agent_sdk.pricing import resolve_model_pricing


def test_resolve_model_pricing_matches_family_for_dated_and_alias_ids() -> None:
    assert resolve_model_pricing("claude-haiku-4-5-20251001") is resolve_model_pricing("haiku")
    assert resolve_model_pricing("claude-sonnet-4-6") is resolve_model_pricing("sonnet")
    assert resolve_model_pricing("claude-opus-4-8") is resolve_model_pricing("opus")


def test_resolve_model_pricing_is_none_for_unknown_model() -> None:
    assert resolve_model_pricing("gpt-4o") is None


def test_compute_total_cost_usd_uses_per_token_rates() -> None:
    # 1M input + 1M output haiku tokens = $1.00 + $5.00 = $6.00.
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert compute_total_cost_usd("claude-haiku-4-5", usage) == 6.0


def test_compute_total_cost_usd_includes_cache_token_classes() -> None:
    # 1M cache-read + 1M cache-write haiku tokens = $0.10 + $1.25 = $1.35.
    usage = {"cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000}
    assert compute_total_cost_usd("claude-haiku-4-5", usage) == 1.35


def test_compute_total_cost_usd_is_positive_for_small_real_world_usage() -> None:
    usage = {"input_tokens": 12, "output_tokens": 8, "cache_read_input_tokens": 4096}
    cost = compute_total_cost_usd("claude-haiku-4-5", usage)
    assert cost is not None and cost > 0.0


def test_compute_total_cost_usd_is_none_for_unknown_model_or_empty_usage() -> None:
    assert compute_total_cost_usd("mystery-model", {"input_tokens": 100}) is None
    assert compute_total_cost_usd("claude-haiku-4-5", None) is None
    assert compute_total_cost_usd("claude-haiku-4-5", {}) is None


def test_accumulate_usage_totals_sums_only_token_fields() -> None:
    first = accumulate_usage_totals({}, {"input_tokens": 10, "output_tokens": 2, "service_tier": "standard"})
    second = accumulate_usage_totals(first, {"input_tokens": 5, "cache_read_input_tokens": 100})
    assert second == {"input_tokens": 15, "output_tokens": 2, "cache_read_input_tokens": 100}
