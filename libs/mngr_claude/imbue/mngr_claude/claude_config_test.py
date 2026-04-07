"""Unit tests for claude_config.py."""

import json
from pathlib import Path

import pytest

from imbue.mngr_claude.claude_config import ClaudeDirectoryNotTrustedError
from imbue.mngr_claude.claude_config import ClaudeEffortCalloutNotDismissedError
from imbue.mngr_claude.claude_config import acknowledge_cost_threshold
from imbue.mngr_claude.claude_config import add_claude_trust_for_path
from imbue.mngr_claude.claude_config import check_claude_dialogs_dismissed
from imbue.mngr_claude.claude_config import check_effort_callout_dismissed
from imbue.mngr_claude.claude_config import check_source_directory_trusted
from imbue.mngr_claude.claude_config import dismiss_effort_callout
from imbue.mngr_claude.claude_config import ensure_claude_dialogs_dismissed
from imbue.mngr_claude.claude_config import find_project_config
from imbue.mngr_claude.claude_config import get_claude_config_backup_path
from imbue.mngr_claude.claude_config import get_claude_config_dir
from imbue.mngr_claude.claude_config import get_claude_config_path
from imbue.mngr_claude.claude_config import get_user_claude_config_dir
from imbue.mngr_claude.claude_config import get_user_claude_config_path
from imbue.mngr_claude.claude_config import is_source_directory_trusted
from imbue.mngr_claude.claude_config import remove_claude_trust_for_path


def test_get_claude_config_path_returns_home_dot_claude_json() -> None:
    """Test that get_claude_config_path returns ~/.claude.json."""
    result = get_claude_config_path()
    assert result == Path.home() / ".claude.json"


def test_get_claude_config_backup_path_returns_home_dot_claude_json_bak() -> None:
    """Test that get_claude_config_backup_path returns ~/.claude.json.bak."""
    result = get_claude_config_backup_path()
    assert result == Path.home() / ".claude.json.bak"


def test_find_project_config_exact_match() -> None:
    """Test that find_project_config finds exact match."""
    projects = {
        "/Users/test/project1": {"allowedTools": ["bash"], "hasTrustDialogAccepted": True},
        "/Users/test/project2": {"allowedTools": [], "hasTrustDialogAccepted": False},
    }
    result = find_project_config(projects, Path("/Users/test/project1"))
    assert result == {"allowedTools": ["bash"], "hasTrustDialogAccepted": True}


def test_find_project_config_ancestor_match() -> None:
    """Test that find_project_config finds closest ancestor."""
    projects = {
        "/Users/test/project": {"allowedTools": ["bash"], "hasTrustDialogAccepted": True},
    }
    # Search for a subdirectory
    result = find_project_config(projects, Path("/Users/test/project/src/components"))
    assert result == {"allowedTools": ["bash"], "hasTrustDialogAccepted": True}


def test_find_project_config_no_match() -> None:
    """Test that find_project_config returns None when no match."""
    projects = {
        "/Users/test/project1": {"allowedTools": ["bash"], "hasTrustDialogAccepted": True},
    }
    result = find_project_config(projects, Path("/Users/other/project"))
    assert result is None


def test_find_project_config_empty_projects() -> None:
    """Test that find_project_config returns None for empty projects."""
    result = find_project_config({}, Path("/Users/test/project"))
    assert result is None


def test_check_source_directory_trusted_succeeds_when_trusted(tmp_path: Path) -> None:
    """Test that check_source_directory_trusted passes when directory is trusted."""
    config_file = get_claude_config_path()
    source_path = tmp_path / "source"
    source_path.mkdir()

    config = {
        "projects": {
            str(source_path): {"allowedTools": ["bash"], "hasTrustDialogAccepted": True},
        }
    }
    config_file.write_text(json.dumps(config, indent=2))

    # Should not raise
    check_source_directory_trusted(config_file, source_path)


def test_check_source_directory_trusted_succeeds_for_subdirectory(tmp_path: Path) -> None:
    """Test that check_source_directory_trusted passes for subdirectory of trusted path."""
    config_file = get_claude_config_path()
    project_root = tmp_path / "project"
    source_path = project_root / "src" / "components"
    project_root.mkdir()
    source_path.mkdir(parents=True)

    config = {
        "projects": {
            str(project_root): {"allowedTools": ["bash"], "hasTrustDialogAccepted": True},
        }
    }
    config_file.write_text(json.dumps(config, indent=2))

    # Should not raise - subdirectory inherits trust from ancestor
    check_source_directory_trusted(config_file, source_path)


