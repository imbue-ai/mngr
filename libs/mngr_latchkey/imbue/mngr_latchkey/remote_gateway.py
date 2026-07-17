"""Provision the latchkey CLI (and its runtime prerequisites) on a remote VPS.

Where the rest of the package reverse-tunnels a desktop-side gateway into
each agent, this module installs the upstream ``latchkey`` CLI directly on
the agent's outer host (the VPS) and runs a gateway there.

The VPS-resident gateway and the VPS->container reverse SSH tunnel are both
long-running processes that must survive crashes and VM pause/resume. Rather
than spawn them detached (``nohup`` + a PID-file guard), they are registered as
``supervisord`` programs: ``supervisord`` is installed from the distro package
and auto-restarts either process if it dies. The SSH tunnel additionally
carries keepalive flags so a connection wedged by a paused-then-resumed VM is
detected and torn down, letting ``supervisord`` restart it.

Crash recovery deliberately stops short of surviving a full *reboot*: the
gateway's secrets (the encryption key and the derived listen password) are
kept in a tmpfs directory under ``/run``, which is RAM-backed and so survives
process crashes -- letting ``supervisord`` restart the gateway without a
desktop round-trip -- but is wiped by a reboot. This is a deliberate choice to
never persist the encryption key on the VPS disk beside the encrypted
credential store (which would be equivalent to storing the credentials in
plaintext from a disk-snapshot threat model). Provisioning verifies the
directory really is RAM-backed and refuses to proceed otherwise, so the key is
never written to a disk filesystem by mistake. After a reboot the gateway
stays down until the next provisioning pass re-writes the secrets.
"""

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
from imbue.mngr_latchkey.core import GATEWAY_MAX_BODY_SIZE_BYTES
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
LATCHKEY_VERSION: Final[str] = "2.20.2"

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

# Oldest pre-existing Node.js major version accepted on the VPS without
# reinstalling from NodeSource. A VPS image frequently ships a distro node
# that *exists* but is too old to run the pinned latchkey (Debian bookworm
# ships 18.x, and modern npm releases refuse node < 20.17), so the install
# gate must be a version check, not a presence check. Anything below this
# major is replaced with the NodeSource ``_NODE_MAJOR_VERSION`` install.
_MINIMUM_NODE_MAJOR_VERSION: Final[int] = 20

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

# Filenames (under the remote ``$HOME/.latchkey`` directory) for the
# supervisord-managed gateway and reverse-tunnel programs' stdout/stderr logs.
_REMOTE_GATEWAY_LOG_FILENAME: Final[str] = "gateway.log"
_REMOTE_TUNNEL_LOG_FILENAME: Final[str] = "tunnel.log"

# PID files a *pre-supervisord* build wrote under ``$HOME/.latchkey`` when it
# launched the gateway and reverse tunnel detached via ``nohup``. A VPS
# provisioned by such a build still has those processes alive, holding
# ``OUTER_PORT`` and the container's reverse-forward bind, so provisioning tears
# them down (by PID, cmdline-guarded) before starting the supervisord programs
# (see :func:`_migrate_legacy_remote_gateway_state`). The cmdline markers match
# what the old PID-file guard used to verify each process by.
_LEGACY_GATEWAY_PID_FILENAME: Final[str] = "gateway.pid"
_LEGACY_TUNNEL_PID_FILENAME: Final[str] = "tunnel.pid"
_LEGACY_GATEWAY_CMDLINE_MARKER: Final[str] = "gateway"

# Filename (under the remote ``$HOME/.latchkey`` directory) for the gateway's
# supervisord launch wrapper. The wrapper is not secret (it only references the
# secret file *paths*), so it lives on the normal disk.
_GATEWAY_RUN_SCRIPT_FILENAME: Final[str] = "gateway_run.sh"

# tmpfs (RAM-backed) directory holding the gateway's two secrets: the encryption
# key and the derived listen password. ``/run`` is the FHS location for runtime
# state, is root-owned, and is a tmpfs under systemd (which we already require
# for the supervisor service), so it is wiped on reboot -- the key is never
# persisted to the VPS disk beside the encrypted credential store (which would
# be equivalent to storing the credentials in plaintext against a disk-snapshot
# threat model), yet it survives a process crash so supervisord can restart the
# gateway without a desktop round-trip. Provisioning verifies this is really a
# RAM-backed filesystem before writing to it (see
# :func:`_ensure_ram_backed_secrets_dir`). The wrapper reads these 0600 files
# into the environment and execs the gateway, so the secrets never appear in the
# supervisord config or a process listing.
#
# Deliberately not ``/tmp``: unlike ``/run``, ``/tmp`` is not reliably a tmpfs
# (on many distros, incl. common Debian/Ubuntu VPS images, it is a normal
# directory on the root disk and is cleaned by age rather than wiped on boot),
# so the key could land on -- and survive on -- persistent disk. ``/tmp`` is
# also world-writable (1777), exposing the classic hostile-symlink attack that a
# root-owned ``/run`` avoids.
_TMPFS_SECRETS_DIR: Final[Path] = Path("/run/mngr-latchkey")

