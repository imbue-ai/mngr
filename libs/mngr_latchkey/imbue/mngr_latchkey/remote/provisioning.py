"""Provision the latchkey CLI (and its runtime prerequisites) on a remote VPS.

Remote workspaces receive the gateway managed here, while local workspaces
receive the desktop gateway directly. The VPS gateway handles third-party
calls itself and forwards Minds-owned endpoint families back to the desktop.

The VPS-resident gateway and the VPS->container reverse SSH tunnel are both
long-running processes that must survive crashes and VM pause/resume. Rather
than spawn them detached (``nohup`` + a PID-file guard), they are registered as
``supervisord`` programs: ``supervisord`` is installed from the distro package
and auto-restarts either process if it dies. The SSH tunnel additionally
carries keepalive flags so a connection wedged by a paused-then-resumed VM is
detected and torn down, letting ``supervisord`` restart it.

Crash recovery deliberately stops short of surviving a full *reboot*: the
gateway's secrets (the machine's own encryption key and listen password, plus
the pair its forwarding extension presents to the desktop) are
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
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import JsonValue
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import CONFIG_FILENAME
from imbue.mngr_latchkey.core import CREDENTIALS_STORE_FILENAME
from imbue.mngr_latchkey.core import GATEWAY_MAX_BODY_SIZE_BYTES
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.core import PERMISSIONS_CONFIG_FILENAME
from imbue.mngr_latchkey.core import REMOTE_GATEWAY_EXTENSION_FILENAME
from imbue.mngr_latchkey.core import bundled_gateway_extension_content
from imbue.mngr_latchkey.core import custom_service_registration_entries
from imbue.mngr_latchkey.core import merge_minds_latchkey_config
from imbue.mngr_latchkey.core import summarize_latchkey_failure
from imbue.mngr_latchkey.encryption_key import LatchkeyEncryptionKeyPermissionError
from imbue.mngr_latchkey.encryption_key import load_or_create_encryption_key
from imbue.mngr_latchkey.owner_exec_vm import provision_owner_exec_vm
from imbue.mngr_latchkey.remote._machine import DESKTOP_GATEWAY_PASSWORD_FILENAME
from imbue.mngr_latchkey.remote._machine import DESKTOP_PERMISSIONS_OVERRIDE_FILENAME
from imbue.mngr_latchkey.remote._machine import GATEWAY_ENCRYPTION_KEY_FILENAME
from imbue.mngr_latchkey.remote._machine import GATEWAY_LISTEN_PASSWORD_FILENAME
from imbue.mngr_latchkey.remote._machine import REMOTE_COMMAND_TIMEOUT_SECONDS
from imbue.mngr_latchkey.remote._machine import REMOTE_FILE_MODE

# Re-exported (the redundant alias marks it as such): consumers outside this
# plugin read the gateway's logs under this directory.
from imbue.mngr_latchkey.remote._machine import REMOTE_LATCHKEY_DIR_NAME as REMOTE_LATCHKEY_DIR_NAME
from imbue.mngr_latchkey.remote._machine import REMOTE_LATCHKEY_TIMEOUT_SECONDS
from imbue.mngr_latchkey.remote._machine import TMPFS_SECRETS_DIR
from imbue.mngr_latchkey.remote._machine import read_machine_gateway_password_from_secrets_dir
from imbue.mngr_latchkey.remote._machine import read_machine_key_from_secrets_dir

# Re-exported (the redundant alias marks it as such): callers about to run a
# reconcile resolve the machine's ~/.latchkey once and pass it down.
from imbue.mngr_latchkey.remote._machine import resolve_remote_latchkey_directory as resolve_remote_latchkey_directory
from imbue.mngr_latchkey.remote._machine import run_remote_command
from imbue.mngr_latchkey.remote._mirror import generate_machine_encryption_key
from imbue.mngr_latchkey.remote._mirror import store_machine_encryption_key
from imbue.mngr_latchkey.remote._mirror import store_machine_gateway_password
from imbue.mngr_latchkey.remote._mirror import stored_machine_encryption_key
from imbue.mngr_latchkey.remote._mirror import stored_machine_gateway_password
from imbue.mngr_latchkey.remote._transfer import adopt_machine_permissions
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir

# Version of the upstream ``latchkey`` CLI to install on the VPS.
LATCHKEY_VERSION: Final[str] = "3.11.1"

# datalib release the VPS fetches the "dispatch curl" + Chrome-impersonating
# curl from (``curl-<triple>.tar.gz``). The gateway runs the dispatch curl as
# its ``LATCHKEY_CURL`` so a caller that sends the ``X-Imbue-Impersonate``
# marker header gets Chrome TLS impersonation, while every other request
# passes through to the system curl. The statically linked musl build is
# fetched rather than the glibc one, so it runs on any VPS image regardless of
# how old its glibc is. The fetch is fail-loud, like every other component of
# the install: a network failure, a checksum mismatch, or a host arch datalib
# doesn't build fails provisioning. The gateway run script exports
# ``LATCHKEY_CURL`` unconditionally and so depends on that -- pointing latchkey
# at a curl that isn't there would break every request, not just impersonating
# ones.
_DATALIB_REPO: Final[str] = "imbue-ai/datalib"
DATALIB_CURL_VERSION: Final[str] = "v0.26.0"
# Where the two binaries land on the VPS. ``/usr/local/bin`` is already on the
# gateway run script's PATH, and the dispatch curl finds the impersonator as a sibling.
_CURL_IMPERSONATE_INSTALL_DIR: Final[str] = "/usr/local/bin"
_CURL_DISPATCH_BIN: Final[str] = "latchkey-curl-dispatch"
_CURL_IMPERSONATE_BIN: Final[str] = "latchkey-curl-impersonate"
_CURL_DISPATCH_PATH: Final[str] = f"{_CURL_IMPERSONATE_INSTALL_DIR}/{_CURL_DISPATCH_BIN}"
_CURL_IMPERSONATE_PATH: Final[str] = f"{_CURL_IMPERSONATE_INSTALL_DIR}/{_CURL_IMPERSONATE_BIN}"
# Suffix the new binaries are staged under before being renamed over the old
# ones. Overwriting them in place would truncate a file the gateway may be
# executing right then, which fails with ETXTBSY ("Text file busy"); a rename
# within the same directory is atomic and leaves any running process on the
# old inode. Staging both before swapping either also means a failure while
# downloading, verifying, or staging leaves the previous pair untouched.
_CURL_STAGED_SUFFIX: Final[str] = ".new"
# Records which datalib release and target triple the installed pair came from.
# The binaries are installed under fixed, version-less names, so without this
# stamp a presence check would leave an already-provisioned VPS on whatever
# build it first received -- exactly the hosts a bump is meant to reach. A
# dotfile in ``/usr/local/bin`` is never picked up by a PATH lookup.
_CURL_VERSION_STAMP_PATH: Final[str] = f"{_CURL_IMPERSONATE_INSTALL_DIR}/.latchkey-curl-version"

# Port on the VPS loopback where the desktop gateway is reverse-tunneled. The
# VPS-only extension forwards Minds-owned endpoint families here.
DESKTOP_GATEWAY_VPS_PORT: Final[int] = 1988

# Port the latchkey gateway binds to on the VPS's loopback. The VPS->container
# reverse tunnel exposes it at the same fixed agent-side port local workspaces
# use, so every workspace has one gateway URL: http://127.0.0.1:1989.
OUTER_PORT: Final[int] = AGENT_SIDE_LATCHKEY_PORT

# A pre-supervisord build exposed the VPS gateway at container port 1990. Keep
# that value only for identifying and killing its legacy nohup tunnel.
_LEGACY_INNER_PORT: Final[int] = AGENT_SIDE_LATCHKEY_PORT + 1

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

# Filename the remote latchkey gateway reads its permissions config from. The
# local per-host file is named ``latchkey_permissions.json``; on the VPS it
# becomes the gateway's single ``permissions.json``.
_REMOTE_PERMISSIONS_FILENAME: Final[str] = PERMISSIONS_CONFIG_FILENAME
_REMOTE_EXTENSIONS_DIR_NAME: Final[str] = "extensions"
_REMOTE_EXTENSION_CANDIDATE_SUFFIX: Final[str] = ".candidate"

# Filenames (under the remote ``$HOME/.latchkey`` directory) for the
# supervisord-managed gateway and reverse-tunnel programs' stdout/stderr logs.
# Public for the same reason as :data:`REMOTE_LATCHKEY_DIR_NAME`.
REMOTE_GATEWAY_LOG_FILENAME: Final[str] = "gateway.log"
REMOTE_TUNNEL_LOG_FILENAME: Final[str] = "tunnel.log"

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

# Filesystem types (as reported by ``stat -f -c %T``) we accept as RAM-backed
# for the secrets directory. Anything else means the key would land on disk.
_RAM_BACKED_FILESYSTEM_TYPES: Final[frozenset[str]] = frozenset({"tmpfs", "ramfs"})

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


class DesktopGatewaySecrets(FrozenModel):
    """What the machine's forwarding extension presents to the desktop gateway it proxies to.

    Both belong to the computer that is currently connected, not to the machine:
    the password is that computer's own gateway listen password, and the JWT is
    signed by its encryption key and names a path on its disk. So a provisioning
    pass overwrites both -- which is how a workspace created from one of the
    user's computers keeps reaching the desktop-owned endpoint families after
    the user moves to another -- while the machine's own key and listen password
    are adopted rather than replaced.

    They are handed to the extension as files it reads per request (see
    :func:`_build_gateway_run_script`), so overwriting them is enough: the
    gateway does not have to be restarted for the new computer's values to take
    effect.
    """

    gateway_password: str = Field(description="The desktop gateway's own listen password.")
    permissions_override: str = Field(
        description="A JWT targeting the host's permissions file on the desktop that minted it."
    )


def _build_ensure_installed_script(
    latchkey_version: str, node_major_version: str, minimum_node_major_version: int
) -> str:
    """Build an idempotent POSIX-sh script that installs curl, Node.js, supervisor, and latchkey.

    It also installs the datalib "dispatch" curl + the Chrome-impersonating
    curl it fronts (see :data:`DATALIB_CURL_VERSION`).

    Each component is gated behind a presence check -- except Node.js, the
    latchkey CLI, and the curl pair, which are gated behind a *version* check.
    A preinstalled distro node (e.g. Debian bookworm's 18.x) exists but cannot
    run the pinned latchkey/npm, so mere presence must not skip the NodeSource
    install. latchkey and the curl pair are installed under version-less names,
    so only a recorded version distinguishes a stale install from a current one.

    The script avoids ``pipefail`` (unsupported by Debian's default ``/bin/sh``,
    dash) by downloading the NodeSource setup script to a file instead of piping
    it, so ``set -e`` still aborts on a failed download. supervisord is installed
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
            # Install the Chrome-impersonating "dispatch" curl + the
            # impersonator it fronts from the datalib release, so marked
            # latchkey requests (X-Imbue-Impersonate header) clear Cloudflare.
            # Fail-loud under ``set -e`` like every other component here.
            '_ci_arch="$(uname -m)"',
            'case "$_ci_arch" in',
            "  x86_64) _ci_triple=x86_64-unknown-linux-musl ;;",
            "  aarch64|arm64) _ci_triple=aarch64-unknown-linux-musl ;;",
            '  *) echo "no impersonating curl build for arch $_ci_arch" >&2; exit 1 ;;',
            "esac",
            # Reinstall whenever the installed pair came from a different
            # release or triple (a host that predates the stamp, or has no
            # curl at all, reads as an empty string and so never matches).
            # ``$(cat ...)`` drops the stamp's trailing newline.
            f'_ci_want="{DATALIB_CURL_VERSION} ${{_ci_triple}}"',
            f"if [ ! -x {_CURL_DISPATCH_PATH} ] || "
            f'[ "$(cat {_CURL_VERSION_STAMP_PATH} 2>/dev/null)" != "$_ci_want" ]; then',
            '  _ci_tb="curl-${_ci_triple}.tar.gz"',
            f'  _ci_url="https://github.com/{_DATALIB_REPO}/releases/download/{DATALIB_CURL_VERSION}/${{_ci_tb}}"',
            '  _ci_tmp="$(mktemp -d)"',
            '  curl -fsSL --retry 3 --retry-delay 2 -o "${_ci_tmp}/${_ci_tb}" "$_ci_url"',
            '  curl -fsSL --retry 2 -o "${_ci_tmp}/${_ci_tb}.sha256" "${_ci_url}.sha256"',
            '  (cd "${_ci_tmp}" && sha256sum -c "${_ci_tb}.sha256" >/dev/null)',
            '  tar -xzf "${_ci_tmp}/${_ci_tb}" -C "${_ci_tmp}" --strip-components=1',
            # Staged beside their destinations, then renamed into place: see
            # :data:`_CURL_STAGED_SUFFIX`. The dispatch curl is swapped last
            # because it is the one ``LATCHKEY_CURL`` names, so no request ever
            # reaches a new dispatch curl fronting an old impersonator.
            f'  install -m 0755 "${{_ci_tmp}}/{_CURL_DISPATCH_BIN}" "{_CURL_DISPATCH_PATH}{_CURL_STAGED_SUFFIX}"',
            f'  install -m 0755 "${{_ci_tmp}}/{_CURL_IMPERSONATE_BIN}" "{_CURL_IMPERSONATE_PATH}{_CURL_STAGED_SUFFIX}"',
            f'  mv -f "{_CURL_IMPERSONATE_PATH}{_CURL_STAGED_SUFFIX}" "{_CURL_IMPERSONATE_PATH}"',
            f'  mv -f "{_CURL_DISPATCH_PATH}{_CURL_STAGED_SUFFIX}" "{_CURL_DISPATCH_PATH}"',
            # Stamped only once both binaries are in place: a failed download,
            # checksum, or swap aborts under ``set -e`` before reaching this,
            # so the stamp never claims a release the host isn't running.
            f"  printf '%s\\n' \"$_ci_want\" > {_CURL_VERSION_STAMP_PATH}",
            '  rm -rf "${_ci_tmp}"',
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
            # installed version differs (a missing install probes as an empty
            # string). The probe reads the globally-installed package's
            # package.json instead of executing ``latchkey --version``: the CLI
            # resolves its encryption key (to run its store migrations) before
            # printing anything, so on a headless VPS with an existing
            # credential store and no key in the environment ``--version``
            # exits non-zero -- making a binary-executing probe read as a
            # permanent mismatch and reinstall on every provisioning pass.
            # node is guaranteed present by the install/verify steps above.
            "if [ \"$(node -p 'require(process.argv[1]).version' "
            f'"$(npm root -g)/latchkey/package.json" 2>/dev/null)" != "{latchkey_version}" ]; then',
            f"  npm install -g latchkey@{latchkey_version}",
            # A supervisord-managed gateway keeps the old code in memory across
            # an npm upgrade (``reread``/``update`` only bounce a program whose
            # *config* changed), so restart it whenever the binary just
            # changed. ``|| true`` covers hosts where the program is not
            # registered yet (first provisioning) -- the normal
            # reread/update/start later in provisioning brings it up.
            f"  supervisorctl restart {_GATEWAY_PROGRAM_NAME} || true",
            "fi",
        )
    )


