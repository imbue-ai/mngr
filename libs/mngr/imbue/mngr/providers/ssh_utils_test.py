"""Unit tests for SSH key generation and management utilities."""

import contextlib
import socket
import stat
import threading
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import paramiko
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.hazmat.primitives.serialization import PublicFormat
from cryptography.hazmat.primitives.serialization import load_ssh_private_key
from paramiko.common import AUTH_FAILED
from paramiko.common import AUTH_SUCCESSFUL
from paramiko.common import OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
from paramiko.common import OPEN_SUCCEEDED
from pyinfra.api import Host as PyinfraHost

from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.host_key_store import HostKeyOrigin
from imbue.mngr.providers.host_key_store import has_host_key_store
from imbue.mngr.providers.host_key_store import load_host_key_record
from imbue.mngr.providers.ssh_utils import SSH_BANNER_TIMEOUT_SECONDS
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import clear_host_from_known_hosts
from imbue.mngr.providers.ssh_utils import create_pyinfra_host
from imbue.mngr.providers.ssh_utils import ensure_per_host_known_hosts_link
from imbue.mngr.providers.ssh_utils import format_as_known_hosts_address
from imbue.mngr.providers.ssh_utils import generate_ed25519_host_keypair
from imbue.mngr.providers.ssh_utils import generate_ssh_keypair
from imbue.mngr.providers.ssh_utils import load_or_create_host_keypair
from imbue.mngr.providers.ssh_utils import load_or_create_per_host_client_keypair
from imbue.mngr.providers.ssh_utils import load_or_create_per_host_host_keypair
from imbue.mngr.providers.ssh_utils import load_or_create_ssh_keypair
from imbue.mngr.providers.ssh_utils import parse_openssh_public_key_blob
from imbue.mngr.providers.ssh_utils import per_host_key_dir
from imbue.mngr.providers.ssh_utils import read_host_public_key_with_legacy_fallback
from imbue.mngr.providers.ssh_utils import resolve_per_host_client_keypair
from imbue.mngr.providers.ssh_utils import resolve_per_host_host_keypair
from imbue.mngr.providers.ssh_utils import save_ssh_keypair
from imbue.mngr.providers.ssh_utils import wait_for_expected_host_key
from imbue.mngr.providers.ssh_utils import wait_for_sshd
from imbue.mngr.providers.ssh_utils import wait_for_sshd_with_retry
from imbue.mngr.utils.testing import allow_warnings

# =============================================================================
# generate_ssh_keypair
# =============================================================================


def test_generate_ssh_keypair_produces_ed25519_openssh_format() -> None:
    """Client keypairs are Ed25519 (owner-exec envelope auth accepts only Ed25519)."""
    private_pem, public_openssh = generate_ssh_keypair()
    private_key = load_ssh_private_key(private_pem.encode("utf-8"), password=None)
    assert isinstance(private_key, ed25519.Ed25519PrivateKey)
    assert public_openssh.startswith("ssh-ed25519 ")


def test_generate_ssh_keypair_each_call_produces_unique_keys() -> None:
    """Each call to generate_ssh_keypair should produce a different keypair."""
    _, public_key_1 = generate_ssh_keypair()
    _, public_key_2 = generate_ssh_keypair()
    assert public_key_1 != public_key_2


# =============================================================================
# save_ssh_keypair
# =============================================================================


def test_save_ssh_keypair_writes_valid_keys_with_correct_permissions(tmp_path: Path) -> None:
    """save_ssh_keypair should write an OpenSSH private key (0o600) and OpenSSH public key (0o644)."""
    key_dir = tmp_path / "keys"
    private_path, public_path = save_ssh_keypair(key_dir)

    assert private_path == key_dir / "ssh_key"
    assert public_path == key_dir / "ssh_key.pub"

    assert private_path.read_text().startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert public_path.read_text().startswith("ssh-ed25519 ")

    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(public_path.stat().st_mode) == 0o644


def test_save_ssh_keypair_custom_key_name(tmp_path: Path) -> None:
    """save_ssh_keypair should use the provided key name."""
    key_dir = tmp_path / "keys"
    private_path, public_path = save_ssh_keypair(key_dir, key_name="id_custom")
    assert private_path == key_dir / "id_custom"
    assert public_path == key_dir / "id_custom.pub"
    assert private_path.read_text().startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert public_path.read_text().startswith("ssh-ed25519 ")


