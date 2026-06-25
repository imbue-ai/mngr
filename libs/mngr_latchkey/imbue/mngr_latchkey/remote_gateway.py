"""Provision the latchkey CLI (and its runtime prerequisites) on a remote VPS.

This is the first piece of "run the latchkey gateway *on* the VPS" support.
Where the rest of the package reverse-tunnels a desktop-side gateway into
each agent, this module installs the upstream ``latchkey`` CLI directly on
the agent's outer host (the VPS) so a gateway can eventually be run there.
"""

import secrets
import shlex
import tempfile
import time
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import CredentialStatus
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.encryption_key import LatchkeyEncryptionKeyPermissionError
from imbue.mngr_latchkey.encryption_key import load_or_create_encryption_key
from imbue.mngr_latchkey.services_catalog import ServiceCatalogError
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import load_permissions
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir

# Version of the upstream ``latchkey`` CLI to install on the VPS.
LATCHKEY_VERSION: Final[str] = "2.19.1"

# Port inside the container on which the VPS-resident gateway is reachable (the
# VPS->container reverse tunnel binds it). Deliberately distinct from
# ``AGENT_SIDE_LATCHKEY_PORT``, which the desktop-side gateway's own reverse
# tunnel already binds inside the container: a VPS agent reaches the desktop
# gateway on ``127.0.0.1:AGENT_SIDE_LATCHKEY_PORT`` and the VPS gateway on
# ``127.0.0.1:INNER_PORT`` at the same time, so the two must not collide.
INNER_PORT: Final[int] = AGENT_SIDE_LATCHKEY_PORT + 1

# Port the latchkey gateway binds to on the VPS's loopback (passed as
# ``LATCHKEY_GATEWAY_PORT``). The reverse tunnel forwards the container's
# ``INNER_PORT`` to this port, so the gateway never has to leave VPS loopback.
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

# Filenames (under the remote ``$HOME/.latchkey`` directory) for the detached
# gateway and reverse-tunnel processes: their stdout/stderr logs and the PID
# files their idempotency checks read.
_REMOTE_GATEWAY_LOG_FILENAME: Final[str] = "gateway.log"
_REMOTE_GATEWAY_PID_FILENAME: Final[str] = "gateway.pid"
_REMOTE_TUNNEL_LOG_FILENAME: Final[str] = "tunnel.log"
_REMOTE_TUNNEL_PID_FILENAME: Final[str] = "tunnel.pid"

# Filename of the ad-hoc private key generated on the VPS for the
# outer-host -> container SSH used by the reverse tunnel. Lives under the
# remote ``$HOME/.latchkey`` directory; the matching ``.pub`` sits beside it.
_CONTAINER_TUNNEL_KEY_FILENAME: Final[str] = "container_tunnel_key"

# Docker label key every mngr container carries, valued with the host id. Used
# to locate an agent's container on the VPS by host id. Must match the
# ``com.imbue.mngr.host-id`` label the docker / vps_docker providers stamp on
# each container (kept as a literal here to avoid a dependency on those
# provider packages).
_CONTAINER_HOST_ID_LABEL: Final[str] = "com.imbue.mngr.host-id"


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
            # (And also for well-functioning latchkey itself.)
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


def _ensure_latchkey_installed(host: OuterHostInterface) -> None:
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


def local_credentials_path(latchkey_directory: Path) -> Path:
    """Return the path to the local (desktop-side) encrypted latchkey credential store."""
    return latchkey_directory / _CREDENTIALS_FILENAME


def _services_allowed_for_host(latchkey_directory: Path, host_id: HostId) -> frozenset[str]:
    """Resolve the canonical service names the host's permissions grant access to.

    Reads the per-host permissions file and maps its rule scopes back to
    canonical service names via the bundled catalog. A host with no
    permissions file (the deny-all default) resolves to the empty set, so
    no credentials are shipped to it. Raises :class:`RemoteGatewayError`
    if the permissions file is malformed or the bundled catalog cannot be
    read (a packaging bug).
    """
    permissions_path = permissions_path_for_host(plugin_data_dir(latchkey_directory), host_id)
    if not permissions_path.is_file():
        logger.debug("No permissions file for host {} at {}; shipping no credentials", host_id, permissions_path)
        return frozenset()
    try:
        config = load_permissions(permissions_path)
        return ServicesCatalog().services_for_permissions(config)
    except (LatchkeyStoreError, ServiceCatalogError) as e:
        raise RemoteGatewayError(f"Failed to resolve allowed services for host {host_id}: {e}") from e


