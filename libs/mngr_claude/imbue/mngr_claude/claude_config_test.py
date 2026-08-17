"""Unit tests for claude_config.py."""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr_claude.claude_config import ClaudeDirectoryNotTrustedError
from imbue.mngr_claude.claude_config import ClaudeEffortCalloutNotDismissedError
from imbue.mngr_claude.claude_config import acknowledge_cost_threshold
from imbue.mngr_claude.claude_config import add_claude_trust_for_path
from imbue.mngr_claude.claude_config import auto_dismiss_claude_dialogs
from imbue.mngr_claude.claude_config import build_permission_auto_allow_hooks_config
from imbue.mngr_claude.claude_config import check_claude_dialogs_dismissed
from imbue.mngr_claude.claude_config import check_effort_callout_dismissed
from imbue.mngr_claude.claude_config import check_source_directory_trusted
from imbue.mngr_claude.claude_config import dismiss_effort_callout
from imbue.mngr_claude.claude_config import encode_claude_project_dir_name
from imbue.mngr_claude.claude_config import ensure_chat_cancel_tap_keybinding
from imbue.mngr_claude.claude_config import find_project_config
from imbue.mngr_claude.claude_config import find_user_config_in_isolated_mode
from imbue.mngr_claude.claude_config import find_user_config_in_unisolated_mode
from imbue.mngr_claude.claude_config import get_claude_config_dir
from imbue.mngr_claude.claude_config import get_user_claude_config_dir
from imbue.mngr_claude.claude_config import is_source_directory_trusted
from imbue.mngr_claude.claude_config import is_tap_binding_active
from imbue.mngr_claude.claude_config import mark_claude_agent_idle
from imbue.mngr_claude.claude_config import remove_claude_trust_for_path
from imbue.mngr_claude.claude_config import resolve_shared_claude_config_dir


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
    config_file = find_user_config_in_isolated_mode()
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
    config_file = find_user_config_in_isolated_mode()
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
    config_file = find_user_config_in_isolated_mode()
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
        check_source_directory_trusted(find_user_config_in_isolated_mode(), source_path)


def test_check_source_directory_trusted_raises_when_empty_config(tmp_path: Path) -> None:
    """Test that check_source_directory_trusted raises when config file is empty."""
    config_file = find_user_config_in_isolated_mode()
    source_path = tmp_path / "source"
    source_path.mkdir()

    config_file.write_text("")

    with pytest.raises(ClaudeDirectoryNotTrustedError):
        check_source_directory_trusted(config_file, source_path)


def test_check_source_directory_trusted_raises_when_not_in_projects(tmp_path: Path) -> None:
    """Test that check_source_directory_trusted raises when source not in projects."""
    config_file = find_user_config_in_isolated_mode()
    source_path = tmp_path / "source"
    source_path.mkdir()

    config = {"projects": {"/other/project": {"allowedTools": [], "hasTrustDialogAccepted": True}}}
    config_file.write_text(json.dumps(config, indent=2))

    with pytest.raises(ClaudeDirectoryNotTrustedError):
        check_source_directory_trusted(config_file, source_path)


def test_check_source_directory_trusted_raises_when_trust_field_missing(tmp_path: Path) -> None:
    """Test that check_source_directory_trusted raises when hasTrustDialogAccepted is missing."""
    config_file = find_user_config_in_isolated_mode()
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
    config_file = find_user_config_in_isolated_mode()

    config_file.write_text("{ invalid json }")

    with pytest.raises(json.JSONDecodeError):
        check_source_directory_trusted(config_file, Path("/some/path"))


# Tests for add_claude_trust_for_path


def test_add_claude_trust_creates_config_when_none_exists(tmp_path: Path) -> None:
    """Test that add_claude_trust_for_path creates ~/.claude.json if it doesn't exist."""
    source_path = tmp_path / "source"
    source_path.mkdir()

    # HOME points to a test-isolated temp dir (autouse setup_test_mngr_env)
    config_file = find_user_config_in_isolated_mode()
    assert not config_file.exists()

    add_claude_trust_for_path(config_file, source_path)

    assert config_file.exists()
    config = json.loads(config_file.read_text())
    assert config["projects"][str(source_path)]["hasTrustDialogAccepted"] is True