def test_save_ssh_keypair_creates_parent_directories(tmp_path: Path) -> None:
    """save_ssh_keypair should create parent directories if they don't exist."""
    key_dir = tmp_path / "nested" / "key" / "dir"
    save_ssh_keypair(key_dir)
    assert key_dir.exists()


# =============================================================================
# load_or_create_ssh_keypair
# =============================================================================


def test_load_or_create_ssh_keypair_creates_keys_when_missing(tmp_path: Path) -> None:
    """load_or_create_ssh_keypair should create keys if they don't exist."""
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    private_path, public_content = load_or_create_ssh_keypair(key_dir)
    assert private_path.exists()
    assert (key_dir / "ssh_key.pub").exists()
    assert public_content.startswith("ssh-ed25519 ")


def test_load_or_create_ssh_keypair_returns_existing_keys(tmp_path: Path) -> None:
    """load_or_create_ssh_keypair should load existing keys without regenerating."""
    key_dir = tmp_path / "keys"
    key_dir.mkdir()

    # Create keys the first time
    _, original_public = load_or_create_ssh_keypair(key_dir)

    # Load again - should return the same key
    _, loaded_public = load_or_create_ssh_keypair(key_dir)

    assert original_public == loaded_public


def test_load_or_create_ssh_keypair_returns_path_to_private_key(tmp_path: Path) -> None:
    """load_or_create_ssh_keypair should return the correct private key path."""
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    private_path, _ = load_or_create_ssh_keypair(key_dir)
    assert private_path == key_dir / "ssh_key"


def test_load_or_create_ssh_keypair_strips_whitespace_from_public_key(tmp_path: Path) -> None:
    """load_or_create_ssh_keypair should strip whitespace from the public key content."""
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    # Generate real keys, then add trailing whitespace to the public key file.
    save_ssh_keypair(key_dir)
    pub_path = key_dir / "ssh_key.pub"
    original_content = pub_path.read_text().strip()
    pub_path.write_text(original_content + "\n\n")

    _, public_content = load_or_create_ssh_keypair(key_dir)
    assert not public_content.endswith("\n")
    assert public_content == original_content


def test_load_or_create_ssh_keypair_custom_key_name(tmp_path: Path) -> None:
    """load_or_create_ssh_keypair should use the provided key name."""
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    private_path, _ = load_or_create_ssh_keypair(key_dir, key_name="mykey")
    assert private_path == key_dir / "mykey"
    assert (key_dir / "mykey.pub").exists()


# 16 concurrent keypair generations, serialized behind a lock, is 9-15s of real
# CPU on its own and overruns the 10s global timeout when the suite is running
# xdist-parallel against other CPU-heavy tests. The contention is the point of
# the test, so it gets the time rather than a flaky mark.
@pytest.mark.timeout(30)
def test_load_or_create_ssh_keypair_concurrent_first_creation_is_consistent(tmp_path: Path) -> None:
    """Concurrent first-time creation must yield one consistent, non-empty keypair.

    The parallel host-discovery fan-out opens one SSH connection per VPS and
    each lazily calls load_or_create_ssh_keypair on the same directory. Without
    serialized, atomic creation, racing writers produced a transient zero-byte
    or mismatched ``.pub`` (paramiko then raised "Not enough fields for public
    blob"). All concurrent callers must observe the same fully-written keypair.
    """
    key_dir = tmp_path / "keys"
    barrier = threading.Barrier(16)

    def _worker() -> str:
        # Release all workers simultaneously to maximize contention on first creation.
        barrier.wait()
        _, public_content = load_or_create_ssh_keypair(key_dir, key_name="vps_ssh_key")
        return public_content

    with ThreadPoolExecutor(max_workers=16) as executor:
        public_contents = [f.result() for f in [executor.submit(_worker) for _ in range(16)]]

    # Every caller saw a non-empty public key, and they all agree on the same one.
    assert all(content for content in public_contents)
    assert len(set(public_contents)) == 1

    # The on-disk public key matches the private key (a real, parseable pair),
    # so no caller wrote a public key from a different generated keypair.
    private_key = load_ssh_private_key((key_dir / "vps_ssh_key").read_bytes(), password=None)
    expected_public = (
        private_key.public_key().public_bytes(encoding=Encoding.OpenSSH, format=PublicFormat.OpenSSH).decode("utf-8")
    )
    assert public_contents[0] == expected_public


# =============================================================================
# generate_ed25519_host_keypair
# =============================================================================