def _services_with_stored_credentials(latchkey: Latchkey, service_names: frozenset[str]) -> frozenset[str]:
    """Narrow ``service_names`` to those that actually have credentials stored.

    A service can be granted by a host's permissions yet have no
    credentials in the store (the user never set them up). Asking
    ``latchkey auth re-encrypt`` to bundle such a service would fail, so
    each candidate is probed with ``latchkey services info <service>
    --offline`` and dropped when its credential status is ``MISSING``.
    The offline probe reports stored state without a network round-trip.
    Non-``MISSING`` states (``VALID`` / ``INVALID`` / ``UNKNOWN``) are
    kept: the credentials exist (or their state is indeterminate), so the
    re-encrypt can include them.
    """
    present: set[str] = set()
    for service_name in sorted(service_names):
        status = latchkey.services_info(service_name, is_offline=True).credential_status
        if status is CredentialStatus.MISSING:
            logger.debug("Service {} has no stored credentials; excluding it from the bundle", service_name)
        else:
            present.add(service_name)
    return frozenset(present)


def _remove_remote_credentials(host: OuterHostInterface, remote_path: Path) -> None:
    """Remove the VPS credential store (idempotent) so no stale credentials linger.

    Used when a host ends up with nothing to ship (deny-all, or every
    granted service lacks stored credentials). ``rm -f`` never errors on a
    missing file, so this is a no-op on first provisioning and a cleanup
    on a later sync that revoked the host's last credential.
    """
    result = host.execute_idempotent_command(
        f"rm -f {shlex.quote(str(remote_path))}", timeout_seconds=_REMOTE_COMMAND_TIMEOUT_SECONDS
    )
    if not result.success:
        raise RemoteGatewayError(
            "Failed to clear latchkey credentials on VPS {}: {}".format(
                host.get_name(), result.stderr.strip() or result.stdout.strip()
            )
        )


def sync_credentials(host: OuterHostInterface, latchkey: Latchkey, host_id: HostId) -> None:
    """Ship a host-scoped subset of the local latchkey credentials onto the VPS.

    Rather than copying the full desktop credential store verbatim, this
    resolves the canonical services the host's permissions actually grant
    (via :func:`_services_allowed_for_host`), drops the ones with no
    stored credentials (via :func:`_services_with_stored_credentials`),
    then re-encrypts a copy containing *only* those services' credentials
    with the *same* encryption key
    (:meth:`Latchkey.export_credentials_subset`). The filtered copy is
    written to ``~/.latchkey/credentials.json.enc`` on the VPS. Keeping
    the same key means the VPS gateway's derived password and the agents'
    permissions-override JWTs keep validating; shipping only the granted,
    actually-stored services means a VPS compromise cannot leak
    credentials the agent was never permitted to use.

    When nothing is left to ship, the remote store is removed instead, since
    ``re-encrypt`` requires at least one service.

    Raises :class:`RemoteGatewayError` if resolving the services, the
    re-encrypt, or reading the filtered copy fails.
    """
    granted = _services_allowed_for_host(latchkey.latchkey_directory, host_id)
    service_names = _services_with_stored_credentials(latchkey, granted)
    remote_path = _resolve_remote_latchkey_directory(host) / _CREDENTIALS_FILENAME
    if not service_names:
        with log_span(
            "Clearing latchkey credentials on VPS {} (nothing to ship for host {})", host.get_name(), host_id
        ):
            _remove_remote_credentials(host, remote_path)
        return
    with tempfile.TemporaryDirectory(prefix="mngr-latchkey-creds-") as tmpdir:
        try:
            latchkey.export_credentials_subset(Path(tmpdir), service_names)
        except LatchkeyError as e:
            raise RemoteGatewayError(f"Failed to export filtered latchkey credentials for host {host_id}: {e}") from e
        subset_path = Path(tmpdir) / _CREDENTIALS_FILENAME
        try:
            content = subset_path.read_bytes()
        except OSError as e:
            raise RemoteGatewayError(f"Failed to read filtered latchkey credentials at {subset_path}: {e}") from e
        with log_span(
            "Syncing {} service(s) of latchkey credentials to VPS {} ({})",
            len(service_names),
            host.get_name(),
            remote_path,
        ):
            # ``is_atomic`` writes to a sibling ``.tmp`` then ``mv``s it into place, so
            # the gateway never reads a half-written file mid-sync.
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
        # ``is_atomic`` writes to a sibling ``.tmp`` then ``mv``s it into place, so
        # the gateway never reads a half-written file mid-sync. (``write_text_file``
        # has no atomic mode, hence the explicit ``write_file`` with encoded bytes.)
        host.write_file(remote_path, content.encode("utf-8"), mode=_REMOTE_FILE_MODE, is_atomic=True)