def test_add_claude_trust_adds_entry_to_existing_config(tmp_path: Path) -> None:
    """Test that add_claude_trust_for_path adds entry to existing config."""
    config_file = find_user_config_in_isolated_mode()
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
    config_file = find_user_config_in_isolated_mode()
    backup_file = find_user_config_in_isolated_mode().with_suffix(".json.bak")
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
    config_file = find_user_config_in_isolated_mode()
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
    config_file = find_user_config_in_isolated_mode()
    source_path = tmp_path / "source"
    source_path.mkdir()

    config_file.write_text("")

    add_claude_trust_for_path(config_file, source_path)

    config = json.loads(config_file.read_text())
    assert config["projects"][str(source_path)]["hasTrustDialogAccepted"] is True


# Tests for remove_claude_trust_for_path


def test_remove_claude_trust_removes_mngr_created_entry(tmp_path: Path) -> None:
    """Test that remove_claude_trust_for_path removes mngr-created entries."""
    config_file = find_user_config_in_isolated_mode()
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
    config_file = find_user_config_in_isolated_mode()
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
    config_file = find_user_config_in_isolated_mode()
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

    result = remove_claude_trust_for_path(find_user_config_in_isolated_mode(), worktree_path)

    assert result is False


def test_remove_claude_trust_returns_false_on_invalid_json(tmp_path: Path) -> None:
    """Test that remove_claude_trust_for_path returns False on invalid JSON.

    The JSON-decode path is the only error the production code catches.
    """
    config_file = find_user_config_in_isolated_mode()
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    config_file.write_text("{ invalid json }")

    # Should not raise, but return False
    result = remove_claude_trust_for_path(config_file, worktree_path)

    assert result is False


def test_remove_claude_trust_returns_false_when_empty_config(tmp_path: Path) -> None:
    """Test that remove_claude_trust_for_path returns False when config file is empty."""
    config_file = find_user_config_in_isolated_mode()
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()

    config_file.write_text("")

    result = remove_claude_trust_for_path(config_file, worktree_path)

    assert result is False


# Tests for check_effort_callout_dismissed / dismiss_effort_callout


def test_check_effort_callout_dismissed_succeeds_when_dismissed() -> None:
    """Test that check_effort_callout_dismissed passes when effortCalloutDismissed is true."""
    config_file = find_user_config_in_isolated_mode()
    config = {"effortCalloutDismissed": True}
    config_file.write_text(json.dumps(config, indent=2))

    # Should not raise
    check_effort_callout_dismissed(config_file)


def test_check_effort_callout_dismissed_raises_when_not_dismissed() -> None:
    """Test that check_effort_callout_dismissed raises when effortCalloutDismissed is false."""
    config_file = find_user_config_in_isolated_mode()
    config = {"effortCalloutDismissed": False}
    config_file.write_text(json.dumps(config, indent=2))

    with pytest.raises(ClaudeEffortCalloutNotDismissedError):
        check_effort_callout_dismissed(config_file)


def test_check_effort_callout_dismissed_raises_when_field_missing() -> None:
    """Test that check_effort_callout_dismissed raises when effortCalloutDismissed is absent."""
    config_file = find_user_config_in_isolated_mode()
    config = {"projects": {}}
    config_file.write_text(json.dumps(config, indent=2))

    with pytest.raises(ClaudeEffortCalloutNotDismissedError):
        check_effort_callout_dismissed(config_file)


def test_check_effort_callout_dismissed_raises_when_no_config() -> None:
    """Test that check_effort_callout_dismissed raises when config file doesn't exist."""
    with pytest.raises(ClaudeEffortCalloutNotDismissedError):
        check_effort_callout_dismissed(find_user_config_in_isolated_mode())


def test_check_effort_callout_dismissed_raises_when_empty_config() -> None:
    """Test that check_effort_callout_dismissed raises when config file is empty."""
    config_file = find_user_config_in_isolated_mode()
    config_file.write_text("")

    with pytest.raises(ClaudeEffortCalloutNotDismissedError):
        check_effort_callout_dismissed(config_file)


def test_dismiss_effort_callout_sets_field() -> None:
    """Test that dismiss_effort_callout sets effortCalloutDismissed to true."""
    config_file = find_user_config_in_isolated_mode()
    config = {"projects": {}}
    config_file.write_text(json.dumps(config, indent=2))

    dismiss_effort_callout(config_file)

    updated = json.loads(config_file.read_text())
    assert updated["effortCalloutDismissed"] is True
    assert "projects" in updated


def test_dismiss_effort_callout_is_noop_when_already_set() -> None:
    """Test that dismiss_effort_callout is a no-op when already dismissed."""
    config_file = find_user_config_in_isolated_mode()
    backup_file = find_user_config_in_isolated_mode().with_suffix(".json.bak")
    config = {"effortCalloutDismissed": True, "projects": {}}
    config_file.write_text(json.dumps(config, indent=2))
    content_before = config_file.read_text()

    dismiss_effort_callout(config_file)

    # The config file content must be byte-for-byte unchanged across the no-op call.
    assert config_file.read_text() == content_before
    # Secondary check: a no-op write would have created a .bak backup; it must not exist.
    assert not backup_file.exists()


