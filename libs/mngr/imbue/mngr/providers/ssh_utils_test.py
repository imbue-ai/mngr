"""Unit tests for SSH key generation and management utilities."""

import socket
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.hazmat.primitives.serialization import load_ssh_private_key
from pyinfra.api import Host as PyinfraHost

from imbue.mngr.errors import MngrError
from imbue.mngr.providers.ssh_utils import add_host_to_known_hosts
from imbue.mngr.providers.ssh_utils import clear_host_from_known_hosts
from imbue.mngr.providers.ssh_utils import create_pyinfra_host
from imbue.mngr.providers.ssh_utils import generate_ed25519_host_keypair
from imbue.mngr.providers.ssh_utils import generate_ssh_keypair
from imbue.mngr.providers.ssh_utils import load_or_create_host_keypair
from imbue.mngr.providers.ssh_utils import load_or_create_ssh_keypair
from imbue.mngr.providers.ssh_utils import save_ssh_keypair
from imbue.mngr.providers.ssh_utils import wait_for_sshd

# =============================================================================
# generate_ssh_keypair
# =============================================================================


def test_generate_ssh_keypair_returns_strings() -> None:
    """generate_ssh_keypair should return a tuple of two strings."""
    private_pem, public_openssh = generate_ssh_keypair()
    assert isinstance(private_pem, str)
    assert isinstance(public_openssh, str)


def test_generate_ssh_keypair_private_key_is_pem_rsa() -> None:
    """The private key should be a PEM-encoded RSA key."""
    private_pem, _ = generate_ssh_keypair()
    assert private_pem.startswith("-----BEGIN RSA PRIVATE KEY-----")
    assert "-----END RSA PRIVATE KEY-----" in private_pem


def test_generate_ssh_keypair_public_key_is_openssh_rsa() -> None:
    """The public key should be in OpenSSH format for RSA."""
    _, public_openssh = generate_ssh_keypair()
    assert public_openssh.startswith("ssh-rsa ")


def test_generate_ssh_keypair_rsa_4096_bits() -> None:
    """The RSA key should be 4096 bits."""
    private_pem, _ = generate_ssh_keypair()
    private_key = load_pem_private_key(private_pem.encode("utf-8"), password=None)
    assert isinstance(private_key, rsa.RSAPrivateKey)
    assert private_key.key_size == 4096


def test_generate_ssh_keypair_each_call_produces_unique_keys() -> None:
    """Each call to generate_ssh_keypair should produce a different keypair."""
    _, public_key_1 = generate_ssh_keypair()
    _, public_key_2 = generate_ssh_keypair()
    assert public_key_1 != public_key_2


# =============================================================================
# save_ssh_keypair
# =============================================================================


def test_save_ssh_keypair_creates_files(tmp_path: Path) -> None:
    """save_ssh_keypair should create both private and public key files."""
    key_dir = tmp_path / "keys"
    private_path, public_path = save_ssh_keypair(key_dir)
    assert private_path.exists()
    assert public_path.exists()


def test_save_ssh_keypair_returns_correct_paths(tmp_path: Path) -> None:
    """save_ssh_keypair should return paths matching the expected filenames."""
    key_dir = tmp_path / "keys"
    private_path, public_path = save_ssh_keypair(key_dir)
    assert private_path == key_dir / "ssh_key"
    assert public_path == key_dir / "ssh_key.pub"


def test_save_ssh_keypair_custom_key_name(tmp_path: Path) -> None:
    """save_ssh_keypair should use the provided key name."""
    key_dir = tmp_path / "keys"
    private_path, public_path = save_ssh_keypair(key_dir, key_name="id_rsa")
    assert private_path == key_dir / "id_rsa"
    assert public_path == key_dir / "id_rsa.pub"
    assert private_path.exists()
    assert public_path.exists()


def test_save_ssh_keypair_private_key_permissions(tmp_path: Path) -> None:
    """save_ssh_keypair should set private key permissions to 0o600."""
    key_dir = tmp_path / "keys"
    private_path, _ = save_ssh_keypair(key_dir)
    file_mode = stat.S_IMODE(private_path.stat().st_mode)
    assert file_mode == 0o600


def test_save_ssh_keypair_public_key_permissions(tmp_path: Path) -> None:
    """save_ssh_keypair should set public key permissions to 0o644."""
    key_dir = tmp_path / "keys"
    _, public_path = save_ssh_keypair(key_dir)
    file_mode = stat.S_IMODE(public_path.stat().st_mode)
    assert file_mode == 0o644


def test_save_ssh_keypair_private_key_content_is_valid_pem(tmp_path: Path) -> None:
    """save_ssh_keypair should write valid PEM content to the private key file."""
    key_dir = tmp_path / "keys"
    private_path, _ = save_ssh_keypair(key_dir)
    content = private_path.read_text()
    assert content.startswith("-----BEGIN RSA PRIVATE KEY-----")


def test_save_ssh_keypair_public_key_content_is_openssh(tmp_path: Path) -> None:
    """save_ssh_keypair should write valid OpenSSH content to the public key file."""
    key_dir = tmp_path / "keys"
    _, public_path = save_ssh_keypair(key_dir)
    content = public_path.read_text()
    assert content.startswith("ssh-rsa ")


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
    assert public_content.startswith("ssh-rsa ")


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
    # Write a public key file with trailing whitespace
    (key_dir / "ssh_key").write_text("-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----\n")
    (key_dir / "ssh_key.pub").write_text("ssh-rsa AAAA fakekey\n\n")

    _, public_content = load_or_create_ssh_keypair(key_dir)
    assert not public_content.endswith("\n")
    assert public_content == "ssh-rsa AAAA fakekey"