# Filesystem types (as reported by ``stat -f -c %T``) we accept as RAM-backed
# for the secrets directory. Anything else means the key would land on disk.
_RAM_BACKED_FILESYSTEM_TYPES: Final[frozenset[str]] = frozenset({"tmpfs", "ramfs"})
_GATEWAY_ENCRYPTION_KEY_FILENAME: Final[str] = "gateway_encryption_key"
_GATEWAY_PASSWORD_FILENAME: Final[str] = "gateway_listen_password"

# supervisord drop-in program directory (the distro ``supervisor`` package's
# ``supervisord.conf`` includes ``conf.d/*.conf``) and the program names /
# config filenames for the gateway and the reverse tunnel.
_SUPERVISOR_CONFD_DIR: Final[Path] = Path("/etc/supervisor/conf.d")
_GATEWAY_PROGRAM_NAME: Final[str] = "latchkey-gateway"
_TUNNEL_PROGRAM_NAME: Final[str] = "latchkey-tunnel"
_GATEWAY_CONF_FILENAME: Final[str] = f"{_GATEWAY_PROGRAM_NAME}.conf"
_TUNNEL_CONF_FILENAME: Final[str] = f"{_TUNNEL_PROGRAM_NAME}.conf"

# Absolute paths to the interpreters/binaries named in supervisord ``command=``
# lines. supervisord resolves a program via its *own* PATH (not the program's
# environment), so an absolute path is the robust choice; both are the fixed
# Debian locations.
_SH_BINARY_PATH: Final[str] = "/bin/sh"
_SSH_BINARY_PATH: Final[str] = "/usr/bin/ssh"

# supervisord program tuning. ``startsecs`` is how long a program must stay up
# to count as successfully started. The tunnel uses a huge ``startretries`` so
# supervisord keeps retrying while the container's sshd is still coming up
# (or a stale remote bind clears) instead of giving up. The gateway uses a
# small ``startretries``: the only expected repeated start failure is a reboot
# (its tmpfs secrets are gone), where going quietly FATAL is the desired
# "down until re-provisioned" state rather than an endless retry loop. Logs are
# size-rotated by supervisord itself.
_SUPERVISOR_START_SECONDS: Final[int] = 5
_SUPERVISOR_GATEWAY_START_RETRIES: Final[int] = 3
_SUPERVISOR_TUNNEL_START_RETRIES: Final[int] = 1_000_000
_SUPERVISOR_LOG_MAX_BYTES: Final[str] = "10MB"
_SUPERVISOR_LOG_BACKUPS: Final[int] = 3

# ``supervisorctl reread && update`` starts the freshly-written programs; it can
# take a beat to actually launch them, so allow more than the quick-command
# budget.
_SUPERVISOR_COMMAND_TIMEOUT_SECONDS: Final[float] = 60.0

# SSH keepalive tuning for the reverse tunnel. Without these, a tunnel whose
# far end vanished (e.g. the VM was paused for a week and resumed) can hang
# indefinitely on a dead TCP connection. ``ServerAliveInterval`` /
# ``ServerAliveCountMax`` make ssh probe the peer every N seconds and exit after
# a few unanswered probes, at which point supervisord restarts it; a bounded
# ``ConnectTimeout`` keeps a stalled *initial* connect from wedging the restart.
_SSH_SERVER_ALIVE_INTERVAL_SECONDS: Final[int] = 30
_SSH_SERVER_ALIVE_COUNT_MAX: Final[int] = 3
_SSH_CONNECT_TIMEOUT_SECONDS: Final[int] = 15

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


