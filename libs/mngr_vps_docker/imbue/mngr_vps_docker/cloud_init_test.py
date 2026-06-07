"""Tests for cloud-init user_data generation."""

from imbue.mngr_vps_docker.cloud_init import _indent
from imbue.mngr_vps_docker.cloud_init import generate_cloud_init_user_data


def test_indent_single_line() -> None:
    result = _indent("hello", 4)
    assert result == "    hello"


def test_indent_multiple_lines() -> None:
    result = _indent("line1\nline2\nline3", 2)
    assert result == "  line1\n  line2\n  line3"


def test_indent_zero_spaces() -> None:
    result = _indent("hello", 0)
    assert result == "hello"


def test_indent_empty_string() -> None:
    result = _indent("", 4)
    # Empty string has no lines, so splitlines returns [] and join returns ""
    assert result == ""


def test_generate_cloud_init_starts_with_cloud_config() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="-----BEGIN OPENSSH PRIVATE KEY-----\ntest\n-----END OPENSSH PRIVATE KEY-----",
        host_public_key="ssh-ed25519 AAAA testkey",
        install_gvisor_runtime=False,
    )
    assert result.startswith("#cloud-config\n")


def test_generate_cloud_init_contains_host_key() -> None:
    private_key = "-----BEGIN OPENSSH PRIVATE KEY-----\ntest-key-content\n-----END OPENSSH PRIVATE KEY-----"
    public_key = "ssh-ed25519 AAAA testkey"

    result = generate_cloud_init_user_data(
        host_private_key=private_key,
        host_public_key=public_key,
        install_gvisor_runtime=False,
    )

    assert "test-key-content" in result
    assert public_key in result


def test_generate_cloud_init_disables_password_auth() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "ssh_pwauth: false" in result


def test_generate_cloud_init_installs_docker() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "get.docker.com" in result
    assert "systemctl enable docker" in result
    assert "systemctl start docker" in result


def test_generate_cloud_init_creates_ready_marker() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "touch /var/run/mngr-ready" in result


def test_generate_cloud_init_deletes_existing_keys() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "ssh_deletekeys: true" in result


def test_generate_cloud_init_omits_gvisor_install_by_default() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "runsc" not in result
    assert "gvisor" not in result


def test_generate_cloud_init_includes_gvisor_install_when_requested() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=True,
    )
    # Installs runsc from gVisor's APT repo and registers it with the daemon.
    assert "apt-get install -y runsc" in result
    assert "gvisor.dev/archive.key" in result
    assert "runsc install" in result
    # Guarded so it is a no-op when runsc is already registered.
    assert "docker info" in result
    # gnupg is needed for `gpg --dearmor` on minimal images that lack it.
    assert "gnupg" in result
