from pathlib import Path

import pytest
from pydantic import ValidationError

from imbue.mngr.primitives import HostId
from imbue.mngr.providers.host_access_manifest import HostAccessManifest
from imbue.mngr.providers.host_access_manifest import PerHostDirScheme
from imbue.mngr.providers.host_access_manifest import host_access_key_dir
from imbue.mngr.providers.host_access_manifest import host_access_per_host_dir
from imbue.mngr.providers.host_access_manifest import host_access_pin_store_paths
from imbue.mngr.providers.host_access_manifest import keypair_file_names
from imbue.mngr.providers.ssh_utils import per_host_key_dir

_PROVIDER_WIDE = HostAccessManifest(
    key_dir_name="keys",
    per_host_dir_scheme=PerHostDirScheme.HOST_KEYS_UUID_HEX,
    file_names=keypair_file_names("client_key", "host_key"),
    agent_key_file_name="client_key",
    pin_store_names=("known_hosts",),
    is_pin_store_per_host=False,
    known_hosts_link_store_name="known_hosts",
)

_PER_HOST = HostAccessManifest(
    key_dir_name="",
    per_host_dir_scheme=PerHostDirScheme.HOSTS_HOST_ID,
    file_names=keypair_file_names("ssh_key"),
    agent_key_file_name="ssh_key",
    pin_store_names=("known_hosts",),
    is_pin_store_per_host=True,
    known_hosts_link_store_name=None,
)


def test_keypair_file_names_pairs_each_private_key_with_its_public_half() -> None:
    assert keypair_file_names("a", "b") == ("a", "a.pub", "b", "b.pub")


def test_provider_wide_layout_resolves_under_the_key_dir(tmp_path: Path) -> None:
    host_id = HostId.generate()
    key_dir = tmp_path / "keys"

    assert host_access_key_dir(tmp_path, _PROVIDER_WIDE) == key_dir
    assert host_access_per_host_dir(tmp_path, _PROVIDER_WIDE, host_id) == per_host_key_dir(key_dir, host_id)
    assert host_access_pin_store_paths(tmp_path, _PROVIDER_WIDE, host_id) == {"known_hosts": key_dir / "known_hosts"}


def test_per_host_layout_resolves_under_the_hosts_own_dir(tmp_path: Path) -> None:
    host_id = HostId.generate()
    host_dir = tmp_path / "hosts" / str(host_id)

    assert host_access_key_dir(tmp_path, _PER_HOST) == tmp_path
    assert host_access_per_host_dir(tmp_path, _PER_HOST, host_id) == host_dir
    assert host_access_pin_store_paths(tmp_path, _PER_HOST, host_id) == {"known_hosts": host_dir / "known_hosts"}


def test_manifest_refuses_an_agent_key_it_does_not_carry() -> None:
    with pytest.raises(ValidationError, match="agent key"):
        HostAccessManifest(
            key_dir_name="keys",
            per_host_dir_scheme=PerHostDirScheme.HOST_KEYS_UUID_HEX,
            file_names=keypair_file_names("host_key"),
            agent_key_file_name="client_key",
            pin_store_names=("known_hosts",),
            is_pin_store_per_host=False,
            known_hosts_link_store_name=None,
        )


def test_manifest_refuses_a_known_hosts_link_into_a_per_host_store() -> None:
    with pytest.raises(ValidationError, match="link target"):
        HostAccessManifest(
            key_dir_name="keys",
            per_host_dir_scheme=PerHostDirScheme.HOSTS_HOST_ID,
            file_names=keypair_file_names("client_key"),
            agent_key_file_name="client_key",
            pin_store_names=("known_hosts",),
            is_pin_store_per_host=True,
            known_hosts_link_store_name="known_hosts",
        )