def _build_ensure_installed_script(
    latchkey_version: str, node_major_version: str, minimum_node_major_version: int
) -> str:
    """Build an idempotent POSIX-sh script that installs curl, Node.js, supervisor, and latchkey.

    Each component is gated behind a presence check -- except Node.js, which is
    gated behind a *version* check: a preinstalled distro node (e.g. Debian
    bookworm's 18.x) exists but cannot run the pinned latchkey/npm, so mere
    presence must not skip the NodeSource install. The script avoids
    ``pipefail`` (unsupported by Debian's default ``/bin/sh``, dash) by
    downloading the NodeSource setup script to a file instead of piping it,
    so ``set -e`` still aborts on a failed download. supervisord is installed
    (and its init service started) so the gateway and reverse tunnel can be run
    as auto-restarting programs that recover from crashes without a desktop
    round-trip.
    """
    nodesource_url = f"https://deb.nodesource.com/setup_{node_major_version}.x"
    # POSIX-sh probe for the installed node's major version; a missing node or
    # unparseable output normalizes to 0, which always triggers the install.
    # The pipeline tolerates a missing node under ``set -e`` because the exit
    # status is the last command's (sed/cut), not node's.
    node_major_probe = "_node_major=\"$(node --version 2>/dev/null | sed 's/^v//' | cut -d. -f1)\""
    node_major_normalize = "case \"$_node_major\" in ''|*[!0-9]*) _node_major=0;; esac"
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
            # Node.js + npm via NodeSource. Version-gated (not presence-gated):
            # a too-old preinstalled node must be replaced, or the npm install
            # below crashes (modern npm refuses node < 20.17).
            node_major_probe,
            node_major_normalize,
            f'if [ "$_node_major" -lt {minimum_node_major_version} ] || ! command -v npm >/dev/null 2>&1; then',
            f"  curl -fsSL {nodesource_url} -o /tmp/nodesource_setup.sh",
            "  bash /tmp/nodesource_setup.sh",
            "  apt-get install -y nodejs",
            "  rm -f /tmp/nodesource_setup.sh",
            "fi",
            # Re-probe after the (possible) install: a stale node earlier on
            # PATH (e.g. a manually installed /usr/local/bin/node) can still
            # shadow the freshly installed /usr/bin/node. Fail with an
            # actionable message now instead of a cryptic npm crash below.
            node_major_probe,
            node_major_normalize,
            f'if [ "$_node_major" -lt {minimum_node_major_version} ]; then',
            '  echo "node at $(command -v node || echo missing) reports version'
            " $(node --version 2>/dev/null || echo none) after the NodeSource install;"
            f" latchkey needs Node.js >= {minimum_node_major_version}."
            ' Remove or upgrade the shadowing installation." >&2',
            "  exit 1",
            "fi",
            # supervisor: supervises the gateway + tunnel and restarts either on
            # crash.
            "if ! command -v supervisord >/dev/null 2>&1; then",
            "  apt-get update",
            "  apt-get install -y supervisor",
            "fi",
            # Ensure supervisord is running now (the distro package starts it on
            # install, but repeating it is idempotent and recovers a host where
            # the service was left stopped). Tolerated on the rare non-systemd
            # host: the reread/update below then fails loudly instead of
            # silently degrading.
            "systemctl enable --now supervisor >/dev/null 2>&1 || true",
            # latchkey CLI, pinned to the exact version. Reinstall whenever the
            # installed version differs (missing latchkey reports an empty string).
            f'if [ "$(LATCHKEY_DISABLE_COUNTING=1 latchkey --version 2>/dev/null | sed \'s/^v//\')" != "{latchkey_version}" ]; then',
            f"  npm install -g latchkey@{latchkey_version}",
            "fi",
        )
    )


def _ensure_latchkey_installed(host: OuterHostInterface) -> None:
    """Ensure curl, Node.js, supervisor, and the pinned latchkey CLI are installed on the VPS.

    Idempotent: each component is installed only when missing (or, for
    latchkey, when the installed version differs from :data:`LATCHKEY_VERSION`).
    Raises :class:`RemoteGatewayError` if the install fails.
    """
    script = _build_ensure_installed_script(LATCHKEY_VERSION, _NODE_MAJOR_VERSION, _MINIMUM_NODE_MAJOR_VERSION)
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


