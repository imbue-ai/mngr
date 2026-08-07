from imbue.mngr_notifications.config import NotificationsPluginConfig


def test_default_config_has_no_terminal() -> None:
    # Intentionally pins the user-facing defaults (no terminal configured, plugin
    # enabled): these are part of the plugin's public contract, so an accidental
    # change to a default should fail here. (Not a tautology -- nothing is passed in.)
    config = NotificationsPluginConfig()
    assert config.terminal_app is None
    assert config.custom_terminal_command is None
    assert config.enabled is True
