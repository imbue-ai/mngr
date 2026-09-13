"""Provision the VM-resident ("vm" role) owner-exec daemon on a remote outer host.

Web-only workspaces have no SSH, so the hosted chrome drives everything that
runs *outside* the workspace container (latchkey, VM debugging, key rotation,
...) through this daemon instead. It runs as root directly on the remote outer
(an imbue-cloud slice VM or a VPS), verifies Ed25519 request signatures against
the VM root's ``authorized_keys`` (which the container cannot write), binds the
audience ``vm:<host-id>``, and signs responses with the VM's SSH host key.

The binary is the pinned static release from ``imbue-ai/owner-exec``, fetched
and sha256-verified on the VM (the datalib-curl pattern). It is installed and
kept current by the same discovery-driven provisioning pass that stands up the
latchkey gateway; this is a no-op on a local outer.
"""

import shlex
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import LatchkeyError

# Pinned owner-exec release. MUST stay in lockstep with the default-workspace-template
# inner-install pin (system/scripts/install_owner_exec.sh); see the
# bump-owner-exec skill.
OWNER_EXEC_VERSION: Final[str] = "v0.2.1"
OWNER_EXEC_REPO: Final[str] = "imbue-ai/owner-exec"

# The port the daemon listens on. It binds at the address resolved by
# _resolve_listen_host (the docker bridge gateway, so the workspace container
# reaches it while nothing off-box can); the request signature is the gate
# regardless.
VM_EXEC_PORT: Final[int] = 8794

_INSTALL_PATH: Final[str] = "/usr/local/bin/owner-exec"
_VERSION_STAMP_PATH: Final[str] = "/usr/local/bin/.owner-exec-version"
_CONFIG_PATH: Final[str] = "/etc/owner-exec/config.toml"
_SYSTEMD_UNIT_PATH: Final[str] = "/etc/systemd/system/owner-exec-vm.service"
_HOST_KEY_PATH: Final[str] = "/etc/ssh/ssh_host_ed25519_key"
_VM_AUTHORIZED_KEYS_PATH: Final[str] = "/root/.ssh/authorized_keys"

# The VM has no workspace checkout; anchor relative file paths at root's home.
_VM_REPO_ROOT: Final[str] = "/root"

_INSTALL_TIMEOUT_SECONDS: Final[float] = 180.0
_COMMAND_TIMEOUT_SECONDS: Final[float] = 30.0

# A MemoryMax backstop so even a hypothetical leak is a bounce, not an incident.
_MEMORY_MAX: Final[str] = "256M"


class OwnerExecVmError(LatchkeyError, RuntimeError):
    """Raised when provisioning the VM-resident owner-exec daemon fails."""


def _build_install_script(version: str) -> str:
    """An idempotent, version-gated POSIX-sh installer for the owner-exec binary."""
    base_url = f"https://github.com/{OWNER_EXEC_REPO}/releases/download/{version}"
    return "\n".join(
        (
            "set -eu",
            # Already at the pinned version? Nothing to do.
            f'if [ -x {_INSTALL_PATH} ] && [ "$(cat {_VERSION_STAMP_PATH} 2>/dev/null || true)" = "{version}" ]; then',
            "  exit 0",
            "fi",
            '_arch="$(uname -m)"',
            'case "$_arch" in',
            "  x86_64) _triple=x86_64-unknown-linux ;;",
            "  aarch64|arm64) _triple=aarch64-unknown-linux ;;",
            '  *) echo "unsupported arch $_arch" >&2; exit 1 ;;',
            "esac",
            '_asset="owner-exec-${_triple}"',
            '_tmp="$(mktemp -d)"',
            # --retry-all-errors: curl's --retry alone only covers "transient"
            # failures (timeouts, 5xx), not protocol-level ones like HTTP/2
            # PROTOCOL_ERROR (exit 92), which GitHub's CDN produces
            # intermittently. Retrying on any error is safe here because the
            # sha256 check below guards integrity.
            f'curl -fsSL --retry 3 --retry-delay 2 --retry-all-errors -o "${{_tmp}}/${{_asset}}" "{base_url}/${{_asset}}"',
            f'curl -fsSL --retry 3 --retry-delay 2 --retry-all-errors -o "${{_tmp}}/${{_asset}}.sha256" "{base_url}/${{_asset}}.sha256"',
            '(cd "$_tmp" && sha256sum -c "${_asset}.sha256" >/dev/null)',
            f'install -m 0755 "${{_tmp}}/${{_asset}}" "{_INSTALL_PATH}.new"',
            f'mv -f "{_INSTALL_PATH}.new" {_INSTALL_PATH}',
            f"printf '%s\\n' {shlex.quote(version)} > {_VERSION_STAMP_PATH}",
            'rm -rf "$_tmp"',
        )
    )