def test_dismiss_effort_callout_creates_config_when_none_exists() -> None:
    """Test that dismiss_effort_callout creates config file if it doesn't exist."""
    config_file = find_user_config_in_isolated_mode()
    assert not config_file.exists()

    dismiss_effort_callout(config_file)

    assert config_file.exists()
    config = json.loads(config_file.read_text())
    assert config["effortCalloutDismissed"] is True


def test_dismiss_effort_callout_handles_empty_config() -> None:
    """Test that dismiss_effort_callout handles empty config file."""
    config_file = find_user_config_in_isolated_mode()
    config_file.write_text("")

    dismiss_effort_callout(config_file)

    config = json.loads(config_file.read_text())
    assert config["effortCalloutDismissed"] is True


# Tests for acknowledge_cost_threshold


def test_acknowledge_cost_threshold_sets_field() -> None:
    """Test that acknowledge_cost_threshold sets hasAcknowledgedCostThreshold to true."""
    config_file = find_user_config_in_isolated_mode()
    config = {"projects": {}}
    config_file.write_text(json.dumps(config, indent=2))

    acknowledge_cost_threshold(config_file)

    updated = json.loads(config_file.read_text())
    assert updated["hasAcknowledgedCostThreshold"] is True
    assert "projects" in updated


def test_acknowledge_cost_threshold_is_noop_when_already_set() -> None:
    """Test that acknowledge_cost_threshold is a no-op when already acknowledged."""
    config_file = find_user_config_in_isolated_mode()
    backup_file = find_user_config_in_isolated_mode().with_suffix(".json.bak")
    config = {"hasAcknowledgedCostThreshold": True, "projects": {}}
    config_file.write_text(json.dumps(config, indent=2))
    content_before = config_file.read_text()

    acknowledge_cost_threshold(config_file)

    # The config file content must be byte-for-byte unchanged across the no-op call.
    assert config_file.read_text() == content_before
    # Secondary check: a no-op write would have created a .bak backup; it must not exist.
    assert not backup_file.exists()


def test_acknowledge_cost_threshold_creates_config_when_none_exists() -> None:
    """Test that acknowledge_cost_threshold creates config file if it doesn't exist."""
    config_file = find_user_config_in_isolated_mode()
    assert not config_file.exists()

    acknowledge_cost_threshold(config_file)

    assert config_file.exists()
    config = json.loads(config_file.read_text())
    assert config["hasAcknowledgedCostThreshold"] is True


def test_acknowledge_cost_threshold_handles_empty_config() -> None:
    """Test that acknowledge_cost_threshold handles empty config file."""
    config_file = find_user_config_in_isolated_mode()
    config_file.write_text("")

    acknowledge_cost_threshold(config_file)

    config = json.loads(config_file.read_text())
    assert config["hasAcknowledgedCostThreshold"] is True


# Tests for check_claude_dialogs_dismissed / auto_dismiss_claude_dialogs


def test_check_claude_dialogs_dismissed_checks_trust(tmp_path: Path) -> None:
    """Test that check_claude_dialogs_dismissed checks trust for source_path."""
    config_file = find_user_config_in_isolated_mode()
    source_path = tmp_path / "source"
    source_path.mkdir()

    # Config has effort dismissed but source is NOT trusted
    config = {"effortCalloutDismissed": True, "projects": {}}
    config_file.write_text(json.dumps(config, indent=2))

    with pytest.raises(ClaudeDirectoryNotTrustedError):
        check_claude_dialogs_dismissed(config_file, source_path)


def test_check_claude_dialogs_dismissed_checks_effort_callout(tmp_path: Path) -> None:
    """Test that check_claude_dialogs_dismissed checks effort callout."""
    config_file = find_user_config_in_isolated_mode()
    source_path = tmp_path / "source"
    source_path.mkdir()

    # Trust and onboarding are both set, so the effort callout is the ONLY
    # remaining undismissed dialog. This isolates the effort check: the raised
    # error type is genuinely attributable to the effort callout rather than
    # being correct only by the trust -> effort -> onboarding check ordering.
    config = {
        "hasCompletedOnboarding": True,
        "projects": {
            str(source_path): {"hasTrustDialogAccepted": True},
        },
    }
    config_file.write_text(json.dumps(config, indent=2))

    with pytest.raises(ClaudeEffortCalloutNotDismissedError):
        check_claude_dialogs_dismissed(config_file, source_path)


