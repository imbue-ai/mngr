import pytest

from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr_notifications.config import NotificationsPluginConfig
from imbue.mngr_notifications.errors import MisconfiguredPluginError


def test_default_config_has_no_terminal() -> None:
    config = NotificationsPluginConfig()
    assert config.terminal_app is None
    assert config.custom_terminal_command is None
    assert config.enabled is True


def test_merge_with_override_terminal_app() -> None:
    base = NotificationsPluginConfig()
    override = NotificationsPluginConfig(terminal_app="iTerm")

    merged = base.merge_with(override)

    assert isinstance(merged, NotificationsPluginConfig)
    assert merged.terminal_app == "iTerm"
    assert merged.custom_terminal_command is None


def test_merge_with_override_custom_command() -> None:
    base = NotificationsPluginConfig(terminal_app="Terminal")
    override = NotificationsPluginConfig(custom_terminal_command="my-cmd $MNGR_AGENT_NAME")

    merged = base.merge_with(override)

    assert isinstance(merged, NotificationsPluginConfig)
    assert merged.terminal_app == "Terminal"
    assert merged.custom_terminal_command == "my-cmd $MNGR_AGENT_NAME"


def test_merge_with_foreign_subtype_raises() -> None:
    """Merging with a non-NotificationsPluginConfig fails loudly rather than silently dropping the override."""
    base = NotificationsPluginConfig(terminal_app="iTerm")
    other = PluginConfig(enabled=False)

    with pytest.raises(MisconfiguredPluginError):
        base.merge_with(other)


def test_merge_with_preserves_base_when_override_is_none() -> None:
    base = NotificationsPluginConfig(terminal_app="iTerm", custom_terminal_command="my-cmd")
    override = NotificationsPluginConfig()

    merged = base.merge_with(override)

    assert merged.terminal_app == "iTerm"
    assert merged.custom_terminal_command == "my-cmd"
