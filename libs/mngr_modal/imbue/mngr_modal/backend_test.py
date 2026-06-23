from pathlib import Path

import pytest
from pydantic import AnyUrl

from imbue.mngr.config.data_types import MngrContext
from imbue.mngr_modal.backend import ModalProviderBackend
from imbue.mngr_modal.backend import get_files_for_deploy
from imbue.mngr_modal.config import MissingModalConnectorUrlError
from imbue.mngr_modal.config import ModalMode
from imbue.mngr_modal.config import ModalProviderConfig
from imbue.modal_proxy.direct import DirectModalInterface
from imbue.modal_proxy.remote import RemoteModalInterface

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
# ModalMode resolution (DIRECT vs PROXIED) -- no Modal/network calls
# =============================================================================


def test_resolve_modal_interface_direct_uses_sdk(temp_mngr_ctx: MngrContext) -> None:
    """DIRECT mode resolves to the SDK-backed interface."""
    iface = ModalProviderBackend._resolve_modal_interface(ModalProviderConfig(), temp_mngr_ctx)
    assert isinstance(iface, DirectModalInterface)


def test_resolve_modal_interface_proxied_uses_connector(temp_mngr_ctx: MngrContext) -> None:
    """PROXIED mode resolves to the connector-backed RemoteModalInterface (no token needed)."""
    config = ModalProviderConfig(
        mode=ModalMode.PROXIED,
        connector_url=AnyUrl("https://rsc.example.modal.run"),
        environment="main",
    )
    iface = ModalProviderBackend._resolve_modal_interface(config, temp_mngr_ctx)
    assert isinstance(iface, RemoteModalInterface)
    assert iface.environment == "main"


def test_get_connector_url_precedence_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """connector_url field wins; otherwise env; otherwise a clear error."""
    monkeypatch.delenv("MNGR__PROVIDERS__MODAL__CONNECTOR_URL", raising=False)
    monkeypatch.delenv("MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL", raising=False)
    field_config = ModalProviderConfig(connector_url=AnyUrl("https://field.example.modal.run"))
    assert field_config.get_connector_url() == "https://field.example.modal.run"

    monkeypatch.setenv("MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL", "https://env.example.modal.run/")
    assert ModalProviderConfig().get_connector_url() == "https://env.example.modal.run"

    monkeypatch.delenv("MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL", raising=False)
    with pytest.raises(MissingModalConnectorUrlError):
        ModalProviderConfig().get_connector_url()