def test_check_source_directory_trusted_raises_when_not_trusted(tmp_path: Path) -> None:
    """Test that check_source_directory_trusted raises when hasTrustDialogAccepted=false."""
    config_file = get_claude_config_path()
    source_path = tmp_path / "source"
    source_path.mkdir()

    config = {
        "projects": {
            str(source_path): {"allowedTools": ["bash"], "hasTrustDialogAccepted": False},
        }
    }
    config_file.write_text(json.dumps(config, indent=2))

    with pytest.raises(ClaudeDirectoryNotTrustedError) as exc_info:
        check_source_directory_trusted(config_file, source_path)

    assert str(source_path) in str(exc_info.value)


def test_check_source_directory_trusted_raises_when_no_config_file(tmp_path: Path) -> None:
    """Test that check_source_directory_trusted raises when ~/.claude.json doesn't exist."""
    source_path = tmp_path / "source"
    source_path.mkdir()

    # Config file doesn't exist (HOME points to tmp_path via autouse fixture)

    with pytest.raises(ClaudeDirectoryNotTrustedError):
        check_source_directory_trusted(get_claude_config_path(), source_path)


def test_check_source_directory_trusted_raises_when_empty_config(tmp_path: Path) -> None:
    """Test that check_source_directory_trusted raises when config file is empty."""
    config_file = get_claude_config_path()
    source_path = tmp_path / "source"
    source_path.mkdir()

    config_file.write_text("")

    with pytest.raises(ClaudeDirectoryNotTrustedError):
        check_source_directory_trusted(config_file, source_path)


def test_check_source_directory_trusted_raises_when_not_in_projects(tmp_path: Path) -> None:
    """Test that check_source_directory_trusted raises when source not in projects."""
    config_file = get_claude_config_path()
    source_path = tmp_path / "source"
    source_path.mkdir()

    config = {"projects": {"/other/project": {"allowedTools": [], "hasTrustDialogAccepted": True}}}
    config_file.write_text(json.dumps(config, indent=2))

    with pytest.raises(ClaudeDirectoryNotTrustedError):
        check_source_directory_trusted(config_file, source_path)


def test_check_source_directory_trusted_raises_when_trust_field_missing(tmp_path: Path) -> None:
    """Test that check_source_directory_trusted raises when hasTrustDialogAccepted is missing."""
    config_file = get_claude_config_path()
    source_path = tmp_path / "source"
    source_path.mkdir()

    config = {
        "projects": {
            str(source_path): {"allowedTools": ["bash"]},
        }
    }
    config_file.write_text(json.dumps(config, indent=2))

    with pytest.raises(ClaudeDirectoryNotTrustedError):
        check_source_directory_trusted(config_file, source_path)


def test_check_source_directory_trusted_raises_json_error_for_invalid_json() -> None:
    """Test that check_source_directory_trusted lets JSONDecodeError bubble up."""
    config_file = get_claude_config_path()

    config_file.write_text("{ invalid json }")

    with pytest.raises(json.JSONDecodeError):
        check_source_directory_trusted(config_file, Path("/some/path"))


# Tests for add_claude_trust_for_path


def test_add_claude_trust_creates_config_when_none_exists(tmp_path: Path) -> None:
    """Test that add_claude_trust_for_path creates ~/.claude.json if it doesn't exist."""
    source_path = tmp_path / "source"
    source_path.mkdir()

    # HOME points to a test-isolated temp dir (autouse setup_test_mngr_env)
    config_file = get_claude_config_path()
    assert not config_file.exists()

    add_claude_trust_for_path(config_file, source_path)

    assert config_file.exists()
    config = json.loads(config_file.read_text())
    assert config["projects"][str(source_path)]["hasTrustDialogAccepted"] is True


def test_add_claude_trust_adds_entry_to_existing_config(tmp_path: Path) -> None:
    """Test that add_claude_trust_for_path adds entry to existing config."""
    config_file = get_claude_config_path()
    source_path = tmp_path / "source"
    source_path.mkdir()

    # Create config with another project
    config = {"projects": {"/other/project": {"allowedTools": [], "hasTrustDialogAccepted": True}}}
    config_file.write_text(json.dumps(config, indent=2))

    add_claude_trust_for_path(config_file, source_path)

    updated = json.loads(config_file.read_text())
    # New entry added
    assert updated["projects"][str(source_path)]["hasTrustDialogAccepted"] is True
    # Existing entry preserved
    assert "/other/project" in updated["projects"]