def _ensure_latchkey_installed(host: OuterHostInterface) -> None:
    """Ensure curl, Node.js, supervisor, and the pinned latchkey CLI are installed on the VPS.

    Idempotent: each component is installed only when missing, or -- for
    latchkey and the impersonating curl pair -- when the installed version
    differs from :data:`LATCHKEY_VERSION` / :data:`DATALIB_CURL_VERSION`.
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


def _default_permissions_json() -> str:
    """Serialize the deny-all default permissions config (matches ``save_permissions`` output)."""
    config = LatchkeyPermissionsConfig()
    # ``save_permissions`` omits an empty ``schemas`` block; mirror it so the
    # remote file is byte-for-byte the same shape the plugin writes locally.
    exclude: set[str] = set()
    if not config.schemas:
        exclude.add("schemas")
    return config.model_dump_json(indent=2, exclude=exclude)


def _adopt_remote_permissions(
    host: OuterHostInterface, latchkey_directory: Path, host_id: HostId, remote_path: Path
) -> None:
    """Take the machine's permissions as this computer's copy of them.

    How a second computer learns what the first one granted, and how this one
    catches up on anything granted while it was not running. The machine's file
    is read once (each remote read is a network hop) and handed to
    :func:`~imbue.mngr_latchkey.remote._transfer.adopt_machine_permissions`,
    which validates and stores it.

    Raises:
        RemoteGatewayError: when the file cannot be read, is not a policy this
            build understands, or cannot be stored.
    """
    with log_span("Adopting the permissions of host {} from VPS {}", host_id, host.get_name()):
        try:
            content = host.read_text_file(remote_path)
        except (OSError, MngrError) as e:
            raise RemoteGatewayError(f"Failed to read the permissions of host {host_id} from its machine: {e}") from e
        adopt_machine_permissions(latchkey_directory, host_id, content)


def sync_permissions(
    host: OuterHostInterface,
    latchkey_directory: Path,
    host_id: HostId,
    # The machine's ~/.latchkey, resolved once by the caller for the whole
    # reconcile (each resolution is a remote round trip).
    remote_latchkey_dir: Path,
) -> None:
    """Adopt the machine's permissions -- or seed a machine that has none yet.

    A machine's policy lives on the machine (``~/.latchkey/permissions.json``),
    which is what lets a second computer see what the first one granted -- it
    reads the machine rather than a copy it never had. This computer keeps the
    canonical file at
    ``<latchkey_directory>/mngr_latchkey/hosts/<host_id>/latchkey_permissions.json``,
    which is the one the permission UI edits and the gateway extension writes.

    Edits made here are pushed to the machine when they are made
    (:meth:`~imbue.mngr_latchkey.remote.credentials.MachineCredentials.set_permissions`),
    so this pass never has to guess which side is newer: it only *adopts* what
    the machine holds, exactly as an ordinary refresh does
    (:meth:`~imbue.mngr_latchkey.remote.credentials.MachineCredentials.refresh`).
    The one write it makes toward the machine is the seed: a machine with no
    policy yet gets this computer's copy -- or the restrictive deny-all default,
    so a host with no explicit grants still gets a locked-down gateway. That
    seed is why this runs during provisioning rather than waiting for someone
    to open the workspace's Permissions tab: a gateway with no permissions file
    at all permits everything.

    Raises :class:`RemoteGatewayError` if either side cannot be read or written.
    """
    local_path = permissions_path_for_host(plugin_data_dir(latchkey_directory), host_id)
    remote_path = remote_latchkey_dir / _REMOTE_PERMISSIONS_FILENAME
    if host.path_exists(remote_path):
        _adopt_remote_permissions(host, latchkey_directory, host_id, remote_path)
        return
    if local_path.is_file():
        try:
            content = local_path.read_text()
        except OSError as e:
            raise RemoteGatewayError(f"Failed to read host permissions file {local_path}: {e}") from e
    else:
        logger.debug("No local permissions file for host {} at {}; using the restrictive default", host_id, local_path)
        content = _default_permissions_json()

    content_bytes = content.encode("utf-8")
    with log_span("Seeding latchkey permissions for host {} on VPS {} ({})", host_id, host.get_name(), remote_path):
        # Requests originating in a VPS-backed workspace carry no override JWT,
        # so this seeded file is their authorization policy.
        host.write_file(remote_path, content_bytes, mode=REMOTE_FILE_MODE, is_atomic=True)


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


def _reload_supervisor_programs(
    host: OuterHostInterface,
    host_name: str,
    program_name: str,
    *,
    restart: bool = False,
) -> None:
    """Apply a supervisord drop-in and ensure ``program_name`` runs with current inputs.

    ``reread`` reloads the config files and ``update`` applies changed program
    definitions. A best-effort ``start`` recovers a STOPPED/FATAL unchanged
    program. When ``restart`` is true, the running process is deliberately
    bounced so updates made outside the supervisord config itself (such as the
    gateway's tmpfs secrets and wrapper environment) take effect immediately.
    ``program_name`` is a fixed internal literal, so it is interpolated directly.
    """
    ensure_running = (
        f"(supervisorctl restart {program_name} || supervisorctl start {program_name})"
        if restart
        else f"(supervisorctl start {program_name} || true)"
    )
    result = host.execute_idempotent_command(
        f"supervisorctl reread && supervisorctl update && {ensure_running}",
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
    this creates :data:`TMPFS_SECRETS_DIR` and checks (via ``stat -f``) that its
    filesystem type is one of :data:`_RAM_BACKED_FILESYSTEM_TYPES`. If the
    directory is not RAM-backed -- e.g. ``/run`` is unexpectedly not a tmpfs on
    some host -- it refuses to proceed rather than silently persisting the key,
    so the caller never writes the secret to disk. Raises
    :class:`RemoteGatewayError` if creation or the verification fails.
    """
    secrets_dir_q = shlex.quote(str(TMPFS_SECRETS_DIR))
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
            f'  echo "refusing to store the latchkey encryption key: {TMPFS_SECRETS_DIR} is on a '
            '$_fstype filesystem, not RAM-backed (tmpfs/ramfs)" >&2',
            "  exit 1",
            "fi",
        )
    )
    result = host.execute_idempotent_command(script, timeout_seconds=REMOTE_COMMAND_TIMEOUT_SECONDS)
    if not result.success:
        raise RemoteGatewayError(
            "Could not prepare a RAM-backed secrets directory ({}) on VPS {}: {}".format(
                TMPFS_SECRETS_DIR, host_name, result.stderr.strip() or result.stdout.strip()
            )
        )