def _build_supervisor_program_config(program_name: str, command: str, log_path: str, start_retries: int) -> str:
    """Build a supervisord ``[program:...]`` drop-in config for a long-running process.

    ``autostart``/``autorestart`` make supervisord launch the program and
    relaunch it whenever it exits, so a crashed process is brought back without
    a desktop round-trip. ``start_retries`` bounds how many times supervisord
    retries a program that keeps failing to *start* before marking it FATAL
    (the tunnel wants a large value while the container's sshd comes up; the
    gateway wants a small one, since a repeated start failure means its tmpfs
    secrets are gone after a reboot and going quietly FATAL is the desired
    state). ``stopasgroup``/``killasgroup`` ensure a stop/restart tears down the
    whole process group (any ssh or child), and supervisord size-rotates the
    combined stdout+stderr into ``log_path``.

    ``command`` must already be shell-quoted by the caller (e.g. via
    ``shlex.quote`` on any token with spaces): supervisord shell-splits it with
    ``shlex.split``, which round-trips ``shlex.quote``'s output. Separately,
    supervisord expands ``%(...)s`` sequences in *every* config value before
    that split, so a literal ``%`` in the command or log path must be doubled to
    ``%%`` or the config fails to parse (``shlex.quote`` does not do this -- it
    treats ``%`` as safe). We double it here so an exotic path can never break
    config loading.
    """
    # supervisord runs %(...)s interpolation on each value; escape literal
    # percent signs so they survive to the shell-split argv unchanged.
    escaped_command = command.replace("%", "%%")
    escaped_log_path = log_path.replace("%", "%%")
    return "\n".join(
        (
            f"[program:{program_name}]",
            f"command={escaped_command}",
            "user=root",
            "autostart=true",
            "autorestart=true",
            f"startsecs={_SUPERVISOR_START_SECONDS}",
            f"startretries={start_retries}",
            "stopasgroup=true",
            "killasgroup=true",
            f"stdout_logfile={escaped_log_path}",
            f"stdout_logfile_maxbytes={_SUPERVISOR_LOG_MAX_BYTES}",
            f"stdout_logfile_backups={_SUPERVISOR_LOG_BACKUPS}",
            "redirect_stderr=true",
            "",
        )
    )


def _reload_supervisor_programs(host: OuterHostInterface, host_name: str, program_name: str) -> None:
    """Apply a freshly-written supervisord drop-in and ensure ``program_name`` is running.

    ``reread`` reloads the config files and ``update`` (re)starts new/changed
    programs and stops removed ones -- idempotent, so an unchanged config never
    needlessly bounces a healthy program. ``update`` does not, however, restart
    a program that is merely STOPPED/FATAL (e.g. a gateway that went FATAL after
    a reboot wiped its tmpfs secrets) when its config is unchanged, so a
    best-effort ``start`` follows: it brings such a program back up with the
    freshly-written secrets, and is a harmless no-op (``|| true``) when the
    program is already running. ``program_name`` is a fixed internal literal, so
    it is interpolated directly. Raises :class:`RemoteGatewayError` if the
    reread/update fails.
    """
    result = host.execute_idempotent_command(
        f"supervisorctl reread && supervisorctl update && (supervisorctl start {program_name} || true)",
        timeout_seconds=_SUPERVISOR_COMMAND_TIMEOUT_SECONDS,
    )
    if not result.success:
        raise RemoteGatewayError(
            "Failed to reload supervisor programs on VPS {}: {}".format(
                host_name, result.stderr.strip() or result.stdout.strip()
            )
        )


def _ensure_ram_backed_secrets_dir(host: OuterHostInterface, host_name: str) -> None:
    """Create the tmpfs secrets directory (0700) and verify it is genuinely RAM-backed.

    The gateway's encryption key must never land on a disk-backed filesystem, so
    this creates :data:`_TMPFS_SECRETS_DIR` and checks (via ``stat -f``) that its
    filesystem type is one of :data:`_RAM_BACKED_FILESYSTEM_TYPES`. If the
    directory is not RAM-backed -- e.g. ``/run`` is unexpectedly not a tmpfs on
    some host -- it refuses to proceed rather than silently persisting the key,
    so the caller never writes the secret to disk. Raises
    :class:`RemoteGatewayError` if creation or the verification fails.
    """
    secrets_dir_q = shlex.quote(str(_TMPFS_SECRETS_DIR))
    # ``$_fstype`` must not be *any* of the accepted RAM-backed types.
    not_ram_backed_condition = " && ".join(
        f'[ "$_fstype" != {shlex.quote(fstype)} ]' for fstype in sorted(_RAM_BACKED_FILESYSTEM_TYPES)
    )
    script = "\n".join(
        (
            "set -e",
            f"mkdir -p {secrets_dir_q}",
            f"chmod 700 {secrets_dir_q}",
            f'_fstype="$(stat -f -c %T {secrets_dir_q})"',
            f"if {not_ram_backed_condition}; then",
            f'  echo "refusing to store the latchkey encryption key: {_TMPFS_SECRETS_DIR} is on a '
            '$_fstype filesystem, not RAM-backed (tmpfs/ramfs)" >&2',
            "  exit 1",
            "fi",
        )
    )
    result = host.execute_idempotent_command(script, timeout_seconds=_REMOTE_COMMAND_TIMEOUT_SECONDS)
    if not result.success:
        raise RemoteGatewayError(
            "Could not prepare a RAM-backed secrets directory ({}) on VPS {}: {}".format(
                _TMPFS_SECRETS_DIR, host_name, result.stderr.strip() or result.stdout.strip()
            )
        )