def test_add_claude_trust_is_noop_when_already_trusted(tmp_path: Path) -> None:
    """Test that add_claude_trust_for_path is a no-op when path is already trusted."""
    config_file = get_claude_config_path()
    backup_file = get_claude_config_backup_path()
    source_path = tmp_path / "source"
    source_path.mkdir()

    # Create config with already-trusted source
    config = {
        "projects": {
            str(source_path): {"allowedTools": ["bash"], "hasTrustDialogAccepted": True},
        }
    }
    config_file.write_text(json.dumps(config, indent=2))

    add_claude_trust_for_path(config_file, source_path)

    # No backup should be created (no modification)
    assert not backup_file.exists()
    # Config should be unchanged
    updated = json.loads(config_file.read_text())
    assert updated["projects"][str(source_path)]["allowedTools"] == ["bash"]


def test_add_claude_trust_updates_entry_when_trust_is_false(tmp_path: Path) -> None:
    """Test that add_claude_trust_for_path updates entry when hasTrustDialogAccepted is False."""
    config_file = get_claude_config_path()
    source_path = tmp_path / "source"
    source_path.mkdir()

    # Create config with untrusted entry that has other fields
    config = {
        "projects": {
            str(source_path): {"allowedTools": ["bash"], "hasTrustDialogAccepted": False},
        }
    }
    config_file.write_text(json.dumps(config, indent=2))

    add_claude_trust_for_path(config_file, source_path)

    updated = json.loads(config_file.read_text())
    entry = updated["projects"][str(source_path)]
    # Trust should be set
    assert entry["hasTrustDialogAccepted"] is True
    # Other fields preserved
    assert entry["allowedTools"] == ["bash"]


def test_add_claude_trust_handles_empty_config_file(tmp_path: Path) -> None:
    """Test that add_claude_trust_for_path handles empty config file."""
    config_file = get_claude_config_path()
    source_path = tmp_path / "source"
    source_path.mkdir()

    config_file.write_text("")

    add_claude_trust_for_path(config_file, source_path)

    config = json.loads(config_file.read_text())
    assert config["projects"][str(source_path)]["hasTrustDialogAccepted"] is True


# Tests for remove_claude_trust_for_path


def test_remove_claude_trust_removes_mngr_created_entry(tmp_path: Path) -> None:
    """Test that remove_claude_trust_for_path removes mngr-created entries."""
    config_file = get_claude_config_path()
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    config = {
        "projects": {
            str(worktree_path): {
                "allowedTools": [],
                "hasTrustDialogAccepted": True,
                "_mngrCreated": True,
                "_mngrSourcePath": "/some/source",
            },
            "/other/project": {"allowedTools": [], "hasTrustDialogAccepted": True},
        }
    }
    config_file.write_text(json.dumps(config, indent=2))

    result = remove_claude_trust_for_path(config_file, worktree_path)

    assert result is True
    updated_config = json.loads(config_file.read_text())
    assert str(worktree_path) not in updated_config["projects"]
    # Other entries should remain
    assert "/other/project" in updated_config["projects"]


def test_remove_claude_trust_skips_non_mngr_entry(tmp_path: Path) -> None:
    """Test that remove_claude_trust_for_path skips entries not created by mngr."""
    config_file = get_claude_config_path()
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    config = {
        "projects": {
            str(worktree_path): {"allowedTools": [], "hasTrustDialogAccepted": True},
        }
    }
    config_file.write_text(json.dumps(config, indent=2))

    result = remove_claude_trust_for_path(config_file, worktree_path)

    # Should return False since it's not an mngr-created entry
    assert result is False
    # Entry should still exist
    updated_config = json.loads(config_file.read_text())
    assert str(worktree_path) in updated_config["projects"]


def test_remove_claude_trust_returns_false_when_not_found(tmp_path: Path) -> None:
    """Test that remove_claude_trust_for_path returns False when entry doesn't exist."""
    config_file = get_claude_config_path()
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    config = {
        "projects": {
            "/other/project": {"allowedTools": [], "hasTrustDialogAccepted": True},
        }
    }
    config_file.write_text(json.dumps(config, indent=2))

    result = remove_claude_trust_for_path(config_file, worktree_path)

    assert result is False


def test_remove_claude_trust_returns_false_when_no_config(tmp_path: Path) -> None:
    """Test that remove_claude_trust_for_path returns False when config doesn't exist."""
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    # Config file doesn't exist (HOME points to tmp_path via autouse fixture)

    result = remove_claude_trust_for_path(get_claude_config_path(), worktree_path)

    assert result is False


