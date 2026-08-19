import pytest

from imbue.mngr_usage.data_types import TokenSnapshot
from imbue.mngr_usage.pricing import MODEL_PRICING
from imbue.mngr_usage.pricing import compute_cost


def test_compute_cost_matches_live_pi_reported_total() -> None:
    # The exact token counts + cost from a live pi (claude-opus-4-8) cache-hit
    # turn. pi computed total=0.00488275 client-side; our table must reproduce
    # it to the digit, anchoring the curated prices to an observed ground truth.
    tokens = TokenSnapshot(input=2, output=7, cache_read=9133, cache_creation=21)
    assert compute_cost("anthropic/claude-opus-4-8", tokens) == pytest.approx(0.00488275)


def test_compute_cost_sums_each_bucket_at_its_own_rate() -> None:
    # 1M tokens in each bucket so each price surfaces as a whole-dollar figure.
    one_million = TokenSnapshot(input=1_000_000, output=1_000_000, cache_read=1_000_000, cache_creation=1_000_000)
    # Opus: 5 (input) + 25 (output) + 0.5 (cache_read) + 6.25 (cache_creation).
    assert compute_cost("anthropic/claude-opus-4-8", one_million) == pytest.approx(36.75)


def test_compute_cost_unknown_model_is_none_not_zero() -> None:
    tokens = TokenSnapshot(input=100, output=100, cache_read=0, cache_creation=0)
    assert compute_cost("openai/gpt-does-not-exist", tokens) is None
    # An unqualified (provider-less) key must not resolve against the canonical table.
    assert compute_cost("claude-opus-4-8", tokens) is None


def test_compute_cost_all_none_tokens_is_zero_for_known_model() -> None:
    assert compute_cost("anthropic/claude-opus-4-8", TokenSnapshot()) == pytest.approx(0.0)


def test_legacy_opus_costs_three_times_current_opus() -> None:
    tokens = TokenSnapshot(input=10_000, output=2_000, cache_read=500, cache_creation=100)
    current = compute_cost("anthropic/claude-opus-4-8", tokens)
    legacy = compute_cost("anthropic/claude-opus-4-1", tokens)
    assert current is not None and legacy is not None
    assert legacy == pytest.approx(current * 3)


def test_all_pricing_keys_are_provider_qualified() -> None:
    # The canonical key is "<provider>/<model>"; a bare model name must not appear.
    for key in MODEL_PRICING:
        assert "/" in key, f"pricing key {key!r} is not provider-qualified"
