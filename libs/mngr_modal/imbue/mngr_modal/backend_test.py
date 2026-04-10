from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr_modal.backend import _create_environment
from imbue.mngr_modal.backend import get_files_for_deploy
from imbue.modal_proxy.testing import TestingModalInterface

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
# _create_environment name validation Tests
# =============================================================================


@pytest.fixture
def modal_interface(tmp_path: Path, cg: ConcurrencyGroup) -> TestingModalInterface:
    root = tmp_path / "modal_testing"
    root.mkdir()
    return TestingModalInterface(root_dir=root, concurrency_group=cg)


def test_create_environment_accepts_timestamped_name(modal_interface: TestingModalInterface) -> None:
    """Valid timestamped test environment names are accepted."""
    _create_environment("mngr_test-2026-03-27-02-02-17-ae01ccb71e", modal_interface)


def test_create_environment_rejects_uuid_prefix(modal_interface: TestingModalInterface) -> None:
    """UUID-based test prefixes (mngr_{uuid}-) are rejected."""
    with pytest.raises(MngrError, match="test environments must match"):
        _create_environment("mngr_6419f5b122464f3f8963c7302a75dfab-user123", modal_interface)


def test_create_environment_rejects_mngr_test_without_timestamp(modal_interface: TestingModalInterface) -> None:
    """mngr_test- prefix without timestamp format is rejected."""
    with pytest.raises(MngrError, match="test environments must match"):
        _create_environment("mngr_test-abc123", modal_interface)


def test_create_environment_allows_production_prefix(modal_interface: TestingModalInterface) -> None:
    """Production-style names (mngr-{user_id}) are allowed -- the guard only applies to mngr_ prefix."""
    _create_environment("mngr-dde9fc2844ec435f9f0a4acb93471f42", modal_interface)
