"""Provision the latchkey CLI (and its runtime prerequisites) on a remote VPS.

This is the first piece of "run the latchkey gateway *on* the VPS" support.
Where the rest of the package reverse-tunnels a desktop-side gateway into
each agent, this module installs the upstream ``latchkey`` CLI directly on
the agent's outer host (the VPS) so a gateway can eventually be run there.
"""

import time
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir

# Version of the upstream ``latchkey`` CLI to install on the VPS. Pinned so
# every remote gateway runs a known-good release rather than whatever
# ``npm install -g latchkey`` happens to resolve to at install time.
LATCHKEY_VERSION: Final[str] = "2.15.1"

# Port the latchkey gateway binds to on the VPS, passed as ``LATCHKEY_GATEWAY_PORT``.
# This is the port reached from *inside* the agent container (the in-container
# ``LATCHKEY_GATEWAY`` env var points here), matching the upstream ``latchkey
# gateway`` default of 1989.
INNER_PORT: Final[int] = 1989

# Port exposed on the *outer* host (the VPS) that bridges to the inner gateway
# port. Reserved for the container/tunnel wiring that connects an agent's
# loopback to the VPS-resident gateway; currently the same value, since no
# remapping is needed yet.
OUTER_PORT: Final[int] = 1989

# Major Node.js version installed via NodeSource. The latchkey CLI is an npm
# package, so it needs a reasonably recent Node runtime; the Debian-shipped
# nodejs is too old, hence the NodeSource setup script.
_NODE_MAJOR_VERSION: Final[str] = "24"

# Generous wall-clock ceiling: ``apt-get update`` + a NodeSource install +
# ``npm install -g`` on a cold VPS routinely runs into the low minutes.
_INSTALL_TIMEOUT_SECONDS: Final[float] = 300.0

# If the install round-trip exceeds this, something is degrading (slow apt
# mirror, slow npm registry) even though it eventually succeeded; warn so we
# notice before it turns into an outright timeout.
_SLOW_INSTALL_WARNING_THRESHOLD_SECONDS: Final[float] = 90.0

# Filename of the upstream latchkey CLI's encrypted credential store, sitting
# directly under the local ``latchkey_directory`` (the LATCHKEY_DIRECTORY the
# desktop-side latchkey uses), e.g. ``~/.minds-staging/latchkey/credentials.json.enc``.
_CREDENTIALS_FILENAME: Final[str] = "credentials.json.enc"

# Name of the latchkey directory on the VPS, under the remote user's home. The
# remote latchkey CLI runs as that user, so ``$HOME/.latchkey`` is the
# LATCHKEY_DIRECTORY it reads its credentials and permissions from.
_REMOTE_LATCHKEY_DIR_NAME: Final[str] = ".latchkey"

# Filename the remote latchkey gateway reads its permissions config from. The
# local per-host file is named ``latchkey_permissions.json``; on the VPS it
# becomes the gateway's single ``permissions.json``.
_REMOTE_PERMISSIONS_FILENAME: Final[str] = "permissions.json"

# Quick remote command (e.g. resolving ``$HOME``); a few seconds of slack
# covers a cold SSH channel without masking a hung connection.
_REMOTE_COMMAND_TIMEOUT_SECONDS: Final[float] = 15.0

# Mode for files we drop on the VPS. Both the encrypted credentials and the
# permissions config are owned by the remote (root) user the gateway runs as;
# 0600 matches the local ``save_permissions`` chmod and keeps secrets private.
_REMOTE_FILE_MODE: Final[str] = "0600"

# Filename the detached gateway's stdout/stderr is redirected to on the VPS,
# under the remote ``$HOME/.latchkey`` directory.
_REMOTE_GATEWAY_LOG_FILENAME: Final[str] = "gateway.log"


class RemoteGatewayError(LatchkeyError, RuntimeError):
    """Raised when provisioning the latchkey CLI on a remote VPS fails."""


