import socket

from imbue.mngr_smolvm.provisioning import HOST_PRIVATE_KEY_ENV_VAR
from imbue.mngr_smolvm.provisioning import allocate_free_tcp_port
from imbue.mngr_smolvm.provisioning import build_shutdown_script
from imbue.mngr_smolvm.provisioning import build_ssh_provisioning_script


def test_ssh_provisioning_script_embeds_public_keys_only() -> None:
    script = build_ssh_provisioning_script(
        host_public_key_openssh="ssh-ed25519 AAAAhost host-key",
        client_authorized_public_key="ssh-ed25519 AAAAclient client-key",
    )
    assert "ssh-ed25519 AAAAhost host-key" in script
    assert "ssh-ed25519 AAAAclient client-key" in script
    # Installs sshd via either package manager and starts it with the
    # dedicated config file.
    assert "apk add" in script
    assert "apt-get install" in script
    assert "/etc/ssh/sshd_config_mngr" in script
    assert "MNGR_PROVISION_OK" in script


def test_ssh_provisioning_script_install_gate_covers_all_base_tools() -> None:
    """The install gate must trigger when ANY base tool is missing, not just
    sshd: an image can ship sshd while lacking tmux/git/..., and mngr needs
    all of them on a host."""
    script = build_ssh_provisioning_script(
        host_public_key_openssh="pub",
        client_authorized_public_key="client",
    )
    assert "for tool in sshd tmux git rsync jq curl; do" in script
    assert 'if [ "$is_install_needed" = "1" ]; then' in script


def test_ssh_provisioning_script_reads_private_key_from_secret_env_var() -> None:
    """The private key is injected via the --secret-file env var (so it never
    appears in host-side argv); the script must reference the var, refuse to
    run without it, and re-append the trailing newline smolvm strips."""
    script = build_ssh_provisioning_script(
        host_public_key_openssh="ssh-ed25519 AAAAhost host-key",
        client_authorized_public_key="ssh-ed25519 AAAAclient client-key",
    )
    assert "PRIVATE KEY" not in script
    assert f"printf '%s\\n' \"${{{HOST_PRIVATE_KEY_ENV_VAR}}}\" > /etc/ssh/ssh_host_ed25519_key" in script
    assert f'if [ -z "${{{HOST_PRIVATE_KEY_ENV_VAR}:-}}" ]' in script


def test_ssh_provisioning_script_is_idempotent_about_authorized_keys() -> None:
    script = build_ssh_provisioning_script(
        host_public_key_openssh="pub",
        client_authorized_public_key="ssh-ed25519 AAAA client",
    )
    # The authorized key is appended only when missing.
    assert "grep -qF" in script


def test_shutdown_script_touches_sentinel() -> None:
    script = build_shutdown_script("/mngr", "/run/smolvm/poweroff")
    assert script.startswith("#!/bin/sh")
    assert "touch /run/smolvm/poweroff" in script
    assert "/mngr/logs/shutdown.log" in script


def test_allocate_free_tcp_port_returns_bindable_port() -> None:
    port = allocate_free_tcp_port()
    assert 1024 < port <= 65535
    # The port is free immediately after allocation
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))