def test_generate_ed25519_host_keypair_produces_valid_ed25519_key() -> None:
    """The private key should be a valid Ed25519 key."""
    private_pem, _ = generate_ed25519_host_keypair()
    private_key = load_ssh_private_key(private_pem.encode("utf-8"), password=None)
    assert isinstance(private_key, ed25519.Ed25519PrivateKey)


def test_generate_ed25519_host_keypair_each_call_unique() -> None:
    """Each call should produce a unique keypair."""
    _, public_1 = generate_ed25519_host_keypair()
    _, public_2 = generate_ed25519_host_keypair()
    assert public_1 != public_2


# =============================================================================
# load_or_create_host_keypair
# =============================================================================


def test_load_or_create_host_keypair_creates_keys_when_missing(tmp_path: Path) -> None:
    """load_or_create_host_keypair should create Ed25519 keys if they don't exist."""
    key_dir = tmp_path / "hostkeys"
    private_path, public_content = load_or_create_host_keypair(key_dir)
    assert private_path.exists()
    assert (key_dir / "host_key.pub").exists()
    assert public_content.startswith("ssh-ed25519 ")


def test_load_or_create_host_keypair_returns_existing_keys(tmp_path: Path) -> None:
    """load_or_create_host_keypair should load existing keys without regenerating."""
    key_dir = tmp_path / "hostkeys"

    _, original_public = load_or_create_host_keypair(key_dir)
    _, loaded_public = load_or_create_host_keypair(key_dir)

    assert original_public == loaded_public


def test_load_or_create_host_keypair_private_key_permissions(tmp_path: Path) -> None:
    """load_or_create_host_keypair should set private key permissions to 0o600."""
    key_dir = tmp_path / "hostkeys"
    private_path, _ = load_or_create_host_keypair(key_dir)
    file_mode = stat.S_IMODE(private_path.stat().st_mode)
    assert file_mode == 0o600


def test_load_or_create_host_keypair_public_key_permissions(tmp_path: Path) -> None:
    """load_or_create_host_keypair should set public key permissions to 0o644."""
    key_dir = tmp_path / "hostkeys"
    load_or_create_host_keypair(key_dir)
    public_path = key_dir / "host_key.pub"
    file_mode = stat.S_IMODE(public_path.stat().st_mode)
    assert file_mode == 0o644


def test_load_or_create_host_keypair_creates_parent_directories(tmp_path: Path) -> None:
    """load_or_create_host_keypair should create parent directories if missing."""
    key_dir = tmp_path / "deep" / "nested" / "dir"
    private_path, _ = load_or_create_host_keypair(key_dir)
    assert key_dir.exists()
    assert private_path.exists()


def test_load_or_create_host_keypair_returns_path_to_private_key(tmp_path: Path) -> None:
    """load_or_create_host_keypair should return the correct private key path."""
    key_dir = tmp_path / "hostkeys"
    private_path, _ = load_or_create_host_keypair(key_dir)
    assert private_path == key_dir / "host_key"


def test_load_or_create_host_keypair_custom_key_name(tmp_path: Path) -> None:
    """load_or_create_host_keypair should use the provided key name."""
    key_dir = tmp_path / "hostkeys"
    private_path, _ = load_or_create_host_keypair(key_dir, key_name="myhost")
    assert private_path == key_dir / "myhost"
    assert (key_dir / "myhost.pub").exists()


# =============================================================================
# format_as_known_hosts_address
# =============================================================================


def test_format_as_known_hosts_address_returns_bare_hostname_for_standard_port() -> None:
    """Port 22 must produce a bare hostname with no brackets and no port suffix."""
    assert format_as_known_hosts_address("example.com", 22) == "example.com"
    assert format_as_known_hosts_address("127.0.0.1", 22) == "127.0.0.1"


def test_format_as_known_hosts_address_returns_bracketed_form_for_nonstandard_port() -> None:
    """Any non-22 port must produce OpenSSH's ``[host]:port`` form."""
    assert format_as_known_hosts_address("example.com", 2222) == "[example.com]:2222"
    # Lima's typical forwarded-port shape for the loopback hostname.
    assert format_as_known_hosts_address("127.0.0.1", 60022) == "[127.0.0.1]:60022"


# =============================================================================
# clear_host_from_known_hosts
# =============================================================================


def test_clear_host_from_known_hosts_no_op_when_file_missing(tmp_path: Path) -> None:
    """clear_host_from_known_hosts should do nothing if the file doesn't exist."""
    known_hosts = tmp_path / "known_hosts"
    # Should not raise
    clear_host_from_known_hosts(known_hosts, "example.com", 22)
    assert not known_hosts.exists()