def _pidfile_guarded_launch_script(pid_filename: str, cmdline_marker: str, launch_command: str) -> str:
    """Build an idempotent background launch keyed off a PID file under ``$HOME/.latchkey``.

    Skips the launch when the PID recorded in ``$HOME/.latchkey/<pid_filename>``
    is still alive *and* its ``/proc/<pid>/cmdline`` contains ``cmdline_marker``
    (the marker check guards a stale PID file whose number got reused).
    Otherwise it runs ``launch_command`` in the background and records the new
    PID via ``$!``.

    A PID file is used deliberately instead of ``pgrep -f``: this script runs as
    ``sh -c '<the whole script>'`` on the VPS, whose own argv therefore contains
    ``launch_command``, so a ``pgrep -f`` for the process would match the shell
    running this very script and wrongly conclude the process is already up.
    Inspecting one specific PID cannot self-match, and ``kill -0`` / ``/proc``
    need no ``procps``.
    """
    return "\n".join(
        (
            "set -e",
            'mkdir -p "$HOME/.latchkey"',
            f'_pidfile="$HOME/.latchkey/{pid_filename}"',
            'if [ -f "$_pidfile" ] && _pid="$(cat "$_pidfile" 2>/dev/null)" && [ -n "$_pid" ] && '
            f'kill -0 "$_pid" 2>/dev/null && grep -qaF {shlex.quote(cmdline_marker)} "/proc/$_pid/cmdline" 2>/dev/null; then',
            "  exit 0",
            "fi",
            f"{launch_command} &",
            'echo $! > "$_pidfile"',
            "exit 0",
        )
    )


