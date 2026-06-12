import socket

from imbue.mngr_smolvm.provisioning import allocate_free_tcp_port
from imbue.mngr_smolvm.provisioning import build_shutdown_script
from imbue.mngr_smolvm.provisioning import build_ssh_provisioning_script


def test_ssh_provisioning_script_embeds_keys() -> None:
    script = build_ssh_provisioning_script(
        host_private_key_pem="-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n",
        host_public_key_openssh="ssh-ed25519 AAAAhost host-key",
        client_authorized_public_key="ssh-ed25519 AAAAclient client-key",
    )
    assert "BEGIN PRIVATE KEY" in script
    assert "ssh-ed25519 AAAAhost host-key" in script
    assert "ssh-ed25519 AAAAclient client-key" in script
    # Installs sshd via either package manager and starts it with the
    # dedicated config file.
    assert "apk add" in script
    assert "apt-get install" in script
    assert "/etc/ssh/sshd_config_mngr" in script
    assert "MNGR_PROVISION_OK" in script


def test_ssh_provisioning_script_is_idempotent_about_authorized_keys() -> None:
    script = build_ssh_provisioning_script(
        host_private_key_pem="key",
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