def test_clear_host_from_known_hosts_removes_standard_port_entry(tmp_path: Path) -> None:
    """clear_host_from_known_hosts should remove the entry for port 22 using bare hostname."""
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(
        "example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA hostkey\n"
        "other.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA otherkey\n"
    )

    clear_host_from_known_hosts(known_hosts, "example.com", 22)

    content = known_hosts.read_text()
    assert "example.com" not in content
    assert "other.com" in content


def test_clear_host_from_known_hosts_removes_nonstandard_port_entry(tmp_path: Path) -> None:
    """clear_host_from_known_hosts should remove entries using [host]:port format for non-22 ports."""
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(
        "[example.com]:2222 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA hostkey\n"
        "other.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA otherkey\n"
    )

    clear_host_from_known_hosts(known_hosts, "example.com", 2222)

    content = known_hosts.read_text()
    assert "[example.com]:2222" not in content
    assert "other.com" in content


def test_clear_host_from_known_hosts_no_change_if_host_not_present(tmp_path: Path) -> None:
    """clear_host_from_known_hosts should leave the file unchanged if host is not present."""
    known_hosts = tmp_path / "known_hosts"
    original_content = "other.com ssh-ed25519 AAAA otherkey\n"
    known_hosts.write_text(original_content)

    clear_host_from_known_hosts(known_hosts, "example.com", 22)

    assert known_hosts.read_text() == original_content


def test_clear_host_from_known_hosts_removes_multiple_entries_for_host(tmp_path: Path) -> None:
    """clear_host_from_known_hosts should remove all entries for a given host."""
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text(
        "example.com ssh-rsa AAAAB3NzaC1yc2EAAAA rsakey\n"
        "example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA ed25519key\n"
        "other.com ssh-ed25519 AAAA otherkey\n"
    )

    clear_host_from_known_hosts(known_hosts, "example.com", 22)

    content = known_hosts.read_text()
    assert "example.com" not in content
    assert "other.com" in content


# =============================================================================
# add_host_to_known_hosts
# =============================================================================


def test_add_host_to_known_hosts_creates_file_with_correct_content(tmp_path: Path) -> None:
    """add_host_to_known_hosts should create parent dirs and file with bare hostname for port 22."""
    known_hosts = tmp_path / "ssh" / "known_hosts"
    add_host_to_known_hosts(known_hosts, "example.com", 22, "ssh-ed25519 AAAAC3Nza hostkey")
    content = known_hosts.read_text()
    assert content == "example.com ssh-ed25519 AAAAC3Nza hostkey\n"


def test_add_host_to_known_hosts_nonstandard_port_uses_bracket_format(tmp_path: Path) -> None:
    """add_host_to_known_hosts should use [host]:port format for non-standard ports."""
    known_hosts = tmp_path / "known_hosts"
    add_host_to_known_hosts(known_hosts, "example.com", 2222, "ssh-ed25519 AAAAC3Nza hostkey")
    content = known_hosts.read_text()
    assert "[example.com]:2222 ssh-ed25519 AAAAC3Nza hostkey\n" in content


def test_add_host_to_known_hosts_no_duplicate_if_entry_exists(tmp_path: Path) -> None:
    """add_host_to_known_hosts should not add duplicate entries."""
    known_hosts = tmp_path / "known_hosts"
    public_key = "ssh-ed25519 AAAAC3Nza hostkey"
    add_host_to_known_hosts(known_hosts, "example.com", 22, public_key)
    add_host_to_known_hosts(known_hosts, "example.com", 22, public_key)

    content = known_hosts.read_text()
    assert content.count("example.com ssh-ed25519") == 1


def test_add_host_to_known_hosts_replaces_stale_entry_same_key_type(tmp_path: Path) -> None:
    """add_host_to_known_hosts should replace a stale entry with the same key type."""
    known_hosts = tmp_path / "known_hosts"
    old_key = "ssh-ed25519 AAAAC3Nza oldkey"
    new_key = "ssh-ed25519 AAAAC3Nza newkey"

    add_host_to_known_hosts(known_hosts, "example.com", 22, old_key)
    add_host_to_known_hosts(known_hosts, "example.com", 22, new_key)

    content = known_hosts.read_text()
    assert "oldkey" not in content
    assert "newkey" in content
    assert content.count("example.com ssh-ed25519") == 1


