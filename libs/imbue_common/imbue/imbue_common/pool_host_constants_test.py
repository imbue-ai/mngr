from imbue.imbue_common.pool_host_constants import PLACEHOLDER_ANTHROPIC_API_KEY


def test_placeholder_anthropic_api_key_has_correct_prefix() -> None:
    assert PLACEHOLDER_ANTHROPIC_API_KEY.startswith("sk-ant-api03-")


def test_placeholder_anthropic_api_key_has_realistic_length() -> None:
    assert len(PLACEHOLDER_ANTHROPIC_API_KEY) >= 100