def _ensure_remote_latchkey_config(
    host: OuterHostInterface, remote_dir: Path, custom_service_entries: Mapping[str, JsonValue]
) -> None:
    """Write minds' hidden services and custom-service registrations into the VPS config.

    Read-merges minds' state into ``~/.latchkey/config.json`` on the VPS via
    :func:`~imbue.mngr_latchkey.core.merge_minds_latchkey_config` (the same merge
    the desktop applies to its own config), so an agent talking to the
    VPS-resident gateway sees the same hidden built-in services as one talking to
    the desktop gateway, and the VPS gateway knows both the bundled additional
    services and the user-created ones in ``custom_service_entries`` (which the
    caller reads from the *desktop's* config, since the VPS config holds none).
    The registration is what makes a custom service's credentials usable: the
    credential sync ships them here, but a gateway with no
    matching registration cannot resolve a request to that service at all, so it
    would never inject them. Any other config latchkey wrote on the VPS is
    preserved. Idempotent. Raises :class:`RemoteGatewayError` if the existing
    remote config is not a valid JSON object.
    """
    config_path = remote_dir / CONFIG_FILENAME
    existing = host.read_text_file(config_path) if host.path_exists(config_path) else None
    try:
        content = merge_minds_latchkey_config(existing, custom_service_entries)
    except LatchkeyError as e:
        raise RemoteGatewayError(
            f"Failed to update latchkey config at {config_path} on VPS {host.get_name()}: {e}"
        ) from e
    # ``is_atomic`` writes to a sibling ``.tmp`` then ``mv``s it into place, so
    # the gateway never reads a half-written config mid-sync.
    host.write_file(config_path, content.encode("utf-8"), mode=REMOTE_FILE_MODE, is_atomic=True)