def test_check_claude_dialogs_dismissed_passes_when_all_set(tmp_path: Path) -> None:
    """Test that check_claude_dialogs_dismissed passes when all dialogs are set."""
    config_file = find_user_config_in_isolated_mode()
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


def test_auto_dismiss_claude_dialogs_sets_all(tmp_path: Path) -> None:
    """Test that auto_dismiss_claude_dialogs sets all dialog fields."""
    config_file = find_user_config_in_isolated_mode()
    source_path = tmp_path / "source"
    source_path.mkdir()

    config = {"projects": {}}
    config_file.write_text(json.dumps(config, indent=2))

    auto_dismiss_claude_dialogs(config_file, source_path)

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
    global_config = find_user_config_in_isolated_mode()
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
    """With ORIGINAL_CLAUDE_CONFIG_DIR set and existing on disk, returns that path even if CLAUDE_CONFIG_DIR differs."""
    user_dir = tmp_path / "user-claude"
    user_dir.mkdir()
    agent_dir = tmp_path / "agent-claude"
    agent_dir.mkdir()
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


def test_get_user_claude_config_dir_falls_back_when_original_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ORIGINAL_CLAUDE_CONFIG_DIR points at a non-existent directory, fall back to CLAUDE_CONFIG_DIR.

    This is the nested-sandbox case: ORIGINAL_CLAUDE_CONFIG_DIR was inherited
    from the host (e.g. /Users/<user>/.claude on macOS) but we are now running
    inside a Linux VM where that path does not exist. The per-agent
    CLAUDE_CONFIG_DIR is where the live config (and credentials) actually
    live, so we treat ORIGINAL as if it were unset.
    """
    bogus_user_dir = tmp_path / "does-not-exist-on-disk"
    agent_dir = tmp_path / "agent-claude"
    agent_dir.mkdir()
    monkeypatch.setenv("ORIGINAL_CLAUDE_CONFIG_DIR", str(bogus_user_dir))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(agent_dir))

    result = get_user_claude_config_dir()

    assert result == agent_dir


def test_get_user_claude_config_dir_falls_back_when_original_is_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ORIGINAL_CLAUDE_CONFIG_DIR points at a non-directory, fall back to CLAUDE_CONFIG_DIR."""
    not_a_dir = tmp_path / "regular-file"
    not_a_dir.write_text("not a directory")
    agent_dir = tmp_path / "agent-claude"
    agent_dir.mkdir()
    monkeypatch.setenv("ORIGINAL_CLAUDE_CONFIG_DIR", str(not_a_dir))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(agent_dir))

    result = get_user_claude_config_dir()

    assert result == agent_dir


def test_get_user_claude_config_dir_credentials_fallback_resolves_to_per_agent_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: nested-sandbox scenario resolves .credentials.json from the per-agent dir.

    Reproduces the bug fixed by this change: ORIGINAL_CLAUDE_CONFIG_DIR points
    at a host path that doesn't exist inside the VM, the per-agent
    CLAUDE_CONFIG_DIR holds the live .credentials.json, and callers that
    resolve credentials via get_user_claude_config_dir() / ".credentials.json"
    must end up pointing at the per-agent file.
    """
    bogus_host_dir = tmp_path / "Users" / "someone" / ".claude"
    agent_dir = tmp_path / "agent-claude"
    agent_dir.mkdir()
    (agent_dir / ".credentials.json").write_text('{"token": "abc"}')
    monkeypatch.setenv("ORIGINAL_CLAUDE_CONFIG_DIR", str(bogus_host_dir))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(agent_dir))

    resolved = get_user_claude_config_dir() / ".credentials.json"

    assert resolved.exists()
    assert resolved.read_text() == '{"token": "abc"}'


# Tests for resolve_shared_claude_config_dir


def test_resolve_shared_claude_config_dir_returns_env_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With CLAUDE_CONFIG_DIR set to a non-empty path, returns it as a Path."""
    target = tmp_path / "shared-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(target))
    assert resolve_shared_claude_config_dir() == target