def _build_gateway_run_script(outer_port: int, key_file_path: Path, password_file_path: Path) -> str:
    """Build the wrapper script supervisord runs to launch ``latchkey gateway``.

    supervisord invokes this as ``/bin/sh <script>``. It reads the encryption
    key and the gateway listen password from their two 0600 tmpfs files into
    ``LATCHKEY_ENCRYPTION_KEY`` / ``LATCHKEY_GATEWAY_LISTEN_PASSWORD`` and
    ``exec``s the gateway (so supervisord tracks the gateway PID directly, not a
    wrapping shell). Reading the secrets from files -- rather than baking them
    into the supervisord ``command=`` line -- keeps them out of the config file
    and out of any process listing (``/proc/<pid>/cmdline``). The gateway binds
    ``outer_port`` on the VPS loopback only; it is reached from the container via
    the reverse tunnel, never exposed off-host.

    The secret files live in tmpfs (see :data:`_TMPFS_SECRETS_DIR`), so a reboot
    wipes them. The script therefore refuses to launch a gateway with missing
    secrets: it exits non-zero with a clear message, which supervisord treats as
    a failed start (the gateway stays down until the next provisioning pass
    re-writes the secrets) rather than running a broken, keyless gateway.

    ``LATCHKEY_ENCRYPTION_KEY`` must be the same key the desktop gateway uses so
    the gateway can decrypt the synced ``credentials.json.enc``.
    ``LATCHKEY_GATEWAY_LISTEN_PASSWORD`` is the desktop-derived shared password
    (the value :meth:`Latchkey.derive_gateway_password` produces -- a pure
    function of the shared encryption key) the agents already present as
    ``LATCHKEY_GATEWAY_PASSWORD``.

    ``LATCHKEY_DISABLE_CREDENTIALS_REFRESH=1`` is set so the VPS gateway never
    refreshes OAuth credentials. The credentials here are a synced *copy* of the
    desktop's store (see :func:`sync_credentials`); the desktop-side latchkey is
    the single owner of credential refresh. ``--max-body-size`` matches the
    limit the desktop-side gateway uses (:data:`GATEWAY_MAX_BODY_SIZE_BYTES`).
    """
    key_q = shlex.quote(str(key_file_path))
    password_q = shlex.quote(str(password_file_path))
    return "\n".join(
        (
            "#!/bin/sh",
            "set -e",
            # supervisord resolves its programs via its own PATH, but this
            # wrapper execs ``latchkey`` (an npm global) itself, so make sure the
            # npm global bin dirs are on PATH regardless of supervisord's.
            'export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"',
            # Refuse to start with missing secrets (tmpfs wiped by a reboot):
            # exit non-zero so supervisord records a failed start instead of
            # launching a keyless gateway.
            f"if [ ! -s {key_q} ] || [ ! -s {password_q} ]; then",
            '  echo "latchkey gateway secrets are missing (tmpfs cleared by a reboot?); awaiting re-provision" >&2',
            "  exit 1",
            "fi",
            # Read both secrets from their 0600 files into the environment; only
            # the file paths (not the secrets) ever appear in this script.
            f'LATCHKEY_ENCRYPTION_KEY="$(cat {key_q})"',
            f'LATCHKEY_GATEWAY_LISTEN_PASSWORD="$(cat {password_q})"',
            "export LATCHKEY_ENCRYPTION_KEY LATCHKEY_GATEWAY_LISTEN_PASSWORD",
            f"export LATCHKEY_GATEWAY_PORT={outer_port}",
            "export LATCHKEY_GATEWAY_LISTEN_HOST=127.0.0.1",
            "export LATCHKEY_DISABLE_COUNTING=1",
            "export LATCHKEY_DISABLE_CREDENTIALS_REFRESH=1",
            f"exec latchkey gateway --max-body-size {GATEWAY_MAX_BODY_SIZE_BYTES}",
            "",
        )
    )


