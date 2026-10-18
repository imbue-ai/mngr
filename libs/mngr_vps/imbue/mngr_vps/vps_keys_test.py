from pathlib import Path

import pytest

from imbue.mngr.primitives import HostId
from imbue.mngr.providers.ssh_utils import per_host_key_dir
from imbue.mngr.providers.ssh_utils import save_ssh_keypair
from imbue.mngr_vps.errors import VpsHostKeyMissingError
from imbue.mngr_vps.primitives import VPS_SSH_KEY_NAME
from imbue.mngr_vps.vps_keys import mint_vps_root_keypair
from imbue.mngr_vps.vps_keys import resolve_vps_root_keypair


def test_mint_writes_the_key_under_the_per_host_dir_without_a_known_hosts_link(tmp_path: Path) -> None:
    host_id = HostId.generate()

    private_key_path, public_key = mint_vps_root_keypair(tmp_path, host_id)

    assert private_key_path == per_host_key_dir(tmp_path, host_id) / VPS_SSH_KEY_NAME
    assert private_key_path.exists()
    assert public_key.startswith("ssh-ed25519 ")
    # The sibling ``known_hosts`` link is owned by the agent endpoint's key, so
    # minting the VPS-root key must not claim it (first-writer-wins would
    # otherwise point the container key's sibling at the port-22 pins).
    assert not (per_host_key_dir(tmp_path, host_id) / "known_hosts").exists()
    # Nothing provider-wide is created.
    assert not (tmp_path / VPS_SSH_KEY_NAME).exists()


def test_mint_is_idempotent(tmp_path: Path) -> None:
    host_id = HostId.generate()
    first_path, first_public = mint_vps_root_keypair(tmp_path, host_id)
    second_path, second_public = mint_vps_root_keypair(tmp_path, host_id)
    assert (first_path, first_public) == (second_path, second_public)


def test_resolve_prefers_the_per_host_key_over_a_legacy_one(tmp_path: Path) -> None:
    host_id = HostId.generate()
    save_ssh_keypair(tmp_path, VPS_SSH_KEY_NAME)
    minted_path, minted_public = mint_vps_root_keypair(tmp_path, host_id)

    resolved = resolve_vps_root_keypair(tmp_path, host_id)

    assert resolved.private_key_path == minted_path
    assert resolved.public_key == minted_public
    assert resolved.is_legacy_provider_key is False


def test_resolve_falls_back_to_the_legacy_key_read_only(tmp_path: Path) -> None:
    """A host created before per-host keys is opened with the provider-wide key, and nothing is minted."""
    legacy_private_path, legacy_public_path = save_ssh_keypair(tmp_path, VPS_SSH_KEY_NAME)
    host_id = HostId.generate()

    resolved = resolve_vps_root_keypair(tmp_path, host_id)

    assert resolved.private_key_path == legacy_private_path
    assert resolved.public_key == legacy_public_path.read_text().strip()
    assert resolved.is_legacy_provider_key is True
    assert not per_host_key_dir(tmp_path, host_id).exists()


def test_resolve_raises_and_mints_nothing_when_no_key_exists(tmp_path: Path) -> None:
    host_id = HostId.generate()

    with pytest.raises(VpsHostKeyMissingError, match=str(host_id)) as exc_info:
        resolve_vps_root_keypair(tmp_path, host_id)

    assert exc_info.value.user_help_text is not None
    assert "created it" in exc_info.value.user_help_text
    assert not per_host_key_dir(tmp_path, host_id).exists()
    assert not (tmp_path / VPS_SSH_KEY_NAME).exists()