def _build_gateway_start_script(outer_port: int, key_file_path: Path, password_file_path: Path) -> str:
    """Build a script that starts a detached ``latchkey gateway`` unless one is already running.

    Launches the gateway under ``nohup`` with stdio detached so it outlives the
    SSH session that started it, logging to ``$HOME/.latchkey/gateway.log``. The
    gateway binds ``outer_port`` on the VPS loopback only -- it is reached from
    the container via the reverse tunnel, never exposed off-host. Idempotency is
    via a PID file (see :func:`_pidfile_guarded_launch_script`).

    The encryption key and the gateway listen password are *not* interpolated
    into the command. Instead the caller writes them to the two 0600 temp files
    at ``key_file_path`` / ``password_file_path``, and this script reads them
    into ``LATCHKEY_ENCRYPTION_KEY`` / ``LATCHKEY_GATEWAY_LISTEN_PASSWORD`` and
    then deletes the files immediately. Only the *paths* appear in the command
    string, so the secrets never reach a process listing (``/proc/<pid>/cmdline``)
    nor any command log, and they do not linger on the VPS disk. The reads are
    synchronous in this (foreground) shell, so the values are captured into the
    environment *before* the gateway is backgrounded: the gateway inherits them
    at exec time and so never needs the files (deleting them right after the
    read is therefore race-free).

    ``LATCHKEY_ENCRYPTION_KEY`` must be the same key the desktop gateway uses so
    the gateway can decrypt the synced ``credentials.json.enc``.
    ``LATCHKEY_GATEWAY_LISTEN_PASSWORD`` is the desktop-derived shared password
    (the value :meth:`Latchkey.derive_gateway_password` produces -- a pure
    function of the shared encryption key) the agents already present as
    ``LATCHKEY_GATEWAY_PASSWORD``.

    ``LATCHKEY_DISABLE_CREDENTIALS_REFRESH=1`` is set so the VPS gateway never
    refreshes OAuth credentials. The credentials here are a synced *copy* of the
    desktop's store (see :func:`sync_credentials`); the desktop-side latchkey is
    the single owner of credential refresh.
    """
    key_q = shlex.quote(str(key_file_path))
    password_q = shlex.quote(str(password_file_path))
    # Detach from the SSH session: nohup + closed stdin + redirected stdio so
    # the channel can close while the gateway keeps running. The two secret env
    # vars are exported into this shell above, so the backgrounded gateway
    # inherits them; only the non-secret config is interpolated here.
    launch_command = (
        f"LATCHKEY_GATEWAY_PORT={outer_port} LATCHKEY_GATEWAY_LISTEN_HOST=127.0.0.1 "
        f"LATCHKEY_DISABLE_COUNTING=1 LATCHKEY_DISABLE_CREDENTIALS_REFRESH=1 "
        f"nohup latchkey gateway "
        f'</dev/null >"$HOME/.latchkey/{_REMOTE_GATEWAY_LOG_FILENAME}" 2>&1'
    )
    guarded = _pidfile_guarded_launch_script(
        pid_filename=_REMOTE_GATEWAY_PID_FILENAME,
        cmdline_marker="gateway",
        launch_command=launch_command,
    )
    return "\n".join(
        (
            "set -e",
            # Safety net: drop the secret files on any early exit (e.g. a failed
            # read aborts under ``set -e``) before the explicit deletion runs.
            f"trap 'rm -f {key_q} {password_q}' EXIT",
            # Read both secrets from their 0600 temp files into this shell's
            # environment (synchronously, before any backgrounding).
            f'LATCHKEY_ENCRYPTION_KEY="$(cat {key_q})"',
            f'LATCHKEY_GATEWAY_LISTEN_PASSWORD="$(cat {password_q})"',
            "export LATCHKEY_ENCRYPTION_KEY LATCHKEY_GATEWAY_LISTEN_PASSWORD",
            # The values now live in the environment (which the gateway inherits
            # at launch), so delete the files immediately and drop the now-
            # redundant trap.
            f"rm -f {key_q} {password_q}",
            "trap - EXIT",
            guarded,
        )
    )


