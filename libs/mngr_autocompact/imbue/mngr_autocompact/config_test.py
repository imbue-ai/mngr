from imbue.mngr_autocompact.config import AutoCompactPluginConfig
from imbue.mngr_autocompact.config import ContextCompactionMode


def test_autocompact_config_defaults() -> None:
    config = AutoCompactPluginConfig()
    assert config.mode == ContextCompactionMode.DISABLED
    assert config.cache_ttl_minutes is None
    assert config.epsilon_offset_minutes == 3
    assert config.min_context_tokens == 100_000
    assert config.get_trigger_delay_seconds(60) == 57 * 60.0


def test_autocompact_config_trigger_delay() -> None:
    config = AutoCompactPluginConfig(
        mode=ContextCompactionMode.PROACTIVE_TIMER,
        cache_ttl_minutes=120,
        epsilon_offset_minutes=5,
        min_context_tokens=50_000,
    )
    assert config.get_trigger_delay_seconds(120) == 115 * 60.0
    # Epsilon greater than ttl returns 0
    assert config.get_trigger_delay_seconds(3) == 0.0


def test_autocompact_config_normalize_mode() -> None:
    assert (
        AutoCompactPluginConfig.model_validate({"mode": "proactive_timer"}).mode
        == ContextCompactionMode.PROACTIVE_TIMER
    )
    assert (
        AutoCompactPluginConfig.model_validate({"mode": "on_next_prompt"}).mode == ContextCompactionMode.ON_NEXT_PROMPT
    )
    assert AutoCompactPluginConfig.model_validate({"mode": "disabled"}).mode == ContextCompactionMode.DISABLED
    # Non-string input passes through unchanged
    assert AutoCompactPluginConfig._normalize_mode(123) == 123