def test_remove_claude_trust_returns_false_on_error(tmp_path: Path) -> None:
    """Test that remove_claude_trust_for_path returns False on errors."""
    config_file = get_claude_config_path()
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    config_file.write_text("{ invalid json }")

    # Should not raise, but return False
    result = remove_claude_trust_for_path(config_file, worktree_path)

    assert result is False


def test_remove_claude_trust_returns_false_when_empty_config(tmp_path: Path) -> None:
    """Test that remove_claude_trust_for_path returns False when config file is empty."""
    config_file = get_claude_config_path()
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    config_file.write_text("")

    result = remove_claude_trust_for_path(config_file, worktree_path)

    assert result is False


# Tests for check_effort_callout_dismissed / dismiss_effort_callout


def test_check_effort_callout_dismissed_succeeds_when_dismissed() -> None:
    """Test that check_effort_callout_dismissed passes when effortCalloutDismissed is true."""
    config_file = get_claude_config_path()
    config = {"effortCalloutDismissed": True}
    config_file.write_text(json.dumps(config, indent=2))

    # Should not raise
    check_effort_callout_dismissed(config_file)


def test_check_effort_callout_dismissed_raises_when_not_dismissed() -> None:
    """Test that check_effort_callout_dismissed raises when effortCalloutDismissed is false."""
    config_file = get_claude_config_path()
    config = {"effortCalloutDismissed": False}
    config_file.write_text(json.dumps(config, indent=2))

    with pytest.raises(ClaudeEffortCalloutNotDismissedError):
        check_effort_callout_dismissed(config_file)


def test_check_effort_callout_dismissed_raises_when_field_missing() -> None:
    """Test that check_effort_callout_dismissed raises when effortCalloutDismissed is absent."""
    config_file = get_claude_config_path()
    config = {"projects": {}}
    config_file.write_text(json.dumps(config, indent=2))

    with pytest.raises(ClaudeEffortCalloutNotDismissedError):
        check_effort_callout_dismissed(config_file)


def test_check_effort_callout_dismissed_raises_when_no_config() -> None:
    """Test that check_effort_callout_dismissed raises when config file doesn't exist."""
    with pytest.raises(ClaudeEffortCalloutNotDismissedError):
        check_effort_callout_dismissed(get_claude_config_path())


def test_check_effort_callout_dismissed_raises_when_empty_config() -> None:
    """Test that check_effort_callout_dismissed raises when config file is empty."""
    config_file = get_claude_config_path()
    config_file.write_text("")

    with pytest.raises(ClaudeEffortCalloutNotDismissedError):
        check_effort_callout_dismissed(config_file)


def test_dismiss_effort_callout_sets_field() -> None:
    """Test that dismiss_effort_callout sets effortCalloutDismissed to true."""
    config_file = get_claude_config_path()
    config = {"projects": {}}
    config_file.write_text(json.dumps(config, indent=2))

    dismiss_effort_callout(config_file)

    updated = json.loads(config_file.read_text())
    assert updated["effortCalloutDismissed"] is True
    assert "projects" in updated


def test_dismiss_effort_callout_is_noop_when_already_set() -> None:
    """Test that dismiss_effort_callout is a no-op when already dismissed."""
    config_file = get_claude_config_path()
    backup_file = get_claude_config_backup_path()
    config = {"effortCalloutDismissed": True, "projects": {}}
    config_file.write_text(json.dumps(config, indent=2))

    dismiss_effort_callout(config_file)

    assert not backup_file.exists()


def test_dismiss_effort_callout_creates_config_when_none_exists() -> None:
    """Test that dismiss_effort_callout creates config file if it doesn't exist."""
    config_file = get_claude_config_path()
    assert not config_file.exists()

    dismiss_effort_callout(config_file)

    assert config_file.exists()
    config = json.loads(config_file.read_text())
    assert config["effortCalloutDismissed"] is True


def test_dismiss_effort_callout_handles_empty_config() -> None:
    """Test that dismiss_effort_callout handles empty config file."""
    config_file = get_claude_config_path()
    config_file.write_text("")

    dismiss_effort_callout(config_file)

    config = json.loads(config_file.read_text())
    assert config["effortCalloutDismissed"] is True


# Tests for acknowledge_cost_threshold


