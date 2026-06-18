import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr.primitives import PluginName
from imbue.mngr_notifications.cli import _ensure_observe
from imbue.mngr_notifications.cli import _get_plugin_config
from imbue.mngr_notifications.cli import _is_observe_running
from imbue.mngr_notifications.config import NotificationsPluginConfig
from imbue.mngr_notifications.errors import MisconfiguredPluginError

# --- _get_plugin_config ---


def test_get_plugin_config_returns_default_when_missing(temp_mngr_ctx: MngrContext) -> None:
    """Returns a default config when no notifications plugin is configured."""
    config = _get_plugin_config(temp_mngr_ctx)
    assert isinstance(config, NotificationsPluginConfig)
    assert config.notification_only is False


def test_get_plugin_config_returns_registered_config(temp_mngr_ctx: MngrContext) -> None:
    """Returns the user's configured NotificationsPluginConfig when present."""
    configured = NotificationsPluginConfig(terminal_app="iTerm")
    plugins = {**temp_mngr_ctx.config.plugins, PluginName("notifications"): configured}
    ctx = temp_mngr_ctx.model_copy_update(("config", temp_mngr_ctx.config.model_copy_update(("plugins", plugins))))

    assert _get_plugin_config(ctx) is configured


def test_get_plugin_config_raises_on_type_mismatch(temp_mngr_ctx: MngrContext) -> None:
    """A wrong-typed config registered under 'notifications' fails loudly instead of silently using defaults."""
    wrong = PluginConfig(enabled=False)
    plugins = {**temp_mngr_ctx.config.plugins, PluginName("notifications"): wrong}
    ctx = temp_mngr_ctx.model_copy_update(("config", temp_mngr_ctx.config.model_copy_update(("plugins", plugins))))

    with pytest.raises(MisconfiguredPluginError):
        _get_plugin_config(ctx)


# --- _is_observe_running ---


def test_is_observe_running_returns_false_when_no_observe(temp_mngr_ctx: MngrContext) -> None:
    """When no observe process holds the lock, returns False."""
    result = _is_observe_running(temp_mngr_ctx)
    assert result is False


# --- _ensure_observe ---


def test_ensure_observe_starts_process_when_not_running(temp_mngr_ctx: MngrContext) -> None:
    """When observe is not running, _ensure_observe starts it and yields a process handle."""
    with _ensure_observe(temp_mngr_ctx) as process:
        assert process is not None