def _ensure_latchkey_gateway_running(
    host: OuterHostInterface, latchkey_directory: Path, gateway_password: str
) -> None:
    """Register (and start) the ``latchkey gateway`` as a supervisord program on the VPS.

    Writes a supervisord drop-in that launches ``latchkey gateway`` bound to the
    VPS loopback on ``OUTER_PORT`` and applies it via ``reread``/``update``, so
    supervisord keeps the gateway running and restarts it if it crashes. The
    local latchkey encryption key (from ``<latchkey_directory>/encryption_key``)
    and ``gateway_password`` (the desktop-derived shared password) are written
    to two 0600 files in a tmpfs directory (:data:`_TMPFS_SECRETS_DIR`); a
    wrapper script (on the normal disk) reads them into the gateway's
    environment at launch, so the secrets never appear in the supervisord config
    or a process listing.

    The secrets go in tmpfs (RAM), not on the disk beside the encrypted
    ``credentials.json.enc``: this deliberately keeps the encryption key off the
    persistent disk (a key file beside the encrypted store would, against a
    disk-snapshot threat model, be equivalent to storing the credentials in
    plaintext) while still surviving a process crash, so supervisord can restart
    the gateway without a desktop round-trip. The tradeoff is that a reboot
    wipes the secrets, leaving the gateway down until the next provisioning pass
    re-writes them -- an accepted limitation. Idempotent. Raises
    :class:`RemoteGatewayError` if loading the key or the reload fails.
    """
    try:
        encryption_key = load_or_create_encryption_key(latchkey_directory).get_secret_value()
    except LatchkeyEncryptionKeyPermissionError as e:
        raise RemoteGatewayError(str(e)) from e
    remote_dir = _resolve_remote_latchkey_directory(host)
    # Secrets go in tmpfs (RAM); the wrapper + log stay on the normal disk.
    key_file_path = _TMPFS_SECRETS_DIR / _GATEWAY_ENCRYPTION_KEY_FILENAME
    password_file_path = _TMPFS_SECRETS_DIR / _GATEWAY_PASSWORD_FILENAME
    run_script_path = remote_dir / _GATEWAY_RUN_SCRIPT_FILENAME
    log_path = remote_dir / _REMOTE_GATEWAY_LOG_FILENAME
    conf_path = _SUPERVISOR_CONFD_DIR / _GATEWAY_CONF_FILENAME
    host_name = host.get_name()

    # Create + verify the RAM-backed secrets dir before writing the key, so we
    # never persist it to a disk filesystem if tmpfs is unexpectedly absent.
    _ensure_ram_backed_secrets_dir(host, host_name)

    # Write the two secrets (0600) into tmpfs and the wrapper that reads them.
    host.write_file(key_file_path, encryption_key.encode("utf-8"), mode=_REMOTE_FILE_MODE)
    host.write_file(password_file_path, gateway_password.encode("utf-8"), mode=_REMOTE_FILE_MODE)
    run_script = _build_gateway_run_script(OUTER_PORT, key_file_path, password_file_path)
    host.write_file(run_script_path, run_script.encode("utf-8"), mode="0700")

    # Write the supervisord program config, then reread/update/start to apply it.
    command = f"{_SH_BINARY_PATH} {shlex.quote(str(run_script_path))}"
    conf = _build_supervisor_program_config(
        _GATEWAY_PROGRAM_NAME, command, str(log_path), _SUPERVISOR_GATEWAY_START_RETRIES
    )
    with log_span("Ensuring latchkey gateway is running on VPS {} (port {})", host_name, OUTER_PORT):
        host.write_file(conf_path, conf.encode("utf-8"), mode=_REMOTE_FILE_MODE, is_atomic=True)
        _reload_supervisor_programs(host, host_name, _GATEWAY_PROGRAM_NAME)


def _build_reverse_tunnel_ssh_command(
    container_ssh_user: str,
    container_ssh_port: int,
    container_ssh_key_path: Path,
    inner_port: int,
    outer_port: int,
) -> str:
    """Build the ``ssh`` command supervisord runs to reverse-tunnel the VPS into the container.

    Run on the VPS, it SSHes into the container (reachable at
    ``127.0.0.1:<container_ssh_port>`` via the published sshd) and binds the
    container's ``127.0.0.1:<inner_port>``, forwarding it back to the VPS's
    ``127.0.0.1:<outer_port>`` where the gateway listens. The agent's
    ``LATCHKEY_GATEWAY=http://127.0.0.1:<inner_port>`` therefore reaches the
    VPS-resident gateway unchanged.

    This runs in the foreground under supervisord (no ``nohup``/``ssh -f``): the
    keepalive flags make ssh exit when the far end is unreachable -- e.g. after
    the VM was paused for a week and resumed, leaving the TCP connection wedged
    -- so supervisord notices the exit and restarts a fresh tunnel.
    ``ExitOnForwardFailure`` makes ssh exit (rather than sit forwarding-less) if
    the remote bind fails, and ``BatchMode``/``ConnectTimeout`` keep a stalled
    connect from wedging the restart. Host-key verification is disabled because
    the target is our own freshly created container reached over VPS loopback (a
    hardened version would pin the container host key). The command is consumed
    by supervisord (which shell-splits it), so the key path and user are
    ``shlex``-quoted.
    """
    forward_spec = f"127.0.0.1:{inner_port}:127.0.0.1:{outer_port}"
    return " ".join(
        (
            _SSH_BINARY_PATH,
            "-N",
            "-T",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "TCPKeepAlive=yes",
            "-o",
            f"ConnectTimeout={_SSH_CONNECT_TIMEOUT_SECONDS}",
            "-o",
            f"ServerAliveInterval={_SSH_SERVER_ALIVE_INTERVAL_SECONDS}",
            "-o",
            f"ServerAliveCountMax={_SSH_SERVER_ALIVE_COUNT_MAX}",
            "-i",
            shlex.quote(str(container_ssh_key_path)),
            "-p",
            str(container_ssh_port),
            "-R",
            forward_spec,
            f"{shlex.quote(container_ssh_user)}@127.0.0.1",
        )
    )