def test_acknowledge_cost_threshold_sets_field() -> None:
    """Test that acknowledge_cost_threshold sets hasAcknowledgedCostThreshold to true."""
    config_file = get_claude_config_path()
    config = {"projects": {}}
    config_file.write_text(json.dumps(config, indent=2))

    acknowledge_cost_threshold(config_file)

    updated = json.loads(config_file.read_text())
    assert updated["hasAcknowledgedCostThreshold"] is True
    assert "projects" in updated


def test_acknowledge_cost_threshold_is_noop_when_already_set() -> None:
    """Test that acknowledge_cost_threshold is a no-op when already acknowledged."""
    config_file = get_claude_config_path()
    backup_file = get_claude_config_backup_path()
    config = {"hasAcknowledgedCostThreshold": True, "projects": {}}
    config_file.write_text(json.dumps(config, indent=2))

    acknowledge_cost_threshold(config_file)

    assert not backup_file.exists()


def test_acknowledge_cost_threshold_creates_config_when_none_exists() -> None:
    """Test that acknowledge_cost_threshold creates config file if it doesn't exist."""
    config_file = get_claude_config_path()
    assert not config_file.exists()

    acknowledge_cost_threshold(config_file)

    assert config_file.exists()
    config = json.loads(config_file.read_text())
    assert config["hasAcknowledgedCostThreshold"] is True


def test_acknowledge_cost_threshold_handles_empty_config() -> None:
    """Test that acknowledge_cost_threshold handles empty config file."""
    config_file = get_claude_config_path()
    config_file.write_text("")

    acknowledge_cost_threshold(config_file)

    config = json.loads(config_file.read_text())
    assert config["hasAcknowledgedCostThreshold"] is True


# Tests for check_claude_dialogs_dismissed / ensure_claude_dialogs_dismissed


def test_check_claude_dialogs_dismissed_checks_trust(tmp_path: Path) -> None:
    """Test that check_claude_dialogs_dismissed checks trust for source_path."""
    config_file = get_claude_config_path()
    source_path = tmp_path / "source"
    source_path.mkdir()

    # Config has effort dismissed but source is NOT trusted
    config = {"effortCalloutDismissed": True, "projects": {}}
    config_file.write_text(json.dumps(config, indent=2))

    with pytest.raises(ClaudeDirectoryNotTrustedError):
        check_claude_dialogs_dismissed(config_file, source_path)


def test_check_claude_dialogs_dismissed_checks_effort_callout(tmp_path: Path) -> None:
    """Test that check_claude_dialogs_dismissed checks effort callout."""
    config_file = get_claude_config_path()
    source_path = tmp_path / "source"
    source_path.mkdir()

    # Config has trust but NOT effort dismissed
    config = {
        "projects": {
            str(source_path): {"hasTrustDialogAccepted": True},
        },
    }
    config_file.write_text(json.dumps(config, indent=2))

    with pytest.raises(ClaudeEffortCalloutNotDismissedError):
        check_claude_dialogs_dismissed(config_file, source_path)


def test_check_claude_dialogs_dismissed_passes_when_all_set(tmp_path: Path) -> None:
    """Test that check_claude_dialogs_dismissed passes when all dialogs are set."""
    config_file = get_claude_config_path()
    source_path = tmp_path / "source"
    source_path.mkdir()

    config = {
        "effortCalloutDismissed": True,
        "hasCompletedOnboarding": True,
        "bypassPermissionsModeAccepted": True,
        "projects": {
            str(source_path): {"hasTrustDialogAccepted": True},
        },
    }
    config_file.write_text(json.dumps(config, indent=2))

    check_claude_dialogs_dismissed(config_file, source_path)


def test_ensure_claude_dialogs_dismissed_sets_all(tmp_path: Path) -> None:
    """Test that ensure_claude_dialogs_dismissed sets all dialog fields."""
    config_file = get_claude_config_path()
    source_path = tmp_path / "source"
    source_path.mkdir()

    config = {"projects": {}}
    config_file.write_text(json.dumps(config, indent=2))

    ensure_claude_dialogs_dismissed(config_file, source_path)

    updated = json.loads(config_file.read_text())
    assert updated["effortCalloutDismissed"] is True
    assert updated["hasCompletedOnboarding"] is True
    assert updated["hasAcknowledgedCostThreshold"] is True
    assert updated["projects"][str(source_path)]["hasTrustDialogAccepted"] is True
    # bypassPermissionsModeAccepted is NOT set (Claude Code resets it;
    # skipDangerousModePermissionPrompt in settings.json handles this instead)


