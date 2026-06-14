from imbue.mngr_notifications.config import NotificationsPluginConfig


def test_default_config_has_no_terminal() -> None:
    config = NotificationsPluginConfig()
    assert config.terminal_app is None
    assert config.custom_terminal_command is None
    assert config.enabled is True