def _ensure_latchkey_gateway_reachable_from_container(
    host: OuterHostInterface,
    container_ssh_user: str,
    container_ssh_port: int,
    container_ssh_key_path: Path,
) -> None:
    """Register (and start) the VPS->container reverse SSH tunnel as a supervisord program.

    Binds the container's ``127.0.0.1:INNER_PORT`` and forwards it to the VPS's
    ``127.0.0.1:OUTER_PORT`` (where :func:`_ensure_latchkey_gateway_running`
    started the gateway), so the agent's ``LATCHKEY_GATEWAY=http://127.0.0.1:INNER_PORT``
    reaches the VPS-resident gateway with no change to how the agent env is
    injected. supervisord keeps the tunnel up and restarts it if ssh exits (e.g.
    after a keepalive timeout on a resumed VM). The tunnel carries no secret, so
    unlike the gateway it also survives a reboot on its own (though it forwards
    to a down gateway until the gateway is re-provisioned).

    ``container_ssh_key_path`` must be a private key present *on the VPS* that
    authenticates to the container's sshd. Idempotent. Raises
    :class:`RemoteGatewayError` if writing the config or the reload fails.
    """
    command = _build_reverse_tunnel_ssh_command(
        container_ssh_user=container_ssh_user,
        container_ssh_port=container_ssh_port,
        container_ssh_key_path=container_ssh_key_path,
        inner_port=INNER_PORT,
        outer_port=OUTER_PORT,
    )
    log_path = _resolve_remote_latchkey_directory(host) / _REMOTE_TUNNEL_LOG_FILENAME
    conf_path = _SUPERVISOR_CONFD_DIR / _TUNNEL_CONF_FILENAME
    conf = _build_supervisor_program_config(
        _TUNNEL_PROGRAM_NAME, command, str(log_path), _SUPERVISOR_TUNNEL_START_RETRIES
    )
    host_name = host.get_name()
    with log_span(
        "Ensuring latchkey gateway is reachable from the container on VPS {} (container:{} -> gateway:{})",
        host_name,
        INNER_PORT,
        OUTER_PORT,
    ):
        host.write_file(conf_path, conf.encode("utf-8"), mode=_REMOTE_FILE_MODE, is_atomic=True)
        _reload_supervisor_programs(host, host_name, _TUNNEL_PROGRAM_NAME)


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


def _build_legacy_pidfile_kill_block(pid_filename: str, cmdline_marker: str) -> tuple[str, ...]:
    """Build sh lines that kill (and forget) a legacy nohup process from its PID file.

    Reads ``$HOME/.latchkey/<pid_filename>`` and, only when that PID is still
    alive *and* its ``/proc/<pid>/cmdline`` contains ``cmdline_marker`` (guarding
    a reused PID -- the same check the old PID-file launcher used), SIGTERMs it,
    gives it a moment, then SIGKILLs to guarantee the held port/forward is freed
    before the supervisord replacement starts. The stale PID file is always
    removed. Every step tolerates a dead/missing process, so this is a no-op on a
    VPS that never ran the old build (or has already been migrated).
    """
    return (
        f'_pidfile="$HOME/.latchkey/{pid_filename}"',
        'if [ -f "$_pidfile" ]; then',
        '  _pid="$(cat "$_pidfile" 2>/dev/null || true)"',
        '  if [ -n "$_pid" ] && kill -0 "$_pid" 2>/dev/null && '
        f'grep -qaF {shlex.quote(cmdline_marker)} "/proc/$_pid/cmdline" 2>/dev/null; then',
        '    kill "$_pid" 2>/dev/null || true',
        "    sleep 1",
        '    kill -9 "$_pid" 2>/dev/null || true',
        "  fi",
        '  rm -f "$_pidfile"',
        "fi",
    )