def test_add_host_to_known_hosts_preserves_different_key_types(tmp_path: Path) -> None:
    """add_host_to_known_hosts should preserve entries with different key types."""
    known_hosts = tmp_path / "known_hosts"
    rsa_key = "ssh-rsa AAAAB3NzaC1yc2EAAAA rsakey"
    ed25519_key = "ssh-ed25519 AAAAC3Nza ed25519key"

    add_host_to_known_hosts(known_hosts, "example.com", 22, rsa_key)
    add_host_to_known_hosts(known_hosts, "example.com", 22, ed25519_key)

    content = known_hosts.read_text()
    assert "ssh-rsa" in content
    assert "ssh-ed25519" in content


# =============================================================================
# store-backed shim behavior
# =============================================================================


def test_add_host_to_known_hosts_without_host_id_creates_no_pin_store(tmp_path: Path) -> None:
    """Legacy callers (throwaway known_hosts files) must stay sidecar-free."""
    known_hosts = tmp_path / "known_hosts"
    add_host_to_known_hosts(known_hosts, "example.com", 22, "ssh-ed25519 AAAAC3Nza hostkey")
    assert not has_host_key_store(known_hosts)


def test_add_host_to_known_hosts_with_host_id_writes_through_the_store(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()

    add_host_to_known_hosts(known_hosts, "example.com", 22, "ssh-ed25519 AAAAC3Nza hostkey", host_id=host_id)

    assert has_host_key_store(known_hosts)
    assert known_hosts.read_text() == "example.com ssh-ed25519 AAAAC3Nza hostkey\n"
    record = load_host_key_record(known_hosts, host_id)
    assert record is not None
    assert [pin.public_key for pin in record.pins] == ["ssh-ed25519 AAAAC3Nza hostkey"]


def test_add_host_to_known_hosts_first_store_write_imports_existing_lines(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    add_host_to_known_hosts(known_hosts, "legacy.com", 22, "ssh-ed25519 AAAAC3Nza legacykey")

    add_host_to_known_hosts(known_hosts, "example.com", 22, "ssh-ed25519 AAAAC3Nza newkey", host_id=HostId.generate())

    content = known_hosts.read_text()
    assert "legacy.com ssh-ed25519 AAAAC3Nza legacykey" in content
    assert "example.com ssh-ed25519 AAAAC3Nza newkey" in content


def test_add_host_to_known_hosts_routes_through_existing_store_without_host_id(tmp_path: Path) -> None:
    """Once a file has a store, a host_id-less bootstrap add cannot displace a USER pin."""
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()
    add_host_to_known_hosts(
        known_hosts, "example.com", 22, "ssh-ed25519 AAAAC3Nza userkey", host_id=host_id, origin=HostKeyOrigin.USER
    )

    add_host_to_known_hosts(known_hosts, "example.com", 22, "ssh-ed25519 AAAAC3Nza bootkey")

    assert known_hosts.read_text() == "example.com ssh-ed25519 AAAAC3Nza userkey\n"


def test_clear_host_from_known_hosts_drops_endpoint_pins_from_the_store(tmp_path: Path) -> None:
    known_hosts = tmp_path / "known_hosts"
    host_id = HostId.generate()
    add_host_to_known_hosts(known_hosts, "example.com", 22, "ssh-ed25519 AAAAC3Nza hostkey", host_id=host_id)
    add_host_to_known_hosts(known_hosts, "other.com", 22, "ssh-ed25519 AAAAC3Nza otherkey", host_id=host_id)

    clear_host_from_known_hosts(known_hosts, "example.com", 22)

    assert known_hosts.read_text() == "other.com ssh-ed25519 AAAAC3Nza otherkey\n"
    record = load_host_key_record(known_hosts, host_id)
    assert record is not None
    assert [pin.address for pin in record.pins] == ["other.com"]


# =============================================================================
# per-host keypair helpers
# =============================================================================


def test_load_or_create_per_host_client_keypair_is_unique_per_host(tmp_path: Path) -> None:
    first_host = HostId.generate()
    second_host = HostId.generate()

    first_path, first_public = load_or_create_per_host_client_keypair(
        tmp_path, first_host, "docker_ssh_key", "known_hosts"
    )
    second_path, second_public = load_or_create_per_host_client_keypair(
        tmp_path, second_host, "docker_ssh_key", "known_hosts"
    )

    assert first_path != second_path
    assert first_public != second_public
    assert first_path == per_host_key_dir(tmp_path, first_host) / "docker_ssh_key"


def test_resolve_per_host_client_keypair_prefers_per_host_pair(tmp_path: Path) -> None:
    host_id = HostId.generate()
    save_ssh_keypair(tmp_path, "docker_ssh_key")
    per_host_path, per_host_public = load_or_create_per_host_client_keypair(
        tmp_path, host_id, "docker_ssh_key", "known_hosts"
    )

    resolved_path, resolved_public = resolve_per_host_client_keypair(
        tmp_path, host_id, "docker_ssh_key", "known_hosts"
    )

    assert resolved_path == per_host_path
    assert resolved_public == per_host_public


def test_resolve_per_host_client_keypair_falls_back_to_legacy_shared_pair(tmp_path: Path) -> None:
    host_id = HostId.generate()
    legacy_path, legacy_public_path = save_ssh_keypair(tmp_path, "docker_ssh_key")

    resolved_path, resolved_public = resolve_per_host_client_keypair(
        tmp_path, host_id, "docker_ssh_key", "known_hosts"
    )

    assert resolved_path == legacy_path
    assert resolved_public == legacy_public_path.read_text().strip()


def test_resolve_per_host_client_keypair_creates_per_host_pair_when_neither_exists(tmp_path: Path) -> None:
    host_id = HostId.generate()

    resolved_path, resolved_public = resolve_per_host_client_keypair(
        tmp_path, host_id, "docker_ssh_key", "known_hosts"
    )

    assert resolved_path == per_host_key_dir(tmp_path, host_id) / "docker_ssh_key"
    assert resolved_public.startswith("ssh-ed25519 ")


def test_resolve_per_host_client_keypair_links_known_hosts_for_preexisting_per_host_pair(tmp_path: Path) -> None:
    """A per-host pair minted before the sibling link existed gains the link on resolution."""
    host_id = HostId.generate()
    save_ssh_keypair(per_host_key_dir(tmp_path, host_id), "docker_ssh_key")

    resolve_per_host_client_keypair(tmp_path, host_id, "docker_ssh_key", "known_hosts")

    assert (per_host_key_dir(tmp_path, host_id) / "known_hosts").is_symlink()


def test_resolve_per_host_host_keypair_falls_back_to_legacy_shared_pair(tmp_path: Path) -> None:
    host_id = HostId.generate()
    legacy_path, legacy_public = load_or_create_host_keypair(tmp_path, "host_key")

    resolved_path, resolved_public = resolve_per_host_host_keypair(tmp_path, host_id, "host_key")

    assert resolved_path == legacy_path
    assert resolved_public == legacy_public


def test_read_host_public_key_with_legacy_fallback_prefers_per_host_key(tmp_path: Path) -> None:
    host_id = HostId.generate()
    load_or_create_host_keypair(tmp_path, "host_key")
    _, per_host_public = load_or_create_per_host_host_keypair(tmp_path, host_id, "host_key")

    assert read_host_public_key_with_legacy_fallback(tmp_path, host_id, "host_key") == per_host_public


def test_read_host_public_key_with_legacy_fallback_returns_none_when_neither_exists(tmp_path: Path) -> None:
    assert read_host_public_key_with_legacy_fallback(tmp_path, HostId.generate(), "host_key") is None


def test_ensure_per_host_known_hosts_link_reads_through_to_the_provider_file(tmp_path: Path) -> None:
    """Key-sibling consumers (the forward tunnel) find the provider-wide pins next to a per-host key."""
    host_id = HostId.generate()
    add_host_to_known_hosts(tmp_path / "known_hosts", "example.com", 22, "ssh-ed25519 AAAAC3Nza hostkey")

    ensure_per_host_known_hosts_link(tmp_path, host_id, "known_hosts")

    sibling = per_host_key_dir(tmp_path, host_id) / "known_hosts"
    assert sibling.is_symlink()
    assert sibling.read_text() == "example.com ssh-ed25519 AAAAC3Nza hostkey\n"


def test_ensure_per_host_known_hosts_link_sees_later_pins(tmp_path: Path) -> None:
    """The link never goes stale: pins added after linking are visible through it."""
    host_id = HostId.generate()
    ensure_per_host_known_hosts_link(tmp_path, host_id, "known_hosts")

    add_host_to_known_hosts(tmp_path / "known_hosts", "example.com", 22, "ssh-ed25519 AAAAC3Nza hostkey")

    sibling = per_host_key_dir(tmp_path, host_id) / "known_hosts"
    assert sibling.read_text() == "example.com ssh-ed25519 AAAAC3Nza hostkey\n"


def test_ensure_per_host_known_hosts_link_is_idempotent(tmp_path: Path) -> None:
    host_id = HostId.generate()
    ensure_per_host_known_hosts_link(tmp_path, host_id, "known_hosts")
    ensure_per_host_known_hosts_link(tmp_path, host_id, "known_hosts")
    assert (per_host_key_dir(tmp_path, host_id) / "known_hosts").is_symlink()


# =============================================================================
# wait_for_sshd
# =============================================================================


def test_wait_for_sshd_raises_on_non_listening_port() -> None:
    """wait_for_sshd should raise MngrError when no server is available and timeout is 0."""
    # Find a port that is not listening
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        unused_port = s.getsockname()[1]

    with pytest.raises(MngrError, match="SSH server not ready after"):
        wait_for_sshd("127.0.0.1", unused_port, timeout_seconds=0.0)


# =============================================================================
# wait_for_sshd_with_retry
# =============================================================================

# A username that never matches the local OS user, so a probe that forgot to
# pass its username through (paramiko then defaults to getpass.getuser())
# reliably fails these tests instead of accidentally passing.
_TEST_SSH_USERNAME: str = "mngr-test-ssh-user"


class _ConfigurableSshServer(paramiko.ServerInterface):
    """In-process SSH server that accepts only a fixed username and public key."""

    def __init__(self, allowed_username: str, allowed_public_key_blob: str, allow_sessions: bool) -> None:
        self._allowed_username = allowed_username
        self._allowed_public_key_blob = allowed_public_key_blob
        self._allow_sessions = allow_sessions

    def check_auth_publickey(self, username: str, key: paramiko.PKey) -> int:
        if username == self._allowed_username and key.get_base64() == self._allowed_public_key_blob:
            return AUTH_SUCCESSFUL
        return AUTH_FAILED

    def get_allowed_auths(self, username: str) -> str:
        return "publickey"

    def check_channel_request(self, kind: str, chanid: int) -> int:
        if kind == "session" and self._allow_sessions:
            return OPEN_SUCCEEDED
        return OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


def _handle_test_ssh_connection(
    client_sock: socket.socket,
    host_key: paramiko.RSAKey,
    server: paramiko.ServerInterface,
) -> None:
    transport = paramiko.Transport(client_sock)
    transport.add_server_key(host_key)
    try:
        transport.start_server(server=server)
    except (paramiko.SSHException, EOFError, OSError):
        transport.close()


def _accept_test_ssh_connections(
    listening_sock: socket.socket,
    host_key: paramiko.RSAKey,
    allowed_username: str,
    allowed_public_key_blob: str,
    allow_sessions: bool,
    stop_event: threading.Event,
) -> None:
    while not stop_event.is_set():
        try:
            client_sock, _ = listening_sock.accept()
        except socket.timeout:
            continue
        except OSError:
            return
        server = _ConfigurableSshServer(allowed_username, allowed_public_key_blob, allow_sessions)
        threading.Thread(target=_handle_test_ssh_connection, args=(client_sock, host_key, server), daemon=True).start()


@contextlib.contextmanager
def _run_test_ssh_server(
    allowed_username: str,
    allowed_public_key_blob: str,
    allow_sessions: bool,
) -> Generator[int, None, None]:
    """Run a loopback SSH server in a background thread, yielding its port."""
    host_key = paramiko.RSAKey.generate(bits=1024)
    listening_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listening_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listening_sock.bind(("127.0.0.1", 0))
    listening_sock.listen(16)
    listening_sock.settimeout(0.1)
    stop_event = threading.Event()
    accept_thread = threading.Thread(
        target=_accept_test_ssh_connections,
        args=(listening_sock, host_key, allowed_username, allowed_public_key_blob, allow_sessions, stop_event),
        daemon=True,
    )
    accept_thread.start()
    try:
        yield listening_sock.getsockname()[1]
    finally:
        stop_event.set()
        listening_sock.close()
        accept_thread.join(timeout=2.0)


def test_wait_for_sshd_with_retry_succeeds_when_server_accepts_auth_and_sessions(tmp_path: Path) -> None:
    """The probe must authenticate as the given username (not the local OS user) and open a session."""
    private_key_path, public_key_path = save_ssh_keypair(tmp_path)
    public_key_blob = public_key_path.read_text().split()[1]

    with _run_test_ssh_server(_TEST_SSH_USERNAME, public_key_blob, allow_sessions=True) as port:
        wait_for_sshd_with_retry("127.0.0.1", port, 10.0, private_key_path, username=_TEST_SSH_USERNAME)


def test_wait_for_sshd_with_retry_times_out_when_username_is_rejected(tmp_path: Path) -> None:
    """A server that rejects the probe's username must produce the session-open timeout error."""
    private_key_path, public_key_path = save_ssh_keypair(tmp_path)
    public_key_blob = public_key_path.read_text().split()[1]

    with _run_test_ssh_server(_TEST_SSH_USERNAME, public_key_blob, allow_sessions=True) as port:
        with pytest.raises(MngrError, match="could not open sessions"):
            wait_for_sshd_with_retry("127.0.0.1", port, 1.0, private_key_path, username="mngr-wrong-ssh-user")


def test_wait_for_sshd_with_retry_times_out_when_sessions_are_refused(tmp_path: Path) -> None:
    """Auth succeeding is not enough: a server that refuses session channels must time out."""
    private_key_path, public_key_path = save_ssh_keypair(tmp_path)
    public_key_blob = public_key_path.read_text().split()[1]

    with _run_test_ssh_server(_TEST_SSH_USERNAME, public_key_blob, allow_sessions=False) as port:
        with allow_warnings(match="Administratively prohibited"):
            with pytest.raises(MngrError, match="could not open sessions"):
                wait_for_sshd_with_retry("127.0.0.1", port, 1.0, private_key_path, username=_TEST_SSH_USERNAME)


# =============================================================================
# parse_openssh_public_key_blob / wait_for_expected_host_key
# =============================================================================


def test_parse_openssh_public_key_blob_extracts_type_and_blob() -> None:
    assert parse_openssh_public_key_blob("ssh-ed25519 AAAAblob comment here") == ("ssh-ed25519", "AAAAblob")


def test_parse_openssh_public_key_blob_without_comment() -> None:
    assert parse_openssh_public_key_blob("ssh-ed25519 AAAAblob") == ("ssh-ed25519", "AAAAblob")


def test_parse_openssh_public_key_blob_raises_on_malformed() -> None:
    with pytest.raises(MngrError, match="Malformed OpenSSH public key"):
        parse_openssh_public_key_blob("ssh-ed25519")


def test_wait_for_expected_host_key_raises_on_non_listening_port() -> None:
    """Times out (and raises) when nothing is listening, so a key can never match."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        unused_port = s.getsockname()[1]

    with pytest.raises(MngrError, match="did not present the expected SSH host key"):
        wait_for_expected_host_key("127.0.0.1", unused_port, "ssh-ed25519 AAAA test", timeout_seconds=0.0)


# =============================================================================
# create_pyinfra_host
# =============================================================================


def test_create_pyinfra_host_configures_all_ssh_data(tmp_path: Path) -> None:
    """create_pyinfra_host should set hostname, port, key path, known_hosts, and default user."""
    private_key_path, _ = save_ssh_keypair(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"

    host = create_pyinfra_host(
        hostname="myhost.example.com",
        port=2222,
        private_key_path=private_key_path,
        known_hosts_path=known_hosts_path,
    )

    assert isinstance(host, PyinfraHost)
    assert host.name == "myhost.example.com"
    assert host.data.get("ssh_port") == 2222
    assert host.data.get("ssh_user") == "root"
    assert host.data.get("ssh_key") == str(private_key_path)
    assert host.data.get("ssh_known_hosts_file") == str(known_hosts_path)
    # The widened banner window must reach paramiko: a slow-but-working tunnel
    # (e.g. a degraded Modal sandbox) needs more than paramiko's 15s default.
    assert host.data.get("ssh_paramiko_connect_kwargs") == {"banner_timeout": SSH_BANNER_TIMEOUT_SECONDS}


def test_create_pyinfra_host_uses_custom_ssh_user(tmp_path: Path) -> None:
    """create_pyinfra_host should pass through a custom ssh_user."""
    private_key_path, _ = save_ssh_keypair(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"

    host = create_pyinfra_host(
        hostname="127.0.0.1",
        port=22,
        private_key_path=private_key_path,
        known_hosts_path=known_hosts_path,
        ssh_user="ubuntu",
    )

    assert host.data.get("ssh_user") == "ubuntu"
