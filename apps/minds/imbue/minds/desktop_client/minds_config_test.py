import logging
from pathlib import Path

import pytest

from imbue.minds.desktop_client.design_tokens import WorkspaceColor
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.errors import MindsConfigError


def _make_config(tmp_path: Path) -> MindsConfig:
    return MindsConfig(data_dir=tmp_path)


def test_default_values_when_no_file(tmp_path: Path) -> None:
    """Default values are returned when config.toml does not exist."""
    config = _make_config(tmp_path)
    assert config.get_default_account_id() is None
    assert config.get_auto_open_requests_panel() is True


def test_set_and_get_default_account_id(tmp_path: Path) -> None:
    """Setting and getting default_account_id works correctly."""
    config = _make_config(tmp_path)
    config.set_default_account_id("user-123")
    assert config.get_default_account_id() == "user-123"


def test_clear_default_account_id(tmp_path: Path) -> None:
    """Clearing the default account sets it to None."""
    config = _make_config(tmp_path)
    config.set_default_account_id("user-123")
    config.set_default_account_id(None)
    assert config.get_default_account_id() is None


def test_set_and_get_auto_open_requests_panel(tmp_path: Path) -> None:
    """Setting auto_open_requests_panel persists correctly."""
    config = _make_config(tmp_path)
    config.set_auto_open_requests_panel(False)
    assert config.get_auto_open_requests_panel() is False

    config.set_auto_open_requests_panel(True)
    assert config.get_auto_open_requests_panel() is True


def test_persistence_across_instances(tmp_path: Path) -> None:
    """Config written by one instance is readable by a new instance."""
    config1 = _make_config(tmp_path)
    config1.set_default_account_id("user-abc")
    config1.set_auto_open_requests_panel(False)

    config2 = _make_config(tmp_path)
    assert config2.get_default_account_id() == "user-abc"
    assert config2.get_auto_open_requests_panel() is False


def test_corrupt_toml_raises(tmp_path: Path) -> None:
    """A corrupt config.toml raises MindsConfigError rather than silently
    returning defaults. Silent fallback would hide data corruption -- e.g.
    the next ``set_*`` call would overwrite the unparseable file with a
    fresh one derived from an empty dict, losing whatever the user had
    intended to be stored.
    """
    config = _make_config(tmp_path)
    (tmp_path / "config.toml").write_text("not valid toml {{{")
    with pytest.raises(MindsConfigError):
        config.get_default_account_id()
    with pytest.raises(MindsConfigError):
        config.get_auto_open_requests_panel()


def test_multiple_settings_coexist(tmp_path: Path) -> None:
    """Setting one value does not clobber other values."""
    config = _make_config(tmp_path)
    config.set_default_account_id("user-xyz")
    config.set_auto_open_requests_panel(False)

    assert config.get_default_account_id() == "user-xyz"
    assert config.get_auto_open_requests_panel() is False

    config.set_default_account_id("user-new")
    assert config.get_auto_open_requests_panel() is False


# -- workspace colors --


def test_get_workspace_color_materializes_oklch_on_first_read(tmp_path: Path) -> None:
    """First read for an unknown agent persists the OKLCH starting color."""
    config = _make_config(tmp_path)
    color = config.get_workspace_color("agent-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert color.startswith("oklch(75%")
    # Second read returns the same value -- it's been persisted.
    second = config.get_workspace_color("agent-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert second == color


def test_set_and_get_workspace_color_preset_round_trips(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.set_workspace_color("agent-test", WorkspaceColor("confusion"))
    assert config.get_workspace_color("agent-test") == "confusion"


def test_set_and_get_workspace_color_hex_round_trips(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.set_workspace_color("agent-test", WorkspaceColor("#9FBBD3"))
    assert config.get_workspace_color("agent-test") == "#9FBBD3"


def test_set_and_get_workspace_color_oklch_round_trips(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    color = WorkspaceColor("oklch(75% 0.15 230)")
    config.set_workspace_color("agent-test", color)
    assert config.get_workspace_color("agent-test") == "oklch(75% 0.15 230)"


def test_workspace_color_persists_across_instances(tmp_path: Path) -> None:
    config1 = _make_config(tmp_path)
    config1.set_workspace_color("agent-x", WorkspaceColor("peace"))
    config2 = _make_config(tmp_path)
    assert config2.get_workspace_color("agent-x") == "peace"


def test_workspace_color_unparseable_falls_back_to_oklch_with_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Corrupt stored value falls back to OKLCH starting color and logs a warning."""
    config = _make_config(tmp_path)
    # Hand-write a corrupt entry.
    (tmp_path / "config.toml").write_text(
        '[workspace_colors]\n"agent-bad" = "not-a-color"\n'
    )
    with caplog.at_level(logging.WARNING, logger="imbue.minds.desktop_client.minds_config"):
        color = config.get_workspace_color("agent-bad")
    assert color.startswith("oklch(75%")
    assert "Unparseable workspace color" in caplog.text
    # The bad value is left in place so the user can see + correct it
    # rather than having it silently replaced.
    raw = (tmp_path / "config.toml").read_text()
    assert "not-a-color" in raw


def test_remove_workspace_color_removes_entry(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    config.set_workspace_color("agent-x", WorkspaceColor("peace"))
    config.remove_workspace_color("agent-x")
    # Subsequent get re-materializes OKLCH for the now-empty entry.
    color = config.get_workspace_color("agent-x")
    assert color.startswith("oklch(75%")


def test_remove_workspace_color_is_idempotent(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    # No-op when the agent has no entry and the table is missing entirely.
    config.remove_workspace_color("agent-never-seen")
    # No-op when the table exists but the agent is absent.
    config.set_workspace_color("agent-x", WorkspaceColor("peace"))
    config.remove_workspace_color("agent-y")
    assert config.get_workspace_color("agent-x") == "peace"


def test_remove_workspace_color_drops_empty_table(tmp_path: Path) -> None:
    """Removing the last entry collapses the table to keep the config tidy."""
    config = _make_config(tmp_path)
    config.set_workspace_color("agent-x", WorkspaceColor("peace"))
    config.remove_workspace_color("agent-x")
    raw = (tmp_path / "config.toml").read_text()
    assert "workspace_colors" not in raw


def test_workspace_color_coexists_with_other_settings(tmp_path: Path) -> None:
    """Workspace color writes don't clobber default_account_id or other keys."""
    config = _make_config(tmp_path)
    config.set_default_account_id("user-1")
    config.set_workspace_color("agent-x", WorkspaceColor("confusion"))
    assert config.get_default_account_id() == "user-1"
    assert config.get_workspace_color("agent-x") == "confusion"
