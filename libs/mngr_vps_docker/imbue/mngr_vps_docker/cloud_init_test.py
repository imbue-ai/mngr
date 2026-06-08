"""Tests for cloud-init user_data generation."""

from imbue.mngr_vps_docker.cloud_init import _indent
from imbue.mngr_vps_docker.cloud_init import generate_cloud_init_user_data
from imbue.mngr_vps_docker.host_setup import PINNED_DOCKER_APT_VERSION
from imbue.mngr_vps_docker.host_setup import PINNED_GVISOR_RELEASE


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


def test_generate_cloud_init_installs_pinned_docker() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    # Pinned install via the official Docker apt repo (not the unpinned get.docker.com script).
    assert "get.docker.com" not in result
    assert "download.docker.com/linux/debian" in result
    assert f"docker-ce={PINNED_DOCKER_APT_VERSION}" in result
    assert "--allow-downgrades" in result
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
    # Downloads the pinned dated gVisor release and registers it with the daemon.
    assert f"gvisor/releases/release/{PINNED_GVISOR_RELEASE}" in result
    assert "runsc install" in result
    # Guarded so it is a no-op when runsc is already registered.
    assert "docker info" in result
    # gnupg is installed with the base packages (needed for the Docker apt key).
    assert "gnupg" in result
