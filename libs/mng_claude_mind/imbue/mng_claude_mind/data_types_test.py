"""Unit tests for claude mind data types."""

from imbue.mng_claude_mind.data_types import ClaudeMindSettings
from imbue.mng_mind.data_types import WatcherSettings


def test_claude_mind_settings_defaults() -> None:
    settings = ClaudeMindSettings()
    assert settings.agent_type is None
    assert settings.watchers == WatcherSettings()


def test_claude_mind_settings_with_agent_type() -> None:
    settings = ClaudeMindSettings.model_validate({"agent_type": "claude-mind"})
    assert settings.agent_type == "claude-mind"
