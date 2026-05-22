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
    )
    assert result.startswith("#cloud-config\n")


def test_generate_cloud_init_contains_host_key() -> None:
    private_key = "-----BEGIN OPENSSH PRIVATE KEY-----\ntest-key-content\n-----END OPENSSH PRIVATE KEY-----"
    public_key = "ssh-ed25519 AAAA testkey"

    result = generate_cloud_init_user_data(
        host_private_key=private_key,
        host_public_key=public_key,
    )

    assert "test-key-content" in result
    assert public_key in result


def test_generate_cloud_init_disables_password_auth() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
    )
    assert "ssh_pwauth: false" in result


def test_generate_cloud_init_installs_docker() -> None:
    """Docker comes from the Debian ``docker.io`` package, installed inline by
    cloud-init's package handler. The ``curl get.docker.com | sh`` installer
    script (used in an earlier revision) made provisioning take 60-120s on a
    ``t3.small``; the packaged install takes 5-15s by piggybacking on the
    same apt run as ca-certificates/curl/rsync.
    """
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
    )
    assert "- docker.io" in result
    assert "systemctl enable docker" in result
    assert "systemctl start docker" in result
    # The slow installer-script approach must NOT come back -- it was the
    # root cause of the EC2 lifecycle test hitting the 300s subprocess
    # timeout on the ``mngr create`` flow.
    assert "get.docker.com" not in result


def test_generate_cloud_init_installs_curl() -> None:
    """``curl`` must stay in the cloud-init package list because
    ``_DEPOT_INSTALL_CMD`` in ``instance.py`` shells out to
    ``curl -fsSL https://depot.dev/install-cli.sh | sh`` on the
    cloud-init-provisioned VPS whenever ``builder=DEPOT``. Debian cloud
    images ship ``wget`` but not ``curl`` by default, so dropping it
    here silently regresses the depot build path.
    """
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
    )
    assert "- curl" in result


def test_generate_cloud_init_creates_ready_marker() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
    )
    assert "touch /var/run/mngr-ready" in result


def test_generate_cloud_init_deletes_existing_keys() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
    )
    assert "ssh_deletekeys: true" in result


def test_generate_cloud_init_no_shutdown_by_default() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
    )
    assert "shutdown -P" not in result


def test_generate_cloud_init_with_auto_shutdown_adds_shutdown_command() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        auto_shutdown_minutes=42,
    )
    assert "shutdown -P +42" in result


def test_generate_cloud_init_with_auto_shutdown_appears_in_runcmd() -> None:
    """The shutdown entry must be inside the runcmd block, not loose YAML."""
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        auto_shutdown_minutes=15,
    )
    runcmd_index = result.index("runcmd:")
    shutdown_index = result.index("shutdown -P +15")
    assert runcmd_index < shutdown_index
    # The shutdown line is a list item under runcmd (starts with "  - ").
    line = next(line for line in result.splitlines() if "shutdown -P" in line)
    assert line.lstrip().startswith("- shutdown -P")


def test_generate_cloud_init_uses_sshd_config_dropin_not_restart() -> None:
    """sshd customization must use a config drop-in + reload (SIGHUP), not restart.

    Regression: ``systemctl restart ssh`` during cloud-init kills any
    in-flight SSH connection, and pyinfra's ``read_output_buffers``
    blocks for its full 10s timeout when the channel dies mid-read. That
    timeout used to escape ``_run_shell_command_with_transient_retry``
    and crash host creation on plain-Debian AMIs (the Docker install
    runs long enough that the provisioning poll loop overlaps with the
    sshd restart). The fix writes the MaxSessions/MaxStartups bump as a
    drop-in under ``/etc/ssh/sshd_config.d/`` so sshd starts with the
    right config, plus ``systemctl reload ssh`` (SIGHUP, no connection
    drop) as belt-and-suspenders. ``systemctl restart`` must NOT appear.
    """
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
    )
    assert "/etc/ssh/sshd_config.d/99-mngr.conf" in result
    assert "MaxSessions 100" in result
    assert "MaxStartups 100:30:200" in result
    assert "systemctl reload ssh" in result
    assert "systemctl restart ssh" not in result, (
        "systemctl restart ssh tears down in-flight connections and races the "
        "provisioning poll loop; use systemctl reload (SIGHUP) instead."
    )
