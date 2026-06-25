from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.utils.polling import wait_for
from imbue.mngr_notifications.cli import _ensure_observe
from imbue.mngr_notifications.cli import _get_plugin_config
from imbue.mngr_notifications.cli import _is_observe_running
from imbue.mngr_notifications.config import NotificationsPluginConfig

# --- _get_plugin_config ---


def test_get_plugin_config_returns_default_when_missing(temp_mngr_ctx: MngrContext) -> None:
    """Returns a default config when no notifications plugin is configured."""
    config = _get_plugin_config(temp_mngr_ctx)
    assert isinstance(config, NotificationsPluginConfig)
    assert config.notification_only is False


# --- _is_observe_running ---


def test_is_observe_running_returns_false_when_no_observe(temp_mngr_ctx: MngrContext) -> None:
    """When no observe process holds the lock, returns False."""
    result = _is_observe_running(temp_mngr_ctx)
    assert result is False


# --- _ensure_observe ---


def test_ensure_observe_starts_and_cleans_up_process_when_not_running(temp_mngr_ctx: MngrContext) -> None:
    """When observe is not running, _ensure_observe launches a real background process
    that is actually running inside the context, and terminates it on exit."""
    assert _is_observe_running(temp_mngr_ctx) is False

    with _ensure_observe(temp_mngr_ctx) as process:
        assert process is not None
        # A real process was launched and is still running (not immediately exited),
        # so this is not just the "already running -> yield None" branch.
        assert process.returncode is None

    # The context manager must terminate the process it started.
    wait_for(
        lambda: process.returncode is not None,
        timeout=10,
        poll_interval=0.05,
        error_message="_ensure_observe did not terminate the observe process on context exit",
    )