def _build_ensure_installed_script(latchkey_version: str, node_major_version: str) -> str:
    """Build an idempotent POSIX-sh script that installs curl, Node.js, and latchkey.

    Each component is gated behind a presence check so a re-run on an
    already-provisioned VPS does no install work. The script avoids
    ``pipefail`` (unsupported by Debian's default ``/bin/sh``, dash) by
    downloading the NodeSource setup script to a file instead of piping it,
    so ``set -e`` still aborts on a failed download.
    """
    nodesource_url = f"https://deb.nodesource.com/setup_{node_major_version}.x"
    return "\n".join(
        (
            "set -e",
            "export DEBIAN_FRONTEND=noninteractive",
            # curl is needed to fetch the NodeSource setup script below.
            "if ! command -v curl >/dev/null 2>&1; then",
            "  apt-get update",
            "  apt-get install -y curl",
            "fi",
            # Node.js + npm via NodeSource (Debian's own nodejs is too old).
            "if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then",
            f"  curl -fsSL {nodesource_url} -o /tmp/nodesource_setup.sh",
            "  bash /tmp/nodesource_setup.sh",
            "  apt-get install -y nodejs",
            "  rm -f /tmp/nodesource_setup.sh",
            "fi",
            # latchkey CLI, pinned to the exact version. Reinstall whenever the
            # installed version differs (missing latchkey reports an empty string).
            f'if [ "$(LATCHKEY_DISABLE_COUNTING=1 latchkey --version 2>/dev/null | sed \'s/^v//\')" != "{latchkey_version}" ]; then',
            f"  npm install -g latchkey@{latchkey_version}",
            "fi",
        )
    )


def ensure_latchkey_installed(host: OuterHostInterface) -> None:
    """Ensure curl, Node.js, and the pinned latchkey CLI are installed on the VPS.

    Idempotent: each component is installed only when missing (or, for
    latchkey, when the installed version differs from :data:`LATCHKEY_VERSION`).
    Raises :class:`RemoteGatewayError` if the install fails.
    """
    script = _build_ensure_installed_script(LATCHKEY_VERSION, _NODE_MAJOR_VERSION)
    host_name = host.get_name()
    with log_span("Ensuring latchkey {} is installed on VPS {}", LATCHKEY_VERSION, host_name):
        started_at = time.monotonic()
        result = host.execute_idempotent_command(script, timeout_seconds=_INSTALL_TIMEOUT_SECONDS)
        elapsed_seconds = time.monotonic() - started_at

    if not result.success:
        raise RemoteGatewayError(
            "Failed to install latchkey {} prerequisites on VPS {}: {}".format(
                LATCHKEY_VERSION, host_name, result.stderr.strip() or result.stdout.strip()
            )
        )
    if elapsed_seconds > _SLOW_INSTALL_WARNING_THRESHOLD_SECONDS:
        logger.warning(
            "Installing latchkey prerequisites on VPS {} took {:.0f}s",
            host_name,
            elapsed_seconds,
        )


def _resolve_remote_latchkey_directory(host: OuterHostInterface) -> Path:
    """Resolve ``$HOME/.latchkey`` on the VPS to an absolute path.

    ``write_file`` transfers over SFTP with a literal path, so ``~`` is not
    expanded for us; we ask the remote shell for ``$HOME`` and build the
    absolute latchkey directory from it.
    """
    result = host.execute_idempotent_command('echo "$HOME"', timeout_seconds=_REMOTE_COMMAND_TIMEOUT_SECONDS)
    home = result.stdout.strip()
    if not result.success or not home:
        raise RemoteGatewayError(
            "Failed to resolve $HOME on VPS {}: {}".format(
                host.get_name(), result.stderr.strip() or result.stdout.strip() or "empty $HOME"
            )
        )
    return Path(home) / _REMOTE_LATCHKEY_DIR_NAME


def _default_permissions_json() -> str:
    """Serialize the deny-all default permissions config (matches ``save_permissions`` output)."""
    config = LatchkeyPermissionsConfig()
    # ``save_permissions`` omits an empty ``schemas`` block; mirror it so the
    # remote file is byte-for-byte the same shape minds writes locally.
    exclude = {"schemas"} if not config.schemas else set()
    return config.model_dump_json(indent=2, exclude=exclude)