def _ensure_latchkey_gateway_running(
    host: OuterHostInterface, latchkey_directory: Path, gateway_password: str
) -> None:
    """Start ``latchkey gateway`` on the VPS unless it is already running.

    Launches ``LATCHKEY_GATEWAY_PORT=<OUTER_PORT> latchkey gateway`` bound to the
    VPS loopback, detached so it survives the SSH session. The local latchkey
    encryption key (from ``<latchkey_directory>/encryption_key``) and
    ``gateway_password`` (the desktop-derived shared password) are written to
    two short-lived 0600 files under the remote ``$HOME/.latchkey`` directory;
    the start script reads them into ``LATCHKEY_ENCRYPTION_KEY`` /
    ``LATCHKEY_GATEWAY_LISTEN_PASSWORD`` and deletes them immediately, so the
    secrets never appear in a command string (and thus never in a process
    listing or a log) and never persist on the VPS disk -- which matters because
    the encrypted ``credentials.json.enc`` already lives there, so a persistent
    key file beside it would be equivalent to storing the credentials in
    plaintext. Idempotent: a no-op when a gateway process is already present.
    Raises :class:`RemoteGatewayError` if loading the key or the launch fails.
    """
    try:
        encryption_key = load_or_create_encryption_key(latchkey_directory).get_secret_value()
    except LatchkeyEncryptionKeyPermissionError as e:
        raise RemoteGatewayError(str(e)) from e
    remote_dir = _resolve_remote_latchkey_directory(host)
    # Random, non-descriptive basenames per launch: they avoid collisions
    # between concurrent provisions, keep the names unpredictable, and -- unlike
    # descriptive names -- do not advertise which secret each file holds to
    # anyone who can merely list the directory. The start script is handed the
    # exact paths, so the names need not be meaningful; the files are 0600, read
    # into the gateway's environment, and deleted immediately.
    key_file_path = remote_dir / f"{secrets.token_hex(16)}.tmp"
    password_file_path = remote_dir / f"{secrets.token_hex(16)}.tmp"
    host.write_file(key_file_path, encryption_key.encode("utf-8"), mode=_REMOTE_FILE_MODE)
    host.write_file(password_file_path, gateway_password.encode("utf-8"), mode=_REMOTE_FILE_MODE)
    script = _build_gateway_start_script(OUTER_PORT, key_file_path, password_file_path)
    host_name = host.get_name()
    with log_span("Ensuring latchkey gateway is running on VPS {} (port {})", host_name, OUTER_PORT):
        result = host.execute_idempotent_command(script, timeout_seconds=_REMOTE_COMMAND_TIMEOUT_SECONDS)
    if not result.success:
        raise RemoteGatewayError(
            "Failed to start latchkey gateway on VPS {}: {}".format(
                host_name, result.stderr.strip() or result.stdout.strip()
            )
        )


def _build_reverse_tunnel_script(
    container_ssh_user: str,
    container_ssh_port: int,
    container_ssh_key_path: Path,
    inner_port: int,
    outer_port: int,
) -> str:
    """Build a command that opens a reverse SSH tunnel from the VPS into the container.

    Run on the VPS, it SSHes into the container (reachable at
    ``127.0.0.1:<container_ssh_port>`` via the published sshd) and binds the
    container's ``127.0.0.1:<inner_port>``, forwarding it back to the VPS's
    ``127.0.0.1:<outer_port>`` where the gateway listens. The agent's
    ``LATCHKEY_GATEWAY=http://127.0.0.1:<inner_port>`` therefore reaches the
    VPS-resident gateway unchanged.

    Skips when a matching tunnel is already running (PID-file guarded, see
    :func:`_pidfile_guarded_launch_script`). The tunnel is detached via ``nohup``
    (not ``ssh -f``, whose self-backgrounding fork would leave us no stable PID
    to track), logging to ``$HOME/.latchkey/tunnel.log``; reconnect/lifecycle
    handling is intentionally out of scope. Host-key verification is disabled
    because the target is our own freshly created container reached over VPS
    loopback (a hardened version would pin the container host key).
    """
    forward_spec = f"127.0.0.1:{inner_port}:127.0.0.1:{outer_port}"
    launch_command = (
        "nohup ssh -N "
        "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
        "-o ExitOnForwardFailure=yes -o ServerAliveInterval=30 "
        f"-i {shlex.quote(str(container_ssh_key_path))} -p {container_ssh_port} "
        f"-R {forward_spec} {shlex.quote(container_ssh_user)}@127.0.0.1 "
        f'</dev/null >"$HOME/.latchkey/{_REMOTE_TUNNEL_LOG_FILENAME}" 2>&1'
    )
    return _pidfile_guarded_launch_script(
        pid_filename=_REMOTE_TUNNEL_PID_FILENAME,
        cmdline_marker=forward_spec,
        launch_command=launch_command,
    )


