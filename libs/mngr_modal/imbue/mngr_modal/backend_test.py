from pathlib import Path

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_modal.backend import ModalProviderBackend
from imbue.mngr_modal.backend import get_files_for_deploy
from imbue.modal_proxy.errors import ModalProxyError
from imbue.modal_proxy.interface import AppInterface
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
# _get_or_create_app Error Propagation Tests
# =============================================================================
#
# build_provider_instance wraps _get_or_create_app in a try/except block:
#
#   except ModalProxyError as e:
#       raise ProviderUnavailableError(name, str(e)) from e
#
# The test below verifies the precondition for this conversion: that a
# ModalProxyError raised inside _get_or_create_app propagates out unchanged.
# Because build_provider_instance constructs its own ModalInterface internally
# with no injection point, the conversion clause itself cannot be exercised
# at the unit-test level without structural changes to the production code.


class _FailingModalInterface(TestingModalInterface):
    """Concrete ModalInterface that raises ModalProxyError on app_lookup."""

    def app_lookup(
        self,
        name: str,
        *,
        create_if_missing: bool = True,
        environment_name: str,
    ) -> AppInterface:
        raise ModalProxyError("Could not connect to the Modal server.")


def test_modal_proxy_error_propagates_out_of_get_or_create_app(tmp_path: Path) -> None:
    """ModalProxyError raised by app_lookup escapes _get_or_create_app unchanged.

    This verifies the precondition for build_provider_instance's error conversion:
    _get_or_create_app does not swallow ModalProxyError, so the caller's
    except-ModalProxyError clause can convert it to ProviderUnavailableError.
    """
    failing_interface = _FailingModalInterface(
        root_dir=tmp_path,
        concurrency_group=ConcurrencyGroup(name="test"),
    )
    with pytest.raises(ModalProxyError, match="Could not connect"):
        ModalProviderBackend._get_or_create_app(
            app_name="fail-test",
            environment_name="test-env",
            is_persistent=True,
            modal_interface=failing_interface,
            is_testing=True,
        )