def sync_credentials(host: OuterHostInterface, latchkey_directory: Path) -> None:
    """Copy the local encrypted latchkey credentials onto the VPS.

    Reads ``<latchkey_directory>/credentials.json.enc`` from the local
    (desktop-side) latchkey directory and writes it to ``~/.latchkey/`` on the
    VPS so the remote latchkey CLI can decrypt the same credential store.
    Raises :class:`RemoteGatewayError` if the local file is missing or unreadable.
    """
    local_path = latchkey_directory / _CREDENTIALS_FILENAME
    try:
        content = local_path.read_bytes()
    except FileNotFoundError as e:
        raise RemoteGatewayError(f"Local latchkey credentials file does not exist: {local_path}") from e
    except OSError as e:
        raise RemoteGatewayError(f"Failed to read local latchkey credentials file {local_path}: {e}") from e

    remote_path = _resolve_remote_latchkey_directory(host) / _CREDENTIALS_FILENAME
    with log_span("Syncing latchkey credentials to VPS {} ({})", host.get_name(), remote_path):
        host.write_file(remote_path, content, mode=_REMOTE_FILE_MODE, is_atomic=True)


def sync_permissions(host: OuterHostInterface, latchkey_directory: Path, host_id: HostId) -> None:
    """Copy the host's latchkey permissions config onto the VPS.

    Reads the per-host permissions file
    (``<latchkey_directory>/mngr_latchkey/hosts/<host_id>/latchkey_permissions.json``)
    and writes it to ``~/.latchkey/permissions.json`` on the VPS. When the
    local file does not exist, the restrictive deny-all default is written
    instead, so a host with no explicit grants still gets a locked-down gateway.
    Raises :class:`RemoteGatewayError` if the local file exists but is unreadable.
    """
    local_path = permissions_path_for_host(plugin_data_dir(latchkey_directory), host_id)
    if local_path.is_file():
        try:
            content = local_path.read_text()
        except OSError as e:
            raise RemoteGatewayError(f"Failed to read host permissions file {local_path}: {e}") from e
    else:
        logger.debug("No local permissions file for host {} at {}; using the restrictive default", host_id, local_path)
        content = _default_permissions_json()

    remote_path = _resolve_remote_latchkey_directory(host) / _REMOTE_PERMISSIONS_FILENAME
    with log_span("Syncing latchkey permissions for host {} to VPS {} ({})", host_id, host.get_name(), remote_path):
        host.write_text_file(remote_path, content, mode=_REMOTE_FILE_MODE)


def _build_gateway_start_script(inner_port: int) -> str:
    """Build a script that starts a detached ``latchkey gateway`` unless one is already running.

    Exits early (no-op) when a ``latchkey gateway`` process is already present.
    Otherwise launches it under ``nohup`` with stdio detached so it outlives
    the SSH session that started it, logging to ``$HOME/.latchkey/gateway.log``.
    """
    return "\n".join(
        (
            "set -e",
            # Already running: leave it be.
            "if pgrep -f 'latchkey gateway' >/dev/null 2>&1; then",
            "  exit 0",
            "fi",
            'mkdir -p "$HOME/.latchkey"',
            # Detach from the SSH session: nohup + closed stdin + redirected
            # stdio so the channel can close while the gateway keeps running.
            f"LATCHKEY_GATEWAY_PORT={inner_port} LATCHKEY_DISABLE_COUNTING=1 nohup latchkey gateway "
            f'</dev/null >"$HOME/.latchkey/{_REMOTE_GATEWAY_LOG_FILENAME}" 2>&1 &',
            "exit 0",
        )
    )


def ensure_latchkey_gateway_running(host: OuterHostInterface) -> None:
    """Start ``latchkey gateway`` on the VPS unless it is already running.

    Launches ``LATCHKEY_GATEWAY_PORT=<INNER_PORT> latchkey gateway`` detached so
    it survives the SSH session. Idempotent: a no-op when a gateway process is
    already present. Raises :class:`RemoteGatewayError` if the launch command fails.
    """
    script = _build_gateway_start_script(INNER_PORT)
    host_name = host.get_name()
    with log_span("Ensuring latchkey gateway is running on VPS {} (port {})", host_name, INNER_PORT):
        result = host.execute_idempotent_command(script, timeout_seconds=_REMOTE_COMMAND_TIMEOUT_SECONDS)
    if not result.success:
        raise RemoteGatewayError(
            "Failed to start latchkey gateway on VPS {}: {}".format(
                host_name, result.stderr.strip() or result.stdout.strip()
            )
        )