def _ensure_latchkey_gateway_reachable_from_container(
    host: OuterHostInterface,
    container_ssh_user: str,
    container_ssh_port: int,
    container_ssh_key_path: Path,
) -> None:
    """Open a reverse SSH tunnel from the VPS into the container so the agent can reach the gateway.

    Binds the container's ``127.0.0.1:INNER_PORT`` and forwards it to the VPS's
    ``127.0.0.1:OUTER_PORT`` (where :func:`_ensure_latchkey_gateway_running`
    started the gateway), so the agent's ``LATCHKEY_GATEWAY=http://127.0.0.1:INNER_PORT``
    reaches the VPS-resident gateway with no change to how the agent env is
    injected.

    ``container_ssh_key_path`` must be a private key present *on the VPS* that
    authenticates to the container's sshd. Idempotent on a best-effort basis
    (skips when a matching tunnel is already running); reconnect/lifecycle
    handling is out of scope. Raises :class:`RemoteGatewayError` if the tunnel
    cannot be established.
    """
    script = _build_reverse_tunnel_script(
        container_ssh_user=container_ssh_user,
        container_ssh_port=container_ssh_port,
        container_ssh_key_path=container_ssh_key_path,
        inner_port=INNER_PORT,
        outer_port=OUTER_PORT,
    )
    host_name = host.get_name()
    with log_span(
        "Ensuring latchkey gateway is reachable from the container on VPS {} (container:{} -> gateway:{})",
        host_name,
        INNER_PORT,
        OUTER_PORT,
    ):
        result = host.execute_idempotent_command(script, timeout_seconds=_REMOTE_COMMAND_TIMEOUT_SECONDS)
    if not result.success:
        raise RemoteGatewayError(
            "Failed to open latchkey reverse tunnel into the container on VPS {}: {}".format(
                host_name, result.stderr.strip() or result.stdout.strip()
            )
        )


def _build_container_tunnel_keypair_script(
    key_path: Path,
    container_name: str,
    container_ssh_user: str,
) -> str:
    """Build a script that mints an ad-hoc VPS->container keypair and authorizes it in the container.

    Generates an ed25519 keypair on the VPS at ``key_path`` (once; reused on
    later calls) and appends its public key to the container ssh user's
    ``authorized_keys`` via ``docker exec`` -- the VPS owns the docker daemon,
    so no pre-existing SSH access to the container is needed to install it. The
    public key is handed to the container through the ``TUNNEL_PUBKEY`` env var
    so it never has to be spliced into the inner shell command. Idempotent: the
    key is only generated when absent and the authorized_keys append is guarded
    by a fixed-string match.
    """
    key = shlex.quote(str(key_path))
    pub = shlex.quote(f"{key_path}.pub")
    # Runs inside the container as the ssh user; reads the public key from the
    # docker-injected TUNNEL_PUBKEY env var and appends it to authorized_keys.
    authorize = (
        'mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh" && '
        'touch "$HOME/.ssh/authorized_keys" && chmod 600 "$HOME/.ssh/authorized_keys" && '
        '{ grep -qxF "$TUNNEL_PUBKEY" "$HOME/.ssh/authorized_keys" || '
        'echo "$TUNNEL_PUBKEY" >> "$HOME/.ssh/authorized_keys"; }'
    )
    return "\n".join(
        (
            "set -e",
            f"mkdir -p {shlex.quote(str(key_path.parent))}",
            # Generate the keypair once; reuse it on subsequent calls.
            f"if [ ! -f {key} ]; then",
            f"  ssh-keygen -t ed25519 -N '' -q -f {key}",
            "fi",
            f'TUNNEL_PUBKEY="$(cat {pub})"',
            f'docker exec -u {shlex.quote(container_ssh_user)} -e TUNNEL_PUBKEY="$TUNNEL_PUBKEY" '
            f"{shlex.quote(container_name)} sh -c {shlex.quote(authorize)}",
        )
    )


