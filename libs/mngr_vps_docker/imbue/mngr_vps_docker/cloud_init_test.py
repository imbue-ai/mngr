"""Tests for cloud-init user_data generation."""

from imbue.mngr_vps_docker.cloud_init import _indent
from imbue.mngr_vps_docker.cloud_init import generate_cloud_init_user_data
from imbue.mngr_vps_docker.host_setup import PINNED_DOCKER_VERSION
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
    # Repo + apt version are derived per-distro from /etc/os-release at run time.
    assert "download.docker.com/linux/${ID}" in result
    assert PINNED_DOCKER_VERSION in result
    assert 'docker-ce="${DOCKER_APT_VERSION}"' in result
    assert "--allow-downgrades" in result
    assert "systemctl enable docker" in result
    assert "systemctl start docker" in result
    # The slow installer-script approach must NOT come back -- it was the
    # root cause of the EC2 lifecycle test hitting the 300s subprocess
    # timeout on the ``mngr create`` flow.
    assert "get.docker.com" not in result


def test_generate_cloud_init_forwards_ssh_key_to_root() -> None:
    """Regression: AMIs whose cloud image installs the provider SSH key on the
    default user (admin / ec2-user / ubuntu / etc.) instead of root would make
    mngr's root-targeted SSH hang (we connect as root per ``ssh_user="root"``
    in ``mngr_vps_docker.instance._make_outer_for_vps_ip``). cloud-init's
    runcmd copies the default user's authorized_keys into ``/root/.ssh``
    before mngr's provisioning poll loop runs, so root SSH always works.

    Vultr / OVH already install the key on root directly so this is a no-op
    there -- but emitting the shell on every provider is cheaper than
    branching in Python by provider.
    """
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "/root/.ssh/authorized_keys" in result
    assert "admin" in result
    assert "ec2-user" in result
    assert "ubuntu" in result


def test_generate_cloud_init_omits_direct_root_key_by_default() -> None:
    """Without ``authorized_user_public_key`` the direct-root-inject line is absent."""
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "printf '%s\\n'" not in result


def test_generate_cloud_init_injects_authorized_user_public_key_into_root() -> None:
    """With ``authorized_user_public_key`` set, the key is written straight into root.

    Removes the dependency on a cloud image's default-user copy landing in root
    -- notably on GCE, where the guest agent provisions the key asynchronously
    and races the runcmd copy. The key is shell-quoted so its embedded space /
    comment survive.
    """
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA host",
        install_gvisor_runtime=False,
        authorized_user_public_key="ssh-ed25519 AAAAaccess user@laptop",
    )
    assert "'ssh-ed25519 AAAAaccess user@laptop'" in result
    assert ">> /root/.ssh/authorized_keys" in result


def test_generate_cloud_init_disables_root_lockout() -> None:
    """Cloud-init defaults to ``disable_root: true``, which prefixes root's
    authorized_keys with a ``no-port-forwarding,no-X11-forwarding,no-agent-
    forwarding,no-pty,command="echo 'Please login as the user ...'"``
    wrapper. mngr_vps_docker SSHes in as root and runs shell-y pyinfra
    commands via that account, so the wrapper would silently break every
    poll. The generated cloud-init must set ``disable_root: false`` so the
    forwarded key lands without the wrapper.
    """
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "disable_root: false" in result


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


def test_generate_cloud_init_no_shutdown_by_default() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "shutdown -P" not in result


def test_generate_cloud_init_with_auto_shutdown_adds_shutdown_command() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
        auto_shutdown_minutes=42,
    )
    assert "shutdown -P +42" in result


def test_generate_cloud_init_with_auto_shutdown_appears_in_runcmd() -> None:
    """The shutdown entry must be inside the runcmd block, not loose YAML."""
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
        auto_shutdown_minutes=15,
    )
    runcmd_index = result.index("runcmd:")
    shutdown_index = result.index("shutdown -P +15")
    assert runcmd_index < shutdown_index
    # The shutdown line is a list item under runcmd (starts with "  - ").
    line = next(line for line in result.splitlines() if "shutdown -P" in line)
    assert line.lstrip().startswith("- shutdown -P")


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