def _ensure_remote_gateway_extension(host: OuterHostInterface, remote_dir: Path) -> None:
    """Install the VPS-only desktop-gateway proxy.

    The caller restarts the gateway after writing all wrapper inputs, so the new
    extension and any updated secrets take effect together.
    """
    extensions_dir = remote_dir / _REMOTE_EXTENSIONS_DIR_NAME
    destination = extensions_dir / REMOTE_GATEWAY_EXTENSION_FILENAME
    candidate = destination.with_name(destination.name + _REMOTE_EXTENSION_CANDIDATE_SUFFIX)
    content = bundled_gateway_extension_content(REMOTE_GATEWAY_EXTENSION_FILENAME).encode("utf-8")
    host.write_file(candidate, content, mode=REMOTE_FILE_MODE, is_atomic=True)

    extensions_dir_q = shlex.quote(str(extensions_dir))
    destination_q = shlex.quote(str(destination))
    candidate_q = shlex.quote(str(candidate))
    script = "\n".join(
        (
            "set -e",
            f"mkdir -p {extensions_dir_q}",
            f"chmod 700 {extensions_dir_q}",
            f"if [ ! -f {destination_q} ] || ! cmp -s {candidate_q} {destination_q}; then",
            f"  mv {candidate_q} {destination_q}",
            f"  chmod {REMOTE_FILE_MODE} {destination_q}",
            "else",
            f"  rm -f {candidate_q}",
            "fi",
        )
    )
    result = host.execute_idempotent_command(script, timeout_seconds=REMOTE_COMMAND_TIMEOUT_SECONDS)
    if not result.success:
        raise RemoteGatewayError(
            "Failed to install the latchkey desktop-gateway proxy extension on VPS {}: {}".format(
                host.get_name(), result.stderr.strip() or result.stdout.strip()
            )
        )