# Resolve the docker bridge address on the VM (the container's default gateway,
# e.g. 172.17.0.1). The daemon binds ONLY this address, so it lives on the
# internal docker bridge and is never reachable on the VM's public interface --
# critical on a VPS, where the VM itself has a public IP. It must never fall
# back to a wildcard bind (0.0.0.0 / ::), so an unresolvable bridge address
# fails provisioning outright rather than exposing the daemon publicly.
_RESOLVE_BRIDGE_ADDRESS_SCRIPT: Final[str] = (
    "ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -n1"
)

# Wildcard bind addresses the daemon must never be configured with.
_WILDCARD_LISTEN_HOSTS: Final[frozenset[str]] = frozenset({"", "0.0.0.0", "::", "[::]", "*"})


def _resolve_listen_host(host: OuterHostInterface) -> str:
    """The docker bridge address to bind to. Raises rather than binding a wildcard.

    The container reaches the daemon at exactly this address (its default
    gateway). If it cannot be resolved we fail closed -- binding a wildcard on
    a VPS would put the daemon on the public interface, which the signature
    gate makes survivable but which we categorically do not want.
    """
    result = host.execute_idempotent_command(_RESOLVE_BRIDGE_ADDRESS_SCRIPT, timeout_seconds=_COMMAND_TIMEOUT_SECONDS)
    bridge_address = result.stdout.strip()
    if not result.success or bridge_address in _WILDCARD_LISTEN_HOSTS:
        raise OwnerExecVmError(
            "Could not resolve the docker bridge address on VM {}; refusing to bind owner-exec to a "
            "public/wildcard interface (stderr: {})".format(host.get_name(), result.stderr.strip() or "empty output")
        )
    return bridge_address


def _build_config_toml(host_id: HostId, listen_host: str) -> str:
    """The vm-role daemon config: audience vm:<host-id>, grants off, host-key signing."""
    audience = f"vm:{host_id}"
    return "\n".join(
        (
            'role = "vm"',
            f'audience = "{audience}"',
            f'listen_host = "{listen_host}"',
            f"listen_port = {VM_EXEC_PORT}",
            f'repo_root = "{_VM_REPO_ROOT}"',
            f'authorized_keys_path = "{_VM_AUTHORIZED_KEYS_PATH}"',
            f'host_key_path = "{_HOST_KEY_PATH}"',
            "grants_enabled = false",
            "",
        )
    )


def _build_systemd_unit() -> str:
    """A restart-always unit with a MemoryMax backstop, running the daemon as root."""
    return "\n".join(
        (
            "[Unit]",
            "Description=owner-exec (VM-resident, vm role)",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={_INSTALL_PATH} --config {_CONFIG_PATH}",
            "User=root",
            "Restart=always",
            "RestartSec=2",
            f"MemoryMax={_MEMORY_MAX}",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        )
    )


def provision_owner_exec_vm(host: OuterHostInterface, host_id: HostId) -> None:
    """Install + configure + (re)start the VM-resident owner-exec daemon.

    Idempotent and version-gated. A local outer (e.g. a local docker daemon on
    the user's own machine) is skipped: the vm daemon exists only on genuinely
    remote outers. Raises :class:`OwnerExecVmError` on any failure.
    """
    if host.is_local:
        logger.debug("Skipping owner-exec vm provisioning: outer host {} is local", host.get_name())
        return
    host_name = host.get_name()
    with log_span("Installing owner-exec {} on VM {}", OWNER_EXEC_VERSION, host_name):
        result = host.execute_idempotent_command(
            _build_install_script(OWNER_EXEC_VERSION), timeout_seconds=_INSTALL_TIMEOUT_SECONDS
        )
    if not result.success:
        raise OwnerExecVmError(
            "Failed to install owner-exec {} on VM {}: {}".format(
                OWNER_EXEC_VERSION, host_name, result.stderr.strip() or result.stdout.strip()
            )
        )

    # Config + unit are written atomically; the daemon reads the host key and
    # authorized_keys per request, so a rotation is picked up without a restart.
    listen_host = _resolve_listen_host(host)
    host.write_file(
        Path(_CONFIG_PATH), _build_config_toml(host_id, listen_host).encode("utf-8"), mode="0644", is_atomic=True
    )
    host.write_file(Path(_SYSTEMD_UNIT_PATH), _build_systemd_unit().encode("utf-8"), mode="0644", is_atomic=True)

    with log_span("Starting owner-exec vm daemon on VM {}", host_name):
        start_result = host.execute_idempotent_command(
            "systemctl daemon-reload && systemctl enable --now owner-exec-vm && systemctl restart owner-exec-vm",
            timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
        )
    if not start_result.success:
        raise OwnerExecVmError(
            "Failed to start owner-exec vm daemon on VM {}: {}".format(
                host_name, start_result.stderr.strip() or start_result.stdout.strip()
            )
        )