def _ensure_container_tunnel_keypair(
    host: OuterHostInterface,
    container_name: str,
    container_ssh_user: str,
) -> Path:
    """Create an ad-hoc outer-host -> container SSH keypair and authorize it in the container.

    Generates an ed25519 keypair on the VPS (under ``$HOME/.latchkey/``) and
    installs its public key into the container ssh user's ``authorized_keys``
    via ``docker exec``. Returns the path to the private key on the VPS,
    suitable for passing as ``container_ssh_key_path`` to
    :func:`_ensure_latchkey_gateway_reachable_from_container`.

    Idempotent: the keypair is generated only when absent and re-authorizing is
    a no-op. Raises :class:`RemoteGatewayError` if key generation or
    authorization fails.
    """
    key_path = _resolve_remote_latchkey_directory(host) / _CONTAINER_TUNNEL_KEY_FILENAME
    script = _build_container_tunnel_keypair_script(
        key_path=key_path,
        container_name=container_name,
        container_ssh_user=container_ssh_user,
    )
    host_name = host.get_name()
    with log_span(
        "Provisioning ad-hoc tunnel keypair for container {} on VPS {}",
        container_name,
        host_name,
    ):
        result = host.execute_idempotent_command(script, timeout_seconds=_REMOTE_COMMAND_TIMEOUT_SECONDS)
    if not result.success:
        raise RemoteGatewayError(
            "Failed to provision tunnel keypair for container {} on VPS {}: {}".format(
                container_name, host_name, result.stderr.strip() or result.stdout.strip()
            )
        )
    return key_path


def _resolve_container_name_for_host(host: OuterHostInterface, host_id: HostId) -> str:
    """Return the docker container name on the VPS for the given mngr host id.

    Looks the container up by the ``com.imbue.mngr.host-id`` label every mngr
    container carries. Raises :class:`RemoteGatewayError` if the lookup fails or
    no matching container is found.
    """
    filter_arg = shlex.quote(f"label={_CONTAINER_HOST_ID_LABEL}={host_id}")
    command = f"docker ps -a --filter {filter_arg} --format '{{{{.Names}}}}'"
    result = host.execute_idempotent_command(command, timeout_seconds=_REMOTE_COMMAND_TIMEOUT_SECONDS)
    if not result.success:
        raise RemoteGatewayError(
            "Failed to locate container for host {} on VPS {}: {}".format(
                host_id, host.get_name(), result.stderr.strip() or result.stdout.strip()
            )
        )
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not names:
        raise RemoteGatewayError(
            f"No container labeled {_CONTAINER_HOST_ID_LABEL}={host_id} found on VPS {host.get_name()}"
        )
    return names[0]


def provision_remote_gateway(
    host: OuterHostInterface,
    host_id: HostId,
    container_ssh_user: str,
    container_ssh_port: int,
    latchkey_directory: Path,
    gateway_password: str,
) -> None:
    """Stand up a VPS-resident latchkey gateway and tunnel it into the agent's container.

    Runs the full remote-gateway sequence on the agent's outer host (the VPS):
    install the latchkey CLI, start the gateway on the VPS loopback (with the
    local encryption key from ``latchkey_directory`` so it can decrypt synced
    credentials, and ``gateway_password`` -- the desktop-derived shared password
    -- so it accepts the same agent traffic the local gateway does), mint an
    ad-hoc VPS->container keypair, and reverse-tunnel the gateway into the
    container so the agent's
    ``LATCHKEY_GATEWAY=http://127.0.0.1:INNER_PORT`` reaches it. The container's
    ssh user/port come from the inner host's SSH info; the container itself is
    located on the VPS by its host-id label.

    Only genuinely-remote outer hosts are provisioned: when ``host`` is the
    local machine (e.g. the outer of a local docker daemon) this is a no-op, so
    we never apt/npm-install latchkey or run a gateway on the user's own
    computer. Raises :class:`RemoteGatewayError` if any step fails.
    """
    if host.is_local:
        logger.debug(
            "Skipping remote latchkey gateway provisioning: outer host {} is local, not a remote VPS",
            host.get_name(),
        )
        return
    _ensure_latchkey_installed(host)
    _ensure_latchkey_gateway_running(host, latchkey_directory, gateway_password)
    container_name = _resolve_container_name_for_host(host, host_id)
    container_ssh_key_path = _ensure_container_tunnel_keypair(
        host, container_name=container_name, container_ssh_user=container_ssh_user
    )
    _ensure_latchkey_gateway_reachable_from_container(
        host,
        container_ssh_user=container_ssh_user,
        container_ssh_port=container_ssh_port,
        container_ssh_key_path=container_ssh_key_path,
    )