def test_functions_work_with_non_global_config_path(tmp_path: Path) -> None:
    """Test that trust functions work with a non-global config path (per-agent config)."""
    config_path = tmp_path / "agent_config" / ".claude.json"
    config_path.parent.mkdir(parents=True)
    source_path = tmp_path / "work"
    source_path.mkdir()

    # Should create the file at the custom path
    add_claude_trust_for_path(config_path, source_path)

    assert config_path.exists()
    config = json.loads(config_path.read_text())
    assert config["projects"][str(source_path)]["hasTrustDialogAccepted"] is True

    # Should read from the custom path
    assert is_source_directory_trusted(config_path, source_path) is True

    # Dismiss effort callout at custom path
    dismiss_effort_callout(config_path)
    updated = json.loads(config_path.read_text())
    assert updated["effortCalloutDismissed"] is True

    # Global config should be untouched
    global_config = get_claude_config_path()
    assert not global_config.exists()


# Tests for get_claude_config_dir
# Note: the autouse setup_test_mngr_env fixture clears CLAUDE_CONFIG_DIR
# and ORIGINAL_CLAUDE_CONFIG_DIR via isolate_home, so tests start clean.


def test_get_claude_config_dir_defaults_to_home_dot_claude() -> None:
    """Without CLAUDE_CONFIG_DIR, returns ~/.claude (autouse fixture already clears env)."""
    result = get_claude_config_dir()
    assert result == Path.home() / ".claude"


def test_get_claude_config_dir_respects_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With CLAUDE_CONFIG_DIR set, returns that path."""
    custom_dir = tmp_path / "custom-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom_dir))
    result = get_claude_config_dir()
    assert result == custom_dir


def test_get_claude_config_dir_ignores_empty_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty CLAUDE_CONFIG_DIR is treated as unset."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
    result = get_claude_config_dir()
    assert result == Path.home() / ".claude"


# Tests for get_user_claude_config_dir


def test_get_user_claude_config_dir_defaults_to_config_dir() -> None:
    """Without ORIGINAL_CLAUDE_CONFIG_DIR, falls back to get_claude_config_dir()."""
    result = get_user_claude_config_dir()
    assert result == Path.home() / ".claude"


def test_get_user_claude_config_dir_respects_original_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With ORIGINAL_CLAUDE_CONFIG_DIR set, returns that path even if CLAUDE_CONFIG_DIR differs."""
    user_dir = tmp_path / "user-claude"
    agent_dir = tmp_path / "agent-claude"
    monkeypatch.setenv("ORIGINAL_CLAUDE_CONFIG_DIR", str(user_dir))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(agent_dir))
    result = get_user_claude_config_dir()
    assert result == user_dir


def test_get_user_claude_config_dir_falls_back_to_claude_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ORIGINAL_CLAUDE_CONFIG_DIR, uses CLAUDE_CONFIG_DIR."""
    custom_dir = tmp_path / "custom-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom_dir))
    result = get_user_claude_config_dir()
    assert result == custom_dir


# Tests for get_claude_config_path (CLAUDE_CONFIG_DIR-aware)


def test_get_claude_config_path_respects_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With CLAUDE_CONFIG_DIR set, returns $CLAUDE_CONFIG_DIR/.claude.json."""
    custom_dir = tmp_path / "custom-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom_dir))
    result = get_claude_config_path()
    assert result == custom_dir / ".claude.json"


def test_get_claude_config_backup_path_derives_from_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backup path should follow the config path location."""
    custom_dir = tmp_path / "custom-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom_dir))
    result = get_claude_config_backup_path()
    assert result == custom_dir / ".claude.json.bak"


# Tests for get_user_claude_config_path


def test_get_user_claude_config_path_defaults_to_home() -> None:
    """Without env vars, returns ~/.claude.json."""
    result = get_user_claude_config_path()
    assert result == Path.home() / ".claude.json"


def test_get_user_claude_config_path_respects_original_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ORIGINAL_CLAUDE_CONFIG_DIR set, returns path inside that dir."""
    user_dir = tmp_path / "user-claude"
    agent_dir = tmp_path / "agent-claude"
    monkeypatch.setenv("ORIGINAL_CLAUDE_CONFIG_DIR", str(user_dir))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(agent_dir))
    result = get_user_claude_config_path()
    assert result == user_dir / ".claude.json"


def test_get_user_claude_config_path_falls_back_to_claude_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ORIGINAL_CLAUDE_CONFIG_DIR, uses CLAUDE_CONFIG_DIR."""
    custom_dir = tmp_path / "custom-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom_dir))
    result = get_user_claude_config_path()
    assert result == custom_dir / ".claude.json"
