from pathlib import Path

import pytest

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr_modal.backend import _validate_test_environment_name
from imbue.mngr_modal.backend import get_files_for_deploy

# =============================================================================
# get_files_for_deploy Tests
# =============================================================================


def test_get_files_for_deploy_returns_empty_when_user_settings_excluded(
    temp_mngr_ctx: MngrContext, tmp_path: Path
) -> None:
    """get_files_for_deploy returns empty dict when include_user_settings is False."""
    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=False, include_project_settings=True, repo_root=tmp_path
    )

    assert result == {}


def test_get_files_for_deploy_returns_empty_when_no_modal_dir(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """get_files_for_deploy returns empty dict when no modal provider directory exists."""
    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=True, repo_root=tmp_path
    )

    assert result == {}


def test_get_files_for_deploy_excludes_ssh_key_files(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """get_files_for_deploy excludes SSH key files from the modal provider directory."""
    modal_dir = temp_mngr_ctx.profile_dir / "providers" / "modal"
    modal_dir.mkdir(parents=True)
    (modal_dir / "modal_ssh_key").write_text("private-key-data")
    (modal_dir / "modal_ssh_key.pub").write_text("public-key-data")
    (modal_dir / "known_hosts").write_text("[localhost]:2222 ssh-ed25519 AAAA...")

    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=True, repo_root=tmp_path
    )

    assert result == {}


def test_get_files_for_deploy_includes_non_key_files(temp_mngr_ctx: MngrContext, tmp_path: Path) -> None:
    """get_files_for_deploy includes non-key files from the modal provider directory."""
    modal_dir = temp_mngr_ctx.profile_dir / "providers" / "modal"
    modal_dir.mkdir(parents=True)
    config_file = modal_dir / "config.json"
    config_file.write_text('{"modal": "config"}')

    result = get_files_for_deploy(
        mngr_ctx=temp_mngr_ctx, include_user_settings=True, include_project_settings=True, repo_root=tmp_path
    )

    assert len(result) == 1
    matched_values = list(result.values())
    assert matched_values[0] == config_file


# =============================================================================
# _validate_test_environment_name Tests
# =============================================================================


def test_validate_test_env_name_accepts_timestamped_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid timestamped names pass validation during test phase."""
    monkeypatch.setenv("_PYTEST_GUARD_PHASE", "call")
    _validate_test_environment_name("mngr_test-2026-03-27-02-02-17-ae01ccb71e")


def test_validate_test_env_name_rejects_production_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production-style names (mngr-{user_id}) are rejected during test phase."""
    monkeypatch.setenv("_PYTEST_GUARD_PHASE", "call")
    with pytest.raises(MngrError, match="does not match the required test pattern"):
        _validate_test_environment_name("mngr-dde9fc2844ec435f9f0a4acb93471f42")


def test_validate_test_env_name_rejects_uuid_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """UUID-based test prefixes (mngr_{uuid}-) are rejected during test phase."""
    monkeypatch.setenv("_PYTEST_GUARD_PHASE", "call")
    with pytest.raises(MngrError, match="does not match the required test pattern"):
        _validate_test_environment_name("mngr_6419f5b122464f3f8963c7302a75dfab-user123")


def test_validate_test_env_name_rejects_mngr_test_without_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """mngr_test- prefix without timestamp is rejected during test phase."""
    monkeypatch.setenv("_PYTEST_GUARD_PHASE", "call")
    with pytest.raises(MngrError, match="does not match the required test pattern"):
        _validate_test_environment_name("mngr_test-abc123")


def test_validate_test_env_name_noop_outside_test_phase(monkeypatch: pytest.MonkeyPatch) -> None:
    """Validation is a no-op when _PYTEST_GUARD_PHASE is not 'call'."""
    monkeypatch.delenv("_PYTEST_GUARD_PHASE", raising=False)
    _validate_test_environment_name("mngr-anything-goes")