def test_resolve_shared_claude_config_dir_falls_back_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without CLAUDE_CONFIG_DIR, falls back to ``~/.claude/`` (claude's own default)."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert resolve_shared_claude_config_dir() == Path.home() / ".claude"


def test_resolve_shared_claude_config_dir_falls_back_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty CLAUDE_CONFIG_DIR is treated the same as unset and falls back to ``~/.claude/``."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "")
    assert resolve_shared_claude_config_dir() == Path.home() / ".claude"


# Tests for find_user_config_in_unisolated_mode


def test_find_user_config_in_unisolated_mode_uses_env_dir_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With CLAUDE_CONFIG_DIR set, the shared config file lives inside that dir."""
    target = tmp_path / "shared-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(target))
    assert find_user_config_in_unisolated_mode() == target / ".claude.json"


def test_find_user_config_in_unisolated_mode_falls_back_to_home_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without CLAUDE_CONFIG_DIR, the shared config file is claude's default ~/.claude.json."""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert find_user_config_in_unisolated_mode() == Path.home() / ".claude.json"


# Tests for find_user_config_in_isolated_mode


def test_find_user_config_in_isolated_mode_defaults_to_home() -> None:
    """Without env vars and no file on disk, returns ~/.claude.json."""
    result = find_user_config_in_isolated_mode()
    assert result == Path.home() / ".claude.json"


def test_find_user_config_in_isolated_mode_returns_default_path() -> None:
    """Without env vars, returns ~/.claude.json when it exists."""
    config = Path.home() / ".claude.json"
    config.write_text(json.dumps({}, indent=2))

    result = find_user_config_in_isolated_mode()
    assert result == config


def test_find_user_config_in_isolated_mode_defaults_to_inside_dir_with_original_dir_but_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ORIGINAL_CLAUDE_CONFIG_DIR set but no config files, returns the inside-dir path."""
    user_dir = tmp_path / "user-claude"
    user_dir.mkdir()
    monkeypatch.setenv("ORIGINAL_CLAUDE_CONFIG_DIR", str(user_dir))

    result = find_user_config_in_isolated_mode()
    assert result == user_dir / ".claude.json"


def test_find_user_config_in_isolated_mode_finds_inside_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With ORIGINAL_CLAUDE_CONFIG_DIR set, finds .claude.json inside it."""
    user_dir = tmp_path / "user-claude"
    user_dir.mkdir()
    monkeypatch.setenv("ORIGINAL_CLAUDE_CONFIG_DIR", str(user_dir))

    inside = user_dir / ".claude.json"
    inside.write_text(json.dumps({}, indent=2))

    result = find_user_config_in_isolated_mode()
    assert result == inside


def test_find_user_config_in_isolated_mode_finds_beside_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finds ~/.claude.json (beside ~/.claude/) when only beside-dir config exists.

    The default Claude Code layout stores .claude.json beside the config dir
    (~/.claude.json), not inside it (~/.claude/.claude.json). When an agent's
    ORIGINAL_CLAUDE_CONFIG_DIR points to ~/.claude, the function should find
    the config file at the beside-dir location.
    """
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ORIGINAL_CLAUDE_CONFIG_DIR", str(claude_dir))

    beside_config = Path.home() / ".claude.json"
    beside_config.write_text(json.dumps({"projects": {}}, indent=2))

    result = find_user_config_in_isolated_mode()
    assert result == beside_config


def test_find_user_config_in_isolated_mode_prefers_inside_dir_when_both_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both inside-dir and beside-dir configs exist, prefers inside-dir."""
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ORIGINAL_CLAUDE_CONFIG_DIR", str(claude_dir))

    inside_config = claude_dir / ".claude.json"
    inside_config.write_text(json.dumps({"inside": True}, indent=2))
    beside_config = Path.home() / ".claude.json"
    beside_config.write_text(json.dumps({"beside": True}, indent=2))

    result = find_user_config_in_isolated_mode()
    assert result == inside_config


def test_find_user_config_in_isolated_mode_ignores_claude_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ORIGINAL_CLAUDE_CONFIG_DIR, ignores CLAUDE_CONFIG_DIR (per-agent dir)."""
    custom_dir = tmp_path / "custom-claude"
    custom_dir.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(custom_dir))

    # File exists in per-agent dir but not at ~/.claude.json
    config = custom_dir / ".claude.json"
    config.write_text(json.dumps({}, indent=2))

    # Should return the default user path, not the per-agent path
    result = find_user_config_in_isolated_mode()
    assert result == Path.home() / ".claude.json"


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Slashes and leading slash become '-' (the original behavior, preserved).
        ("/Users/foo/bar", "-Users-foo-bar"),
        # Dots become '-' (the original behavior, preserved).
        ("/a.b.c", "-a-b-c"),
        # Underscores become '-' (newly added by this branch).
        ("/foo_bar/baz_qux", "-foo-bar-baz-qux"),
        # Spaces and tabs become '-' (newly added).
        ("/with space/and\ttab", "-with-space-and-tab"),
        # '@' and '+' become '-' (newly added).
        ("/user@host/foo+bar", "-user-host-foo-bar"),
        # Non-ASCII letters become '-' (newly added). Each non-ASCII char is one
        # codepoint in str(path), so each maps to exactly one '-'. The leading
        # slash and the slash between segments each contribute one '-' as well.
        ("/café/naïve", "-caf--na-ve"),
        ("/中文/path", "----path"),
        # Hyphens and ASCII alphanumerics are preserved.
        ("/already-dashed/Mixed123", "-already-dashed-Mixed123"),
        # Consecutive special chars are NOT collapsed (we mirror Claude Code's
        # 1:1 mapping; collapsing would create dir-name collisions).
        ("/a..b", "-a--b"),
        ("/a__b", "-a--b"),
    ],
)
def test_encode_claude_project_dir_name(raw: str, expected: str) -> None:
    """encode_claude_project_dir_name maps every non-[A-Za-z0-9-] char to '-'.

    Pins the behavior introduced when the encoder was broadened to match
    Claude Code's actual algorithm (anthropics/claude-code#19972). If this
    encoder ever regresses to ``replace("/", "-").replace(".", "-")`` or to a
    pattern that treats ``_`` as a word char (e.g. ``\\W``), several of these
    cases will fail -- which is the point: a divergence here silently breaks
    ``mngr create --adopt``.
    """
    assert encode_claude_project_dir_name(Path(raw)) == expected


def _chat_meta_q(keybindings: dict[str, Any]) -> str | None:
    """Return the action bound to meta+q in the Chat context of a keybindings dict, or None."""
    for entry in keybindings.get("bindings", []):
        if entry.get("context") == "Chat":
            return entry.get("bindings", {}).get("meta+q")
    return None


def test_ensure_tap_keybinding_merges_into_existing_chat_entry(tmp_path: Path) -> None:
    """Adds meta+q -> chat:cancel to an existing Chat entry without disturbing its other bindings."""
    path = tmp_path / "keybindings.json"
    path.write_text(
        json.dumps(
            {
                "$schema": "https://example/schema.json",
                "bindings": [
                    {"context": "Global", "bindings": {"ctrl+t": "app:toggleTodos"}},
                    {"context": "Chat", "bindings": {"escape": "chat:cancel", "enter": "chat:submit"}},
                ],
            }
        )
    )
    ensure_chat_cancel_tap_keybinding(path)
    result = json.loads(path.read_text())
    assert _chat_meta_q(result) == "chat:cancel"
    # Existing metadata and bindings survive untouched.
    assert result["$schema"] == "https://example/schema.json"
    chat = next(e for e in result["bindings"] if e["context"] == "Chat")
    assert chat["bindings"]["escape"] == "chat:cancel"
    assert chat["bindings"]["enter"] == "chat:submit"


def test_ensure_tap_keybinding_creates_file_and_entry_when_absent(tmp_path: Path) -> None:
    """Creates the file, bindings list, and Chat entry when none exist."""
    path = tmp_path / "keybindings.json"
    assert not path.exists()
    ensure_chat_cancel_tap_keybinding(path)
    result = json.loads(path.read_text())
    assert _chat_meta_q(result) == "chat:cancel"


def test_ensure_tap_keybinding_creates_chat_entry_when_only_other_contexts_present(tmp_path: Path) -> None:
    """Appends a fresh Chat entry when the file has contexts but no Chat one."""
    path = tmp_path / "keybindings.json"
    path.write_text(json.dumps({"bindings": [{"context": "Global", "bindings": {"ctrl+t": "app:toggleTodos"}}]}))
    ensure_chat_cancel_tap_keybinding(path)
    result = json.loads(path.read_text())
    assert _chat_meta_q(result) == "chat:cancel"
    assert any(e["context"] == "Global" for e in result["bindings"])


def test_ensure_tap_keybinding_never_clobbers_existing_chat_meta_q(tmp_path: Path) -> None:
    """A meta+q already bound in Chat (to anything) is left untouched."""
    path = tmp_path / "keybindings.json"
    path.write_text(json.dumps({"bindings": [{"context": "Chat", "bindings": {"meta+q": "chat:modelPicker"}}]}))
    ensure_chat_cancel_tap_keybinding(path)
    assert _chat_meta_q(json.loads(path.read_text())) == "chat:modelPicker"


def test_ensure_tap_keybinding_never_clobbers_global_meta_q(tmp_path: Path) -> None:
    """A meta+q bound in Global blocks the Chat binding (the chord would be shadowed)."""
    path = tmp_path / "keybindings.json"
    path.write_text(
        json.dumps(
            {
                "bindings": [
                    {"context": "Global", "bindings": {"meta+q": "app:quit"}},
                    {"context": "Chat", "bindings": {"escape": "chat:cancel"}},
                ]
            }
        )
    )
    ensure_chat_cancel_tap_keybinding(path)
    # Neither the Global binding nor the Chat entry gains a meta+q.
    result = json.loads(path.read_text())
    assert _chat_meta_q(result) is None
    global_entry = next(e for e in result["bindings"] if e["context"] == "Global")
    assert global_entry["bindings"]["meta+q"] == "app:quit"


def test_ensure_tap_keybinding_unrelated_context_meta_q_does_not_block(tmp_path: Path) -> None:
    """A meta+q bound only in an unrelated context (not Chat/Global) does not block the merge."""
    path = tmp_path / "keybindings.json"
    path.write_text(json.dumps({"bindings": [{"context": "Settings", "bindings": {"meta+q": "settings:something"}}]}))
    ensure_chat_cancel_tap_keybinding(path)
    assert _chat_meta_q(json.loads(path.read_text())) == "chat:cancel"


def test_ensure_tap_keybinding_is_idempotent(tmp_path: Path) -> None:
    """Running twice leaves exactly one meta+q -> chat:cancel and identical content."""
    path = tmp_path / "keybindings.json"
    ensure_chat_cancel_tap_keybinding(path)
    first = path.read_text()
    ensure_chat_cancel_tap_keybinding(path)
    assert path.read_text() == first
    assert _chat_meta_q(json.loads(first)) == "chat:cancel"


def test_ensure_tap_keybinding_leaves_corrupt_file_untouched(tmp_path: Path) -> None:
    """A file that is not valid JSON is left exactly as-is rather than overwritten."""
    path = tmp_path / "keybindings.json"
    path.write_text("{ this is not valid json")
    ensure_chat_cancel_tap_keybinding(path)
    assert path.read_text() == "{ this is not valid json"


def test_ensure_tap_keybinding_replaces_non_dict_chat_bindings(tmp_path: Path) -> None:
    """A Chat entry whose "bindings" is not an object is replaced with a fresh dict, not crashed on."""
    path = tmp_path / "keybindings.json"
    path.write_text(json.dumps({"bindings": [{"context": "Chat", "bindings": "not-a-dict"}]}))
    ensure_chat_cancel_tap_keybinding(path)
    result = json.loads(path.read_text())
    assert _chat_meta_q(result) == "chat:cancel"


def _write_binding_and_marker(
    tmp_path: Path, chat_meta_q: str | None, keybindings_before_marker: bool
) -> tuple[Path, Path]:
    """Write a keybindings.json (Chat meta+q optionally set) and a process marker with ordered mtimes."""
    keybindings_path = tmp_path / "keybindings.json"
    chat_bindings: dict[str, str] = {"escape": "chat:cancel"}
    if chat_meta_q is not None:
        chat_bindings["meta+q"] = chat_meta_q
    keybindings_path.write_text(json.dumps({"bindings": [{"context": "Chat", "bindings": chat_bindings}]}))
    marker_path = tmp_path / "claude_process_started"
    marker_path.write_text("")
    # Order the two files' mtimes explicitly so the test does not depend on write timing.
    if keybindings_before_marker:
        os.utime(keybindings_path, (1000, 1000))
        os.utime(marker_path, (2000, 2000))
    else:
        os.utime(keybindings_path, (2000, 2000))
        os.utime(marker_path, (1000, 1000))
    return keybindings_path, marker_path


def test_is_tap_binding_active_true_when_bound_and_predates_marker(tmp_path: Path) -> None:
    """Active when Chat binds meta+q -> chat:cancel and the file predates the process marker."""
    keybindings_path, marker_path = _write_binding_and_marker(tmp_path, "chat:cancel", keybindings_before_marker=True)
    assert is_tap_binding_active(keybindings_path, marker_path) is True


def test_is_tap_binding_active_false_when_edited_after_launch(tmp_path: Path) -> None:
    """Inactive (conservative) when the binding is present but written after the process started."""
    keybindings_path, marker_path = _write_binding_and_marker(tmp_path, "chat:cancel", keybindings_before_marker=False)
    assert is_tap_binding_active(keybindings_path, marker_path) is False


def test_is_tap_binding_active_false_when_meta_q_bound_elsewhere(tmp_path: Path) -> None:
    """Inactive when Chat binds meta+q to something other than chat:cancel."""
    keybindings_path, marker_path = _write_binding_and_marker(
        tmp_path, "chat:modelPicker", keybindings_before_marker=True
    )
    assert is_tap_binding_active(keybindings_path, marker_path) is False


def test_is_tap_binding_active_false_when_chord_absent(tmp_path: Path) -> None:
    """Inactive when the Chat context has no meta+q binding at all."""
    keybindings_path, marker_path = _write_binding_and_marker(tmp_path, None, keybindings_before_marker=True)
    assert is_tap_binding_active(keybindings_path, marker_path) is False


def test_is_tap_binding_active_false_when_marker_missing(tmp_path: Path) -> None:
    """Inactive when the process-started marker is absent (no live process to tap)."""
    keybindings_path, _ = _write_binding_and_marker(tmp_path, "chat:cancel", keybindings_before_marker=True)
    assert is_tap_binding_active(keybindings_path, tmp_path / "does_not_exist") is False


def test_is_tap_binding_active_false_when_keybindings_missing(tmp_path: Path) -> None:
    """Inactive when keybindings.json does not exist."""
    marker_path = tmp_path / "claude_process_started"
    marker_path.write_text("")
    assert is_tap_binding_active(tmp_path / "missing.json", marker_path) is False


def test_is_tap_binding_active_false_when_keybindings_malformed(tmp_path: Path) -> None:
    """A corrupt keybindings.json reads as not-active, never raises. ``ensure_chat_cancel_tap_keybinding``
    tolerates (and does not repair) a malformed file, so the gate must not raise on it -- the stop
    button then falls back to the base restart-drain instead of 500ing."""
    keybindings_path = tmp_path / "keybindings.json"
    keybindings_path.write_text("{ this is not valid json")
    marker_path = tmp_path / "claude_process_started"
    marker_path.write_text("")
    assert is_tap_binding_active(keybindings_path, marker_path) is False


def test_build_permission_auto_allow_hooks_config_has_permission_request_hook() -> None:
    """build_permission_auto_allow_hooks_config should produce a PermissionRequest hook with wildcard matcher."""
    config = build_permission_auto_allow_hooks_config()
    assert "hooks" in config
    assert "PermissionRequest" in config["hooks"]
    permission_hooks = config["hooks"]["PermissionRequest"]
    assert len(permission_hooks) == 1
    hook_entry = permission_hooks[0]
    assert hook_entry["matcher"] == "*"
    assert len(hook_entry["hooks"]) == 1
    inner_hook = hook_entry["hooks"][0]
    assert inner_hook["type"] == "command"
    assert inner_hook["timeout"] == 5
    assert "allow" in inner_hook["command"]
    assert "PermissionRequest" in inner_hook["command"]


def test_mark_claude_agent_idle_clears_markers_and_emits_activity_event(tmp_path: Path) -> None:
    """mark_claude_agent_idle removes both markers and appends one format-conformant activity event.

    It runs the same shell snippet the Notification / SessionStart / Stop hooks run, so the
    emitted line must match the hooks' activity-event format exactly."""
    state_dir = tmp_path / "agent"
    state_dir.mkdir()
    (state_dir / "active").write_text("")
    (state_dir / "permissions_waiting").write_text("")
    host_dir = tmp_path / "host"
    host_dir.mkdir()

    mark_claude_agent_idle(state_dir, host_dir)

    # Both markers are cleared, so the agent stops reporting RUNNING.
    assert not (state_dir / "active").exists()
    assert not (state_dir / "permissions_waiting").exists()
    # Exactly one activity event was appended, in the hooks' own format.
    events_file = host_dir / "events" / "mngr" / "activity" / "events.jsonl"
    lines = events_file.read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["source"] == "mngr/activity"
    assert event["type"] == "activity"
    assert event["event_id"].startswith("evt-")
    assert event["timestamp"].endswith("Z")


def test_mark_claude_agent_idle_is_idempotent_on_absent_markers(tmp_path: Path) -> None:
    """Absent markers are a no-op (``rm -f``); the activity event is still emitted."""
    state_dir = tmp_path / "agent"
    state_dir.mkdir()
    host_dir = tmp_path / "host"
    host_dir.mkdir()

    mark_claude_agent_idle(state_dir, host_dir)

    assert not (state_dir / "active").exists()
    events_file = host_dir / "events" / "mngr" / "activity" / "events.jsonl"
    assert len(events_file.read_text().splitlines()) == 1