def _build_legacy_migration_script(forward_spec: str) -> str:
    """Build the sh script that tears down a pre-supervisord (nohup) gateway + tunnel.

    Kills the legacy gateway (holding ``OUTER_PORT``) and reverse tunnel (holding
    the container's ``forward_spec`` bind), each guarded by a cmdline marker, and
    scrubs any encryption key / listen password an intermediate build left on the
    persistent disk under ``$HOME/.latchkey`` (the current gateway keeps its
    secrets in tmpfs only). Idempotent.
    """
    gateway_block = _build_legacy_pidfile_kill_block(_LEGACY_GATEWAY_PID_FILENAME, _LEGACY_GATEWAY_CMDLINE_MARKER)
    # The old tunnel's cmdline carried the exact reverse-forward spec, which is
    # the unambiguous marker for it (unlike a bare "ssh").
    tunnel_block = _build_legacy_pidfile_kill_block(_LEGACY_TUNNEL_PID_FILENAME, forward_spec)
    return "\n".join(
        (
            "set -e",
            *gateway_block,
            *tunnel_block,
            # Scrub any on-disk secrets a pre-tmpfs build persisted here. The
            # basenames are fixed constants (no shell-special chars), so they are
            # double-quoted inline -- ``shlex.quote`` would single-quote and thus
            # stop ``$HOME`` from expanding.
            f'rm -f "$HOME/.latchkey/{_GATEWAY_ENCRYPTION_KEY_FILENAME}" '
            f'"$HOME/.latchkey/{_GATEWAY_PASSWORD_FILENAME}"',
        )
    )


def _migrate_legacy_remote_gateway_state(host: OuterHostInterface) -> None:
    """Tear down any pre-supervisord (nohup + PID-file) gateway/tunnel on the VPS.

    A VPS provisioned by an older build runs the gateway and reverse tunnel as
    detached ``nohup`` processes tracked by
    ``$HOME/.latchkey/{gateway,tunnel}.pid``. Those still hold ``OUTER_PORT`` and
    the container's reverse-forward bind, so the new supervisord programs would
    fail to start (address in use / refused forward) until they are gone. This
    kills each legacy process (cmdline-guarded against PID reuse), drops the
    stale PID files, and scrubs any on-disk secrets an intermediate build left
    behind. Idempotent: a no-op on an already-migrated or freshly-created VPS.
    Raises :class:`RemoteGatewayError` if the cleanup command fails.
    """
    forward_spec = f"127.0.0.1:{INNER_PORT}:127.0.0.1:{OUTER_PORT}"
    script = _build_legacy_migration_script(forward_spec)
    host_name = host.get_name()
    with log_span("Migrating any legacy nohup latchkey gateway/tunnel to supervisord on VPS {}", host_name):
        result = host.execute_idempotent_command(script, timeout_seconds=_REMOTE_COMMAND_TIMEOUT_SECONDS)
    if not result.success:
        raise RemoteGatewayError(
            "Failed to migrate legacy latchkey gateway state on VPS {}: {}".format(
                host_name, result.stderr.strip() or result.stdout.strip()
            )
        )


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
    install the latchkey CLI and supervisord, register the gateway as a
    supervisord program bound to the VPS loopback (with the local encryption key
    from ``latchkey_directory`` so it can decrypt synced credentials, and
    ``gateway_password`` -- the desktop-derived shared password -- so it accepts
    the same agent traffic the local gateway does), mint an ad-hoc
    VPS->container keypair, and register the VPS->container reverse tunnel as a
    second supervisord program so the agent's
    ``LATCHKEY_GATEWAY=http://127.0.0.1:INNER_PORT`` reaches it. supervisord
    keeps both processes running and restarts them on failure. A VPS provisioned
    by an older (nohup + PID-file) build is migrated first: its detached gateway
    and tunnel are killed so they free ``OUTER_PORT`` and the container forward
    bind before the supervisord programs start. (The gateway's
    secrets live in tmpfs, so a reboot leaves the gateway down until the next
    provisioning pass; this is a deliberate choice to keep the encryption key
    off the persistent disk -- see :func:`_ensure_latchkey_gateway_running`.)
    The container's ssh user/port come from the inner host's SSH info; the
    container itself is located on the VPS by its host-id label.

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
    # Tear down any pre-supervisord (nohup + PID-file) gateway/tunnel first: an
    # old build's processes still hold OUTER_PORT and the container's forward
    # bind, which would make the new supervisord programs fail to start.
    _migrate_legacy_remote_gateway_state(host)
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