def _build_gateway_run_script(
    outer_port: int,
    key_file_path: Path,
    password_file_path: Path,
    desktop_password_file_path: Path,
    desktop_permissions_override_file_path: Path,
    desktop_gateway_url: str,
) -> str:
    """Build the wrapper script supervisord runs to launch ``latchkey gateway``.

    supervisord invokes this as ``/bin/sh <script>``. It reads the encryption key
    and the gateway listen password from 0600 tmpfs files into their respective
    environment variables and ``exec``s the gateway (so supervisord tracks the
    gateway PID directly, not a wrapping shell). Reading the secrets from files
    -- rather than baking them into the supervisord ``command=`` line -- keeps
    them out of the config file and out of any process listing
    (``/proc/<pid>/cmdline``). The gateway binds ``outer_port`` on the VPS
    loopback only; it is reached from the container via the reverse tunnel, never
    exposed off-host.

    The secret files live in tmpfs (see :data:`TMPFS_SECRETS_DIR`), so a reboot
    wipes them. The script therefore refuses to launch a gateway with missing
    secrets: it exits non-zero with a clear message, which supervisord treats as
    a failed start (the gateway stays down until the next provisioning pass
    re-writes the secrets) rather than running a broken, keyless gateway.

    ``LATCHKEY_ENCRYPTION_KEY`` is this machine's own key, which is what its
    ``credentials.json.enc`` is encrypted with, and
    ``LATCHKEY_GATEWAY_LISTEN_PASSWORD`` is the machine's own listen password,
    which the agents on it present as ``LATCHKEY_GATEWAY_PASSWORD``.

    The desktop-owned pair is handed over as *paths*, not values: the forwarding
    extension reads them on every request it proxies, so a provisioning pass
    from another of the user's computers takes effect without this gateway (and
    the workspace it is serving) having to be restarted. Their absence is
    therefore not a launch failure either -- the extension answers the
    desktop-owned routes with a 503 that says so, while third-party calls, which
    need neither, keep working.

    The gateway refreshes its own OAuth credentials, because the store here is
    this machine's own rather than a copy of anyone else's: nothing else holds
    the refresh tokens in it, so nothing else can race it to rotate one -- and a
    machine that renews its own tokens keeps working while the user's computer
    is off. ``--max-body-size`` matches the limit the desktop-side gateway uses
    (:data:`GATEWAY_MAX_BODY_SIZE_BYTES`).
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
            # Read the secrets from their 0600 files into the environment; only
            # the file paths (not the secrets) ever appear in this script.
            f'LATCHKEY_ENCRYPTION_KEY="$(cat {key_q})"',
            f'LATCHKEY_GATEWAY_LISTEN_PASSWORD="$(cat {password_q})"',
            "export LATCHKEY_ENCRYPTION_KEY LATCHKEY_GATEWAY_LISTEN_PASSWORD",
            f"export LATCHKEY_EXTENSION_DESKTOP_GATEWAY_PASSWORD_FILE={shlex.quote(str(desktop_password_file_path))}",
            "export LATCHKEY_EXTENSION_DESKTOP_GATEWAY_PERMISSIONS_OVERRIDE_FILE="
            f"{shlex.quote(str(desktop_permissions_override_file_path))}",
            # ``LATCHKEY_GATEWAY_LISTEN_PORT`` is the name upstream reads; a
            # gateway handed any other one silently binds latchkey's default.
            f"export LATCHKEY_GATEWAY_LISTEN_PORT={outer_port}",
            "export LATCHKEY_GATEWAY_LISTEN_HOST=127.0.0.1",
            "export LATCHKEY_DISABLE_COUNTING=1",
            f"export LATCHKEY_EXTENSION_DESKTOP_GATEWAY_URL={shlex.quote(desktop_gateway_url)}",
            # Route latchkey through the bundled dispatch curl (installed by
            # _build_ensure_installed_script): requests carrying the
            # X-Imbue-Impersonate marker header get Chrome TLS impersonation
            # via the sibling impersonator, everything else uses system curl.
            f"export LATCHKEY_CURL={_CURL_DISPATCH_PATH}",
            f"exec latchkey gateway --max-body-size {GATEWAY_MAX_BODY_SIZE_BYTES}",
            "",
        )
    )


def _ensure_latchkey_gateway_running(
    host: OuterHostInterface,
    latchkey_directory: Path,
    machine_encryption_key: SecretStr,
    machine_gateway_password: str,
    desktop_secrets: DesktopGatewaySecrets,
) -> None:
    """Register (and start) the ``latchkey gateway`` as a supervisord program on the VPS.

    Writes a supervisord drop-in that launches ``latchkey gateway`` bound to the
    VPS loopback on ``OUTER_PORT`` and applies it via ``reread``/``update``, so
    supervisord keeps the gateway running and restarts it if it crashes. This
    machine's own encryption key and listen password (see
    :func:`_resolve_machine_encryption_key` and
    :func:`_resolve_machine_gateway_password`) plus ``desktop_secrets`` are
    written to 0600 files in a tmpfs directory (:data:`TMPFS_SECRETS_DIR`); a
    wrapper script (on the normal disk) reads the machine's own two into the
    gateway's environment at launch and hands the desktop-owned pair over as
    paths, so no secret ever appears in the supervisord config or a process
    listing.

    The secrets go in tmpfs (RAM), not on the disk beside the encrypted
    ``credentials.json.enc``: this deliberately keeps the encryption key off the
    persistent disk (a key file beside the encrypted store would, against a
    disk-snapshot threat model, be equivalent to storing the credentials in
    plaintext) while still surviving a process crash, so supervisord can restart
    the gateway without a desktop round-trip. The tradeoff is that a reboot
    wipes the secrets, leaving the gateway down until the next provisioning pass
    re-writes them -- an accepted limitation, and the reason the desktop keeps
    the durable copy of this machine's key. Idempotent. Raises
    :class:`RemoteGatewayError` if the reload fails.
    """
    encryption_key = machine_encryption_key.get_secret_value()
    remote_dir = resolve_remote_latchkey_directory(host)
    # Secrets go in tmpfs (RAM); the wrapper + log stay on the normal disk.
    key_file_path = TMPFS_SECRETS_DIR / GATEWAY_ENCRYPTION_KEY_FILENAME
    password_file_path = TMPFS_SECRETS_DIR / GATEWAY_LISTEN_PASSWORD_FILENAME
    desktop_password_file_path = TMPFS_SECRETS_DIR / DESKTOP_GATEWAY_PASSWORD_FILENAME
    desktop_permissions_override_file_path = TMPFS_SECRETS_DIR / DESKTOP_PERMISSIONS_OVERRIDE_FILENAME
    run_script_path = remote_dir / _GATEWAY_RUN_SCRIPT_FILENAME
    log_path = remote_dir / REMOTE_GATEWAY_LOG_FILENAME
    conf_path = _SUPERVISOR_CONFD_DIR / _GATEWAY_CONF_FILENAME
    host_name = host.get_name()

    # Create + verify the RAM-backed secrets dir before writing the key, so we
    # never persist it to a disk filesystem if tmpfs is unexpectedly absent.
    _ensure_ram_backed_secrets_dir(host, host_name)

    # Write this package's config.json state before the gateway starts: the
    # confusing built-in services (e.g. ``notion``) stay hidden, and the custom
    # services are registered so this gateway can inject the credentials the credential
    # sync ships for them. Services created later reach this file as the config
    # snapshot every connect installs (see ``MachineCredentials._config_for``).
    _ensure_remote_latchkey_config(host, remote_dir, custom_service_registration_entries(latchkey_directory))

    # Write the secrets (0600) into tmpfs and the wrapper that reads them. The
    # machine's own two are rewritten with what it already runs under; the
    # desktop-owned pair is this computer's, and replaces whichever computer's
    # was there before.
    host.write_file(key_file_path, encryption_key.encode("utf-8"), mode=REMOTE_FILE_MODE)
    host.write_file(password_file_path, machine_gateway_password.encode("utf-8"), mode=REMOTE_FILE_MODE)
    host.write_file(
        desktop_password_file_path,
        desktop_secrets.gateway_password.encode("utf-8"),
        mode=REMOTE_FILE_MODE,
    )
    host.write_file(
        desktop_permissions_override_file_path,
        desktop_secrets.permissions_override.encode("utf-8"),
        mode=REMOTE_FILE_MODE,
    )
    _ensure_remote_gateway_extension(host, remote_dir)
    desktop_gateway_url = f"http://127.0.0.1:{DESKTOP_GATEWAY_VPS_PORT}"
    run_script = _build_gateway_run_script(
        OUTER_PORT,
        key_file_path,
        password_file_path,
        desktop_password_file_path,
        desktop_permissions_override_file_path,
        desktop_gateway_url,
    )
    host.write_file(run_script_path, run_script.encode("utf-8"), mode="0700")

    # Write the supervisord program config, then reread/update/start to apply it.
    command = f"{_SH_BINARY_PATH} {shlex.quote(str(run_script_path))}"
    conf = _build_supervisor_program_config(
        _GATEWAY_PROGRAM_NAME, command, str(log_path), _SUPERVISOR_GATEWAY_START_RETRIES
    )
    with log_span("Ensuring latchkey gateway is running on VPS {} (port {})", host_name, OUTER_PORT):
        host.write_file(conf_path, conf.encode("utf-8"), mode=REMOTE_FILE_MODE, is_atomic=True)
        _reload_supervisor_programs(host, host_name, _GATEWAY_PROGRAM_NAME, restart=True)


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

    Binds the container's ``127.0.0.1:AGENT_SIDE_LATCHKEY_PORT`` and forwards it to the VPS's
    ``127.0.0.1:OUTER_PORT`` (where :func:`_ensure_latchkey_gateway_running`
    started the gateway), so the agent's fixed ``LATCHKEY_GATEWAY`` URL
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
        inner_port=AGENT_SIDE_LATCHKEY_PORT,
        outer_port=OUTER_PORT,
    )
    log_path = resolve_remote_latchkey_directory(host) / REMOTE_TUNNEL_LOG_FILENAME
    conf_path = _SUPERVISOR_CONFD_DIR / _TUNNEL_CONF_FILENAME
    conf = _build_supervisor_program_config(
        _TUNNEL_PROGRAM_NAME, command, str(log_path), _SUPERVISOR_TUNNEL_START_RETRIES
    )
    host_name = host.get_name()
    with log_span(
        "Ensuring latchkey gateway is reachable from the container on VPS {} (container:{} -> gateway:{})",
        host_name,
        AGENT_SIDE_LATCHKEY_PORT,
        OUTER_PORT,
    ):
        host.write_file(conf_path, conf.encode("utf-8"), mode=REMOTE_FILE_MODE, is_atomic=True)
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
    key_path = resolve_remote_latchkey_directory(host) / _CONTAINER_TUNNEL_KEY_FILENAME
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
        result = host.execute_idempotent_command(script, timeout_seconds=REMOTE_COMMAND_TIMEOUT_SECONDS)
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
            f'rm -f "$HOME/.latchkey/{GATEWAY_ENCRYPTION_KEY_FILENAME}" '
            f'"$HOME/.latchkey/{GATEWAY_LISTEN_PASSWORD_FILENAME}" '
            f'"$HOME/.latchkey/{DESKTOP_PERMISSIONS_OVERRIDE_FILENAME}"',
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
    forward_spec = f"127.0.0.1:{_LEGACY_INNER_PORT}:127.0.0.1:{OUTER_PORT}"
    script = _build_legacy_migration_script(forward_spec)
    host_name = host.get_name()
    with log_span("Migrating any legacy nohup latchkey gateway/tunnel to supervisord on VPS {}", host_name):
        result = host.execute_idempotent_command(script, timeout_seconds=REMOTE_COMMAND_TIMEOUT_SECONDS)
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
    result = host.execute_idempotent_command(command, timeout_seconds=REMOTE_COMMAND_TIMEOUT_SECONDS)
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


def _resolve_machine_encryption_key(host: OuterHostInterface, latchkey_directory: Path, host_id: HostId) -> SecretStr:
    """Return the encryption key this machine keeps its own credential store under.

    The machine's own copy -- the tmpfs file its gateway reads -- is the source
    of truth while it exists: any of the user's computers may have provisioned
    the machine, so what this computer recorded is only a durable *mirror* of
    the machine's key, kept so a rebooted machine (whose tmpfs is wiped) can be
    handed its key back. Adopting the running key, rather than deciding one,
    is what lets two Minds installs manage one machine without re-keying it out
    from under each other.

    Only when neither the machine nor this computer knows a key is one decided
    (see :func:`_decide_key_for_machine_with_no_known_key`).

    Raises:
        RemoteGatewayError: when the key cannot be read, decided, or recorded.
    """
    data_dir = plugin_data_dir(latchkey_directory)
    try:
        running_key = read_machine_key_from_secrets_dir(host)
        recorded_key = stored_machine_encryption_key(data_dir, host_id)
        if running_key is not None:
            if recorded_key is None or recorded_key.get_secret_value() != running_key.get_secret_value():
                store_machine_encryption_key(data_dir, host_id, running_key)
                logger.info("Adopted the encryption key the machine of host {} is already running under", host_id)
            return running_key
        if recorded_key is not None:
            # The machine rebooted (or has never run a gateway); hand it back
            # the key its store is already written under.
            return recorded_key
        key = _decide_key_for_machine_with_no_known_key(host, latchkey_directory, host_id)
        store_machine_encryption_key(data_dir, host_id, key)
    except (LatchkeyStoreError, LatchkeyEncryptionKeyPermissionError) as e:
        raise RemoteGatewayError(f"Failed to resolve the encryption key for host {host_id}: {e}") from e
    return key


def _resolve_machine_gateway_password(
    host: OuterHostInterface,
    latchkey_directory: Path,
    host_id: HostId,
    desktop_gateway_password: str,
) -> str:
    """Return the listen password this machine's gateway holds its callers to.

    Adopted, never decided, for a blunter reason than the encryption key is
    (:func:`_resolve_machine_encryption_key`): the workspaces on this machine
    present the password from a host env file written once, at ``mngr create``,
    and nothing rewrites that file for the life of the workspace. Whichever of
    the user's computers created them therefore fixed the value, and a second
    computer that wrote its own here would answer every one of their requests
    with a 401.

    So the machine's running copy wins, this computer's record of it is the
    fallback for a machine whose tmpfs a reboot wiped, and only a machine that
    neither is running one nor has one recorded gets ``desktop_gateway_password``
    -- the value this computer bakes into the workspaces it creates, and hence
    the right one for a machine it is about to provision for the first time.

    One combination cannot be served, and is why the record is worth writing on
    every pass: a machine whose password no computer has recorded yet, which
    rebooted (wiping its own copy) before this computer -- not the one that
    created its workspaces -- provisioned it. It is seeded with a password those
    workspaces do not hold, and every later pass then adopts that, since nothing
    anywhere still knows the value they were given. Their latchkey calls stay
    unauthorized; a build that records the password while the machine is still
    running closes the window for good.

    Raises:
        RemoteGatewayError: when the password cannot be read or recorded.
    """
    data_dir = plugin_data_dir(latchkey_directory)
    try:
        running_password = read_machine_gateway_password_from_secrets_dir(host)
        recorded_password = stored_machine_gateway_password(data_dir, host_id)
        if running_password is not None:
            if running_password != recorded_password:
                store_machine_gateway_password(data_dir, host_id, running_password)
                logger.info(
                    "Adopted the gateway listen password the machine of host {} is already running under", host_id
                )
            return running_password
        if recorded_password is not None:
            # The machine rebooted (or has never run a gateway); hand it back
            # the password its workspaces were created with.
            return recorded_password
        store_machine_gateway_password(data_dir, host_id, desktop_gateway_password)
    except LatchkeyStoreError as e:
        raise RemoteGatewayError(f"Failed to resolve the gateway listen password for host {host_id}: {e}") from e
    logger.info("Host {} will hold its callers to this computer's gateway listen password", host_id)
    return desktop_gateway_password


def _decide_key_for_machine_with_no_known_key(
    host: OuterHostInterface, latchkey_directory: Path, host_id: HostId
) -> SecretStr:
    """Decide the key for a machine that is not running one, with none recorded here.

    A machine holding no credential store gets a fresh random key: there is
    nothing a key choice could make unreadable, and a key of its own means what
    the machine will hold is readable by that machine and by the desktops the
    user manages it from, and by nothing else.

    A machine that *does* hold a store can only be served by the key the store
    was written under, so the one candidate anyone here holds -- this desktop's
    key, which is what a build predating per-machine keys provisioned machines
    with -- is verified against the store rather than assumed. When it opens the
    store, this is our own legacy machine and the desktop key is kept (agents
    created before the one-gateway rollout also carry permissions-override JWTs
    the gateway validates with a key derived from it, so rotating it would
    reject everything they send, with no way to reissue their tokens).

    When it does not, the store was written under a key nobody present holds:
    another computer provisioned this machine and the machine rebooted since.
    The store is abandoned -- removed, and a fresh key minted -- because signing
    the machine's services in again is always possible, while waiting for a
    computer that may never return is not.

    CLEANUP: once no agent predates the one-gateway rollout, drop the
    desktop-key verification branch (minting fresh for every storeless machine
    and abandoning every unreadable store).
    """
    remote_dir = resolve_remote_latchkey_directory(host)
    if not host.path_exists(remote_dir / CREDENTIALS_STORE_FILENAME):
        # No store to get wrong. A remote latchkey directory without one still
        # means an older build provisioned the machine, so its agents' override
        # JWTs pin it to the desktop key.
        is_provisioned_by_an_older_build = host.path_exists(remote_dir)
        key = (
            load_or_create_encryption_key(latchkey_directory)
            if is_provisioned_by_an_older_build
            else generate_machine_encryption_key()
        )
        logger.info(
            "Host {} will keep its credentials under {}",
            host_id,
            "the desktop's encryption key (provisioned by an older build)"
            if is_provisioned_by_an_older_build
            else "its own encryption key",
        )
        return key
    desktop_key = load_or_create_encryption_key(latchkey_directory)
    if _does_key_open_the_machine_store(host, desktop_key):
        logger.info("Host {} keeps its credentials under the desktop's key (provisioned by an older build)", host_id)
        return desktop_key
    logger.warning(
        "Abandoning the credential store of host {}: it was written under a key this computer does not hold "
        "(provisioned by another computer, and the machine rebooted since); its services need signing in again",
        host_id,
    )
    _abandon_machine_store(host, host_id)
    return generate_machine_encryption_key()


def _does_key_open_the_machine_store(host: OuterHostInterface, candidate_key: SecretStr) -> bool:
    """Whether the machine's own credential store decrypts under ``candidate_key``.

    Asked of the machine itself (an offline ``auth list`` under the candidate),
    because only the store can answer: there is nothing on this computer to
    check a guessed key against. The candidate travels in the script body, the
    way a re-encrypt out-key does, never in ``argv`` on the machine.
    """
    script = "\n".join(
        (
            "set -e",
            f"LATCHKEY_ENCRYPTION_KEY={shlex.quote(candidate_key.get_secret_value())}",
            "export LATCHKEY_ENCRYPTION_KEY",
            "latchkey auth list --offline >/dev/null",
        )
    )
    result = host.execute_idempotent_command(script, timeout_seconds=REMOTE_LATCHKEY_TIMEOUT_SECONDS)
    if not result.success:
        logger.debug(
            "Ruled out a candidate key for the store on VPS {}: {}",
            host.get_name(),
            summarize_latchkey_failure(result.stderr.strip(), "the machine reported no reason"),
        )
    return result.success


def _abandon_machine_store(host: OuterHostInterface, host_id: HostId) -> None:
    """Remove the machine's credential store: nobody present holds the key it was written under.

    Raises:
        RemoteGatewayError: when the store cannot be removed -- leaving it would
            run a gateway against a store its key cannot read.
    """
    store_path = resolve_remote_latchkey_directory(host) / CREDENTIALS_STORE_FILENAME
    run_remote_command(
        host,
        f"rm -f {shlex.quote(str(store_path))}",
        failure_description=f"abandon the unreadable credential store of host {host_id}",
    )


def provision_remote_gateway(
    host: OuterHostInterface,
    host_id: HostId,
    container_ssh_user: str,
    container_ssh_port: int,
    latchkey_directory: Path,
    desktop_secrets: DesktopGatewaySecrets,
) -> None:
    """Stand up a VPS-resident latchkey gateway and tunnel it into the agent's container.

    Runs the full remote-gateway sequence on the agent's outer host (the VPS):
    install the latchkey CLI and supervisord, register the gateway as a
    supervisord program bound to the VPS loopback (with this machine's own
    encryption key so it can decrypt the credentials it is given, and its own
    listen password so it accepts the traffic of the workspaces on it, plus
    ``desktop_secrets`` for the forwarding extension's hop to this computer),
    mint an ad-hoc
    VPS->container keypair, and register the VPS->container reverse tunnel as a
    second supervisord program so the agent's
    ``LATCHKEY_GATEWAY=http://127.0.0.1:AGENT_SIDE_LATCHKEY_PORT`` reaches it. supervisord
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
    # Stand up the VM-resident owner-exec daemon first: it is independent of the
    # latchkey gateway (a web workspace uses it to configure the VM, including to
    # provision latchkey), and piggybacking on this pass is how every remote
    # provider converges on the one exec channel. A failure here fails the whole
    # provisioning pass, which is retried on the next discovery cycle.
    provision_owner_exec_vm(host, host_id)
    _ensure_latchkey_installed(host)
    # Tear down any pre-supervisord (nohup + PID-file) gateway/tunnel first: an
    # old build's processes still hold OUTER_PORT and the container's forward
    # bind, which would make the new supervisord programs fail to start.
    _migrate_legacy_remote_gateway_state(host)
    _ensure_latchkey_gateway_running(
        host,
        latchkey_directory,
        _resolve_machine_encryption_key(host, latchkey_directory, host_id),
        _resolve_machine_gateway_password(host, latchkey_directory, host_id, desktop_secrets.gateway_password),
        desktop_secrets,
    )
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
