from pathlib import Path

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr.config.data_types import MngrContext
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
    assert _is_observe_running(temp_mngr_ctx) is False


def test_is_observe_running_reports_a_probe_it_could_not_answer(
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A probe that cannot answer starts an observer anyway, and says why.

    Assuming one is already running would leave ``mngr notify`` waiting forever on
    events nobody writes, whereas starting a redundant one announces itself: the
    child exits immediately on the lock and the watcher reports it.

    A regular file where the host dir should be makes the lock open fail with
    ENOTDIR, standing in for the permission problem this path really guards against
    while staying deterministic and unaffected by running as root.
    """
    not_a_directory = tmp_path / "host-dir-is-a-file"
    not_a_directory.write_text("")
    unprobeable_config = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().default_host_dir, not_a_directory),
    )
    unprobeable_ctx = temp_mngr_ctx.model_copy_update(
        to_update(temp_mngr_ctx.field_ref().config, unprobeable_config),
    )

    assert _is_observe_running(unprobeable_ctx) is False
    assert "Could not tell whether mngr observe is already running" in capsys.readouterr().out


# --- _ensure_observe ---


def test_ensure_observe_starts_process_when_not_running(temp_mngr_ctx: MngrContext) -> None:
    """When observe is not running, _ensure_observe starts it and yields a process handle."""
    with _ensure_observe(temp_mngr_ctx) as process:
        assert process is not None