def test_load_or_create_ssh_keypair_custom_key_name(tmp_path: Path) -> None:
    """load_or_create_ssh_keypair should use the provided key name."""
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    private_path, _ = load_or_create_ssh_keypair(key_dir, key_name="mykey")
    assert private_path == key_dir / "mykey"
    assert (key_dir / "mykey.pub").exists()


# =============================================================================
# generate_ed25519_host_keypair
# =============================================================================


def test_generate_ed25519_host_keypair_returns_strings() -> None:
    """generate_ed25519_host_keypair should return a tuple of two strings."""
    private_pem, public_openssh = generate_ed25519_host_keypair()
    assert isinstance(private_pem, str)
    assert isinstance(public_openssh, str)


def test_generate_ed25519_host_keypair_private_key_is_openssh_format() -> None:
    """The private key should be in OpenSSH PEM format."""
    private_pem, _ = generate_ed25519_host_keypair()
    assert private_pem.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert "-----END OPENSSH PRIVATE KEY-----" in private_pem


def test_generate_ed25519_host_keypair_public_key_is_ed25519() -> None:
    """The public key should be in OpenSSH format for Ed25519."""
    _, public_openssh = generate_ed25519_host_keypair()
    assert public_openssh.startswith("ssh-ed25519 ")


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


def test_add_host_to_known_hosts_creates_file_if_missing(tmp_path: Path) -> None:
    """add_host_to_known_hosts should create the file if it doesn't exist."""
    known_hosts = tmp_path / "ssh" / "known_hosts"
    add_host_to_known_hosts(known_hosts, "example.com", 22, "ssh-ed25519 AAAAC3Nza hostkey")
    assert known_hosts.exists()


def test_add_host_to_known_hosts_standard_port_uses_bare_hostname(tmp_path: Path) -> None:
    """add_host_to_known_hosts should use bare hostname format for port 22."""
    known_hosts = tmp_path / "known_hosts"
    add_host_to_known_hosts(known_hosts, "example.com", 22, "ssh-ed25519 AAAAC3Nza hostkey")
    content = known_hosts.read_text()
    assert content.startswith("example.com ssh-ed25519 AAAAC3Nza hostkey\n")


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


def test_add_host_to_known_hosts_creates_parent_directories(tmp_path: Path) -> None:
    """add_host_to_known_hosts should create parent directories if needed."""
    known_hosts = tmp_path / "deep" / "nested" / "known_hosts"
    add_host_to_known_hosts(known_hosts, "example.com", 22, "ssh-ed25519 AAAA hostkey")
    assert known_hosts.exists()


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
# create_pyinfra_host
# =============================================================================


def test_create_pyinfra_host_returns_pyinfra_host(tmp_path: Path) -> None:
    """create_pyinfra_host should return a PyinfraHost object."""
    private_key_path, _ = save_ssh_keypair(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"

    host = create_pyinfra_host(
        hostname="127.0.0.1",
        port=22,
        private_key_path=private_key_path,
        known_hosts_path=known_hosts_path,
    )

    assert isinstance(host, PyinfraHost)


def test_create_pyinfra_host_has_correct_hostname(tmp_path: Path) -> None:
    """create_pyinfra_host should configure the host with the given hostname."""
    private_key_path, _ = save_ssh_keypair(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"

    host = create_pyinfra_host(
        hostname="myhost.example.com",
        port=22,
        private_key_path=private_key_path,
        known_hosts_path=known_hosts_path,
    )

    assert host.name == "myhost.example.com"


def test_create_pyinfra_host_uses_custom_ssh_user(tmp_path: Path) -> None:
    """create_pyinfra_host should pass through the ssh_user to the host data."""
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


def test_create_pyinfra_host_default_user_is_root(tmp_path: Path) -> None:
    """create_pyinfra_host should default to 'root' as the ssh_user."""
    private_key_path, _ = save_ssh_keypair(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"

    host = create_pyinfra_host(
        hostname="127.0.0.1",
        port=2222,
        private_key_path=private_key_path,
        known_hosts_path=known_hosts_path,
    )

    assert host.data.get("ssh_user") == "root"


def test_create_pyinfra_host_correct_port(tmp_path: Path) -> None:
    """create_pyinfra_host should set the ssh_port correctly."""
    private_key_path, _ = save_ssh_keypair(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"

    host = create_pyinfra_host(
        hostname="127.0.0.1",
        port=2222,
        private_key_path=private_key_path,
        known_hosts_path=known_hosts_path,
    )

    assert host.data.get("ssh_port") == 2222


def test_create_pyinfra_host_known_hosts_path_is_set(tmp_path: Path) -> None:
    """create_pyinfra_host should set the ssh_known_hosts_file on the host."""
    private_key_path, _ = save_ssh_keypair(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"

    host = create_pyinfra_host(
        hostname="127.0.0.1",
        port=22,
        private_key_path=private_key_path,
        known_hosts_path=known_hosts_path,
    )

    assert host.data.get("ssh_known_hosts_file") == str(known_hosts_path)


def test_create_pyinfra_host_private_key_path_is_set(tmp_path: Path) -> None:
    """create_pyinfra_host should set the ssh_key path on the host."""
    private_key_path, _ = save_ssh_keypair(tmp_path)
    known_hosts_path = tmp_path / "known_hosts"

    host = create_pyinfra_host(
        hostname="127.0.0.1",
        port=22,
        private_key_path=private_key_path,
        known_hosts_path=known_hosts_path,
    )

    assert host.data.get("ssh_key") == str(private_key_path)
