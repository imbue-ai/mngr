"""Build the Debian package that stands up a machine's latchkey gateway.

Everything a remote host's machine (the VPS) needs from this computer, other
than its data, travels as one ``.deb``: the supervisord programs for the
gateway and the reverse tunnel, the wrapper scripts they run, the forwarding
extension the gateway loads, and the two commands this computer drives the
machine through afterwards -- ``mngr-latchkey read-state``, which assembles
what the machine holds, and ``mngr-latchkey apply-state``, which makes the
machine match a document handed to it (see :mod:`imbue.mngr_latchkey.remote._machine`).
Its ``postinst`` installs the pinned latchkey CLI and the curl shims, loads
the nftables policy that keeps the machine's bridge-bound services on the
docker bridge (shipped with its boot-time systemd unit), scrubs what the
ad-hoc provisioning left on a machine it set up, and registers the programs,
so installing the package is the whole of "set this machine up".

The package's source is the ``debian/tree/`` directory beside this module,
laid out exactly as the package's files land on the machine, with ``DEBIAN/``
holding the control file and maintainer scripts. Files ending in ``.j2`` are
jinja templates rendered from a :class:`RemotePackageContext` -- the versions,
ports, paths and names this computer decides -- and lose the suffix; the
forwarding extension is taken from the bundled extensions rather than kept as a
second copy here. The build is pure Python (a ``.deb`` is an ``ar`` archive of
two tarballs), so it runs wherever the desktop does, and it is deterministic:
the same sources and context give the same bytes, and the package version
carries a hash of them so the version installed on a machine says exactly
which build it is.

The package is ``Architecture: all`` and small: the arch-specific binaries
(Node.js, the latchkey CLI, the curl shims) are fetched by its scripts on the
machine, version-gated, rather than shipped. Node.js and the package's Debian
dependencies come from apt, which cannot run from inside a maintainer script
(dpkg holds its lock), so :func:`render_bootstrap_script` produces the short
script that puts them in place and then installs the package.
"""

import gzip
import hashlib
import importlib.metadata
import io
import shlex
import tarfile
from collections.abc import Mapping
from collections.abc import Sequence
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Final

from jinja2 import Environment
from jinja2 import StrictUndefined
from jinja2 import TemplateError
from pydantic import Field
from pydantic import model_validator

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.core import AGENT_SIDE_LATCHKEY_PORT
from imbue.mngr_latchkey.core import CONFIG_FILENAME
from imbue.mngr_latchkey.core import CREDENTIALS_STORE_FILENAME
from imbue.mngr_latchkey.core import GATEWAY_MAX_BODY_SIZE_BYTES
from imbue.mngr_latchkey.core import PERMISSIONS_CONFIG_FILENAME
from imbue.mngr_latchkey.core import REMOTE_GATEWAY_EXTENSION_FILENAME
from imbue.mngr_latchkey.core import UPSTREAM_DATA_FORMAT_VERSION_FILENAME
from imbue.mngr_latchkey.core import bundled_gateway_extension_content
from imbue.mngr_latchkey.docker_bridge import BRIDGE_SERVICES_FIREWALL_UNIT_NAME
from imbue.mngr_latchkey.docker_bridge import BRIDGE_SERVICES_NFT_POLICY_FILENAME
from imbue.mngr_latchkey.docker_bridge import BRIDGE_SERVICES_NFT_TABLE
from imbue.mngr_latchkey.docker_bridge import DOCKER_BRIDGE_INTERFACE_NAME
from imbue.mngr_latchkey.docker_bridge import NFT_BINARY_PATH
from imbue.mngr_latchkey.owner_exec_vm import VM_EXEC_PORT
from imbue.mngr_latchkey.remote._machine import ANSWER_PREFIX
from imbue.mngr_latchkey.remote._machine import DESKTOP_GATEWAY_PASSWORD_FILENAME
from imbue.mngr_latchkey.remote._machine import DESKTOP_PERMISSIONS_OVERRIDE_FILENAME
from imbue.mngr_latchkey.remote._machine import DIFFERENT_MACHINE_KEY_MESSAGE
from imbue.mngr_latchkey.remote._machine import DIFFERENT_MACHINE_PASSWORD_MESSAGE
from imbue.mngr_latchkey.remote._machine import GATEWAY_ENCRYPTION_KEY_FILENAME
from imbue.mngr_latchkey.remote._machine import GATEWAY_LISTEN_PASSWORD_FILENAME
from imbue.mngr_latchkey.remote._machine import OUTCOME_DONE_MARKER
from imbue.mngr_latchkey.remote._machine import REMOTE_LATCHKEY_DIR_NAME
from imbue.mngr_latchkey.remote._machine import TMPFS_SECRETS_DIR
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError
from imbue.mngr_latchkey.store import DESKTOP_EGRESS_RULES_FILENAME

# Version of the upstream ``latchkey`` CLI the package installs on the machine.
LATCHKEY_VERSION: Final[str] = "3.15.0"

# latchkey-curl-shims release the machine fetches the curl router + the
# Chrome-impersonating curl from (``latchkey-curl-shims-<triple>.tar.gz``). The
# gateway runs the router as its ``LATCHKEY_CURL``: a request carrying the
# ``X-Imbue-Impersonate`` marker header gets Chrome TLS impersonation, and every
# other request passes through to the system curl. The statically linked musl
# build is fetched, so it runs on any VPS image regardless of how old its glibc
# is.
CURL_SHIMS_REPO: Final[str] = "imbue-ai/latchkey-curl-shims"
CURL_SHIMS_VERSION: Final[str] = "v0.4.0"
# sha256 of each tarball a machine can fetch, from the release's ``SHA256SUMS``.
# Pinned here rather than downloaded beside the tarball, so a tarball replaced
# on the release fails the install instead of verifying against its own sum.
CURL_SHIMS_SHA256_BY_TRIPLE: Final[Mapping[str, str]] = {
    "x86_64-unknown-linux-musl": "7173b301133c4a465174481041ade5c1f8fffac4ae4b86d54cdf4279ac7a0f93",
    "aarch64-unknown-linux-musl": "afa65e2c795dce9acfd32ad22d732c27509a07382e6db00626a98e1ec93f8f76",
}
# ``uname -m`` patterns, as ``case`` alternatives, and the build each selects.
CURL_SHIMS_TRIPLE_BY_UNAME_PATTERN: Final[tuple[tuple[str, str], ...]] = (
    ("x86_64", "x86_64-unknown-linux-musl"),
    ("aarch64|arm64", "aarch64-unknown-linux-musl"),
)
CURL_ROUTER_BIN: Final[str] = "latchkey-curl-router"
CURL_IMPERSONATE_BIN: Final[str] = "curl-impersonate"
# Suffix the new binaries are staged under before being renamed over the old
# ones (overwriting a binary the gateway is executing fails with ETXTBSY).
CURL_STAGED_SUFFIX: Final[str] = ".new"
# Records which release and target triple the installed shims came from; the
# binaries live under version-less names, so this is what a bump reaches.
CURL_VERSION_STAMP_FILENAME: Final[str] = ".latchkey-curl-version"

# Port on the machine's loopback where the desktop gateway is reverse-tunneled.
# The forwarding extension sends the desktop-owned endpoint families here.
DESKTOP_GATEWAY_VPS_PORT: Final[int] = 1988

# Port the latchkey gateway binds on the machine's docker bridge address. It is
# the same fixed agent-side port a desktop-gateway agent uses, so every agent's
# gateway URL differs only in its host: http://host.docker.internal:1989 on a
# VPS host, http://127.0.0.1:1989 on a local one (and on a VPS host whose
# container predates the docker-bridge route, where the reverse tunnel serves it).
OUTER_PORT: Final[int] = AGENT_SIDE_LATCHKEY_PORT

# Major Node.js version installed via NodeSource, and the oldest pre-existing
# major accepted without reinstalling: a VPS image frequently ships a distro
# node that exists but is too old to run the pinned latchkey (Debian bookworm
# ships 18.x, and modern npm releases refuse node < 20.17).
NODE_MAJOR_VERSION: Final[str] = "24"
MINIMUM_NODE_MAJOR_VERSION: Final[int] = 20

# Filenames (under the machine's log directory) of the supervisord-managed
# gateway and reverse-tunnel programs' stdout/stderr logs.
REMOTE_GATEWAY_LOG_FILENAME: Final[str] = "gateway.log"
REMOTE_TUNNEL_LOG_FILENAME: Final[str] = "tunnel.log"

# supervisord program names, and the drop-in filenames the package ships them
# under (the distro ``supervisor`` package's ``supervisord.conf`` includes
# ``conf.d/*.conf``).
GATEWAY_PROGRAM_NAME: Final[str] = "latchkey-gateway"
TUNNEL_PROGRAM_NAME: Final[str] = "latchkey-tunnel"

# supervisord program tuning. ``startsecs`` is how long a program must stay up
# to count as started; the gateway's few ``startretries`` let it go quietly
# FATAL after a reboot (its RAM-backed secrets are gone), while the tunnel's
# huge count keeps it retrying while the container's sshd comes up. The tunnel
# is not autostarted: ``apply-state`` starts it only for a container that needs
# it, and a reboot leaves it down beside the gateway until the next pass.
_SUPERVISOR_START_SECONDS: Final[int] = 5
_SUPERVISOR_GATEWAY_START_RETRIES: Final[int] = 3
_SUPERVISOR_TUNNEL_START_RETRIES: Final[int] = 1_000_000
_SUPERVISOR_LOG_MAX_BYTES: Final[str] = "10MB"
_SUPERVISOR_LOG_BACKUPS: Final[int] = 3

# SSH keepalive tuning for the reverse tunnel, so a tunnel whose far end
# vanished (a VM paused for a week and resumed) exits and gets restarted
# instead of hanging on a dead TCP connection.
_SSH_SERVER_ALIVE_INTERVAL_SECONDS: Final[int] = 30
_SSH_SERVER_ALIVE_COUNT_MAX: Final[int] = 3
_SSH_CONNECT_TIMEOUT_SECONDS: Final[int] = 15

# Absolute paths to the interpreters named in supervisord ``command=`` lines
# (supervisord resolves programs via its *own* PATH) and in the tunnel wrapper.
_SH_BINARY_PATH: Final[str] = "/bin/sh"
_SSH_BINARY_PATH: Final[str] = "/usr/bin/ssh"

# Filesystem types (as reported by ``stat -f -c %T``) accepted as RAM-backed
# for the secrets directory. Anything else means a key would land on disk.
_RAM_BACKED_FILESYSTEM_TYPES: Final[tuple[str, ...]] = ("ramfs", "tmpfs")

# Docker label key every mngr container carries, valued with the host id (kept
# as a literal to avoid a dependency on the provider packages that stamp it).
CONTAINER_HOST_ID_LABEL: Final[str] = "com.imbue.mngr.host-id"

# Files under the machine's latchkey directory: the address the gateway binds
# (read by the gateway wrapper, and by the tunnel wrapper as its far end), the
# ad-hoc private key for the machine -> container SSH the reverse tunnel uses
# (its ``.pub`` sits beside it), the tunnel target the tunnel wrapper reads, and
# the gateway's extensions.
GATEWAY_CONF_FILENAME: Final[str] = "gateway.conf"
# CLEANUP: drop the reverse tunnel (this key and target, and everything else
# marked CLEANUP as pointing here) once no remote host whose agent reaches the
# gateway at its own loopback remains; the info log in
# ``provisioning._does_container_need_reverse_tunnel`` shows when that is.
CONTAINER_TUNNEL_KEY_FILENAME: Final[str] = "container_tunnel_key"
TUNNEL_CONF_FILENAME: Final[str] = "tunnel.conf"
REMOTE_EXTENSIONS_DIR_NAME: Final[str] = "extensions"

# What the ad-hoc provisioning this package replaces left under the machine's
# latchkey directory, which the package's postinst scrubs: the gateway wrapper
# it wrote beside the store, and the suffix it staged the extension under.
_LEGACY_GATEWAY_RUN_SCRIPT_FILENAME: Final[str] = "gateway_run.sh"
_LEGACY_EXTENSION_CANDIDATE_SUFFIX: Final[str] = ".candidate"

# The ports the machine binds on its docker bridge address, which the shipped
# nftables policy confines to that bridge (and loopback): the gateway's and the
# owner-exec daemon's.
_BRIDGE_SERVICE_PORTS: Final[tuple[int, ...]] = (OUTER_PORT, VM_EXEC_PORT)

# The package's name, and the Debian packages it depends on (declared in its
# control file, installed by the bootstrap): supervisord for the programs, curl
# for the fetches its postinst makes, ssh for the tunnel, nftables for the
# policy that keeps the bridge-bound services on the docker bridge.
PACKAGE_NAME: Final[str] = "mngr-latchkey"
_DEBIAN_DEPENDENCIES: Final[tuple[str, ...]] = (
    "supervisor",
    "curl",
    "ca-certificates",
    "openssh-client",
    "nftables",
)

# The Python distribution whose version the package version starts from.
_DISTRIBUTION_NAME: Final[str] = "imbue-mngr-latchkey"
_VERSION_HASH_CHARS: Final[int] = 12

# Package sources: the tree beside this module, its template suffix, the
# bootstrap template outside the tree, and where the extension goes in it.
_SOURCES_PACKAGE: Final[str] = "imbue.mngr_latchkey.remote.debian"
_TREE_DIR_NAME: Final[str] = "tree"
_BOOTSTRAP_TEMPLATE_NAME: Final[str] = "bootstrap.j2"
_TEMPLATE_SUFFIX: Final[str] = ".j2"
_CONTROL_DIR_NAME: Final[str] = "DEBIAN"
# Where the tree places the package's files, which a layout's directories
# (:class:`RemotePackageLayout`) have to end with.
_INSTALL_DIR_IN_TREE: Final[str] = "usr/lib/mngr-latchkey"
_BIN_DIR_IN_TREE: Final[str] = "usr/bin"
_SUPERVISOR_CONFD_DIR_IN_TREE: Final[str] = "etc/supervisor/conf.d"
_NFTABLES_CONFD_DIR_IN_TREE: Final[str] = "etc/nftables.d"
_SYSTEMD_UNIT_DIR_IN_TREE: Final[str] = "etc/systemd/system"
# The log directory ships empty (supervisord creates the logs), so it is not in
# the tree and is added to the package as a bare directory entry.
_LOG_DIR_IN_TREE: Final[str] = "var/log/mngr-latchkey"
# What the sources must carry for a build to be worth shipping: the bootstrap,
# the control file, and files under every directory the tree places them in.
_REQUIRED_SOURCE_PATHS: Final[tuple[str, ...]] = (
    _BOOTSTRAP_TEMPLATE_NAME,
    f"{_TREE_DIR_NAME}/{_CONTROL_DIR_NAME}/control{_TEMPLATE_SUFFIX}",
)
_REQUIRED_SOURCE_DIRS_IN_TREE: Final[tuple[str, ...]] = (
    _INSTALL_DIR_IN_TREE,
    _BIN_DIR_IN_TREE,
    _SUPERVISOR_CONFD_DIR_IN_TREE,
    _NFTABLES_CONFD_DIR_IN_TREE,
    _SYSTEMD_UNIT_DIR_IN_TREE,
)

# Fixed timestamp every packaged file carries, so a build is reproducible.
_BUILD_EPOCH_SECONDS: Final[int] = 1_704_067_200
_EXECUTABLE_MODE: Final[int] = 0o755
_REGULAR_MODE: Final[int] = 0o644
_DIRECTORY_MODE: Final[int] = 0o755
_AR_MAGIC: Final[bytes] = b"!<arch>\n"
_DEBIAN_BINARY_VERSION: Final[bytes] = b"2.0\n"


def _build_template_environment() -> Environment:
    """How every template is rendered: strictly, keeping trailing newlines, with a ``shquote`` filter.

    Strict, so a template naming something the context lacks fails rather than
    shipping an empty string on a machine. A value that can carry arbitrary
    text (a message) is rendered through ``shquote``, so an apostrophe in it
    can never break the script that carries it; the paths and names, which
    this build fixes, are rendered inside literal single quotes.
    """
    environment = Environment(undefined=StrictUndefined, keep_trailing_newline=True, autoescape=False)
    environment.filters["shquote"] = shlex.quote
    return environment


_TEMPLATE_ENVIRONMENT: Final[Environment] = _build_template_environment()


class RemotePackageLayout(FrozenModel):
    """Where the package's files, and what its scripts touch, live on the machine.

    Each directory is the literal path the package tree places its files under,
    prefixed by the root the package is unpacked at: ``/`` on a real machine,
    which :data:`DEFAULT_REMOTE_PACKAGE_LAYOUT` describes, or a scratch
    directory in a test that unpacks the package there and runs its scripts.
    """

    install_dir: Path = Field(description="The package's private directory: its scripts and the extension.")
    bin_dir: Path = Field(description="Where the ``mngr-latchkey`` command lands.")
    secrets_dir: Path = Field(description="The RAM-backed directory the gateway's secrets live in.")
    log_dir: Path = Field(description="Where supervisord writes the gateway's and the tunnel's logs.")
    supervisor_confd_dir: Path = Field(description="supervisord's drop-in directory the program configs land in.")
    nftables_confd_dir: Path = Field(description="Where the bridge-services nftables policy lands.")
    systemd_unit_dir: Path = Field(description="Where the unit that loads that policy at boot lands.")
    curl_install_dir: Path = Field(description="Where the curl shims are installed.")
    artifact_dir: Path = Field(description="Where the built package is uploaded to before it is installed.")

    @model_validator(mode="after")
    def _validate_directories_match_the_package_tree(self) -> "RemotePackageLayout":
        """The scripts are rendered with these paths while dpkg unpacks the files at the tree's, so they must agree.

        Raises:
            RemoteGatewayError: when a directory the package tree places files
                in is not that tree path under some root.
        """
        for name, directory, path_in_tree in (
            ("install_dir", self.install_dir, _INSTALL_DIR_IN_TREE),
            ("bin_dir", self.bin_dir, _BIN_DIR_IN_TREE),
            ("log_dir", self.log_dir, _LOG_DIR_IN_TREE),
            ("supervisor_confd_dir", self.supervisor_confd_dir, _SUPERVISOR_CONFD_DIR_IN_TREE),
            ("nftables_confd_dir", self.nftables_confd_dir, _NFTABLES_CONFD_DIR_IN_TREE),
            ("systemd_unit_dir", self.systemd_unit_dir, _SYSTEMD_UNIT_DIR_IN_TREE),
        ):
            if not directory.as_posix().endswith(f"/{path_in_tree}"):
                raise RemoteGatewayError(
                    f"A remote package layout's {name} must be {path_in_tree} under the root the package is "
                    f"unpacked at, which is where its files land; got {directory}"
                )
        return self


DEFAULT_REMOTE_PACKAGE_LAYOUT: Final[RemotePackageLayout] = RemotePackageLayout(
    install_dir=Path("/usr/lib/mngr-latchkey"),
    bin_dir=Path("/usr/bin"),
    secrets_dir=TMPFS_SECRETS_DIR,
    log_dir=Path("/var/log/mngr-latchkey"),
    supervisor_confd_dir=Path("/etc/supervisor/conf.d"),
    nftables_confd_dir=Path("/etc/nftables.d"),
    systemd_unit_dir=Path("/etc/systemd/system"),
    curl_install_dir=Path("/usr/local/bin"),
    # Exists on every machine before anything is installed, which is what an
    # upload ahead of the install needs.
    artifact_dir=Path("/tmp"),
)


class RemotePackageContext(FrozenModel):
    """Everything the package templates are rendered with: the machine as this computer wants it."""

    layout: RemotePackageLayout = Field(description="Where things live on the machine.")
    latchkey_version: str = Field(description="The upstream latchkey CLI version the machine runs.")
    node_major_version: str = Field(description="The Node.js major the bootstrap installs from NodeSource.")
    minimum_node_major_version: int = Field(description="The oldest Node.js major accepted as already installed.")
    curl_shims_repo: str = Field(description="GitHub repository the curl shims are released from.")
    curl_shims_version: str = Field(description="The latchkey-curl-shims release the shims are fetched from.")
    curl_shims_by_uname_pattern: tuple[tuple[str, str, str], ...] = Field(
        description=(
            "The ``uname -m`` pattern (a ``case`` alternative), the target triple it selects, and the pinned "
            "sha256 of that triple's tarball, for every arch a machine can be."
        )
    )
    curl_router_bin: str = Field(description="Filename of the curl router inside the release tarball.")
    curl_impersonate_bin: str = Field(description="Filename of the impersonating curl inside the release tarball.")
    curl_router_path: Path = Field(description="Where the curl router is installed.")
    curl_impersonate_path: Path = Field(description="Where the impersonating curl is installed.")
    curl_staged_suffix: str = Field(description="Suffix new curl binaries are staged under before the swap.")
    curl_version_stamp_path: Path = Field(description="Records the release the installed curl shims came from.")
    outer_port: int = Field(
        description="Port the gateway binds on the machine's docker bridge address (the port the agent's container reaches it at)."
    )
    agent_side_port: int = Field(description="Loopback port the tunnel binds in the container.")
    desktop_gateway_vps_port: int = Field(description="Loopback port the desktop gateway is tunneled to.")
    max_body_size_bytes: int = Field(description="The gateway's request body limit.")
    remote_latchkey_dir_name: str = Field(description="The latchkey directory's name under the machine's home.")
    credentials_store_filename: str = Field(description="Upstream's credential store filename.")
    data_format_version_filename: str = Field(description="Upstream's format stamp filename.")
    permissions_filename: str = Field(description="The policy filename the gateway reads.")
    desktop_egress_rules_filename: str = Field(
        description="The file naming the services the curl router sends out through the desktop."
    )
    config_filename: str = Field(description="Upstream's config filename.")
    extensions_dir_name: str = Field(description="The gateway's extensions directory, under its latchkey directory.")
    extension_filename: str = Field(description="The forwarding extension's filename.")
    gateway_conf_filename: str = Field(description="The gateway's bind address file, under the latchkey directory.")
    container_tunnel_key_filename: str = Field(description="The tunnel's private key, under the latchkey directory.")
    tunnel_conf_filename: str = Field(description="The tunnel target file, under the latchkey directory.")
    encryption_key_filename: str = Field(description="The machine's own key, under the secrets directory.")
    listen_password_filename: str = Field(
        description="The machine's own listen password, under the secrets directory."
    )
    desktop_gateway_password_filename: str = Field(
        description="The desktop gateway's password, under the secrets directory."
    )
    desktop_permissions_override_filename: str = Field(
        description="The desktop-target JWT, under the secrets directory."
    )
    ram_backed_filesystem_types: tuple[str, ...] = Field(
        description="Filesystem types accepted for the secrets directory."
    )
    docker_bridge_interface_name: str = Field(description="The docker bridge interface the policy confines to.")
    bridge_service_ports: tuple[int, ...] = Field(description="The bridge-bound ports the policy confines.")
    nft_table_name: str = Field(description="The nftables table the bridge-services policy lives in.")
    nft_binary_path: str = Field(description="The nft binary the policy's unit loads it with.")
    nft_policy_path: Path = Field(description="Where the package places the policy file.")
    firewall_unit_name: str = Field(description="The systemd oneshot that loads the policy at boot.")
    gateway_program_name: str = Field(description="supervisord program name of the gateway.")
    tunnel_program_name: str = Field(description="supervisord program name of the reverse tunnel.")
    gateway_log_filename: str = Field(description="The gateway program's log, under the log directory.")
    tunnel_log_filename: str = Field(description="The tunnel program's log, under the log directory.")
    supervisor_start_seconds: int = Field(description="supervisord ``startsecs`` for both programs.")
    supervisor_gateway_start_retries: int = Field(description="supervisord ``startretries`` for the gateway.")
    supervisor_tunnel_start_retries: int = Field(description="supervisord ``startretries`` for the tunnel.")
    supervisor_log_max_bytes: str = Field(description="supervisord log rotation size.")
    supervisor_log_backups: int = Field(description="supervisord rotated log copies kept.")
    sh_binary_path: str = Field(description="The shell supervisord runs the wrappers with.")
    ssh_binary_path: str = Field(description="The ssh the tunnel wrapper execs.")
    ssh_server_alive_interval_seconds: int = Field(description="ssh ``ServerAliveInterval`` for the tunnel.")
    ssh_server_alive_count_max: int = Field(description="ssh ``ServerAliveCountMax`` for the tunnel.")
    ssh_connect_timeout_seconds: int = Field(description="ssh ``ConnectTimeout`` for the tunnel.")
    container_host_id_label: str = Field(description="Docker label the agent's container is found by.")
    different_key_message: str = Field(description="Why a key that differs from the machine's is refused.")
    different_password_message: str = Field(description="Why a password that differs from the machine's is refused.")
    outcome_done_marker: str = Field(description="The last stdout line of a script that ran to its end.")
    answer_prefix: str = Field(description="Prefix of every answer line ``read-state`` prints.")
    legacy_gateway_run_script_filename: str = Field(description="The wrapper an older build wrote beside the store.")
    legacy_extension_candidate_suffix: str = Field(description="Suffix an older build staged the extension under.")
    debian_dependencies: tuple[str, ...] = Field(
        description="The Debian packages the control file declares and the bootstrap installs first."
    )


class RemotePackageArtifact(FrozenModel):
    """A built package: the bytes to upload, and what they say they are."""

    version: str = Field(description="The Debian version the package carries.")
    filename: str = Field(description="The conventional ``<name>_<version>_<arch>.deb`` filename.")
    content: bytes = Field(description="The ``.deb`` file.")


class _PackageFile(FrozenModel):
    """One file of the package, rendered, at its path relative to the tree root."""

    path: str = Field(description="Tree-relative POSIX path, without any template suffix.")
    content: bytes = Field(description="The file's bytes.")

    @property
    def mode(self) -> int:
        return _EXECUTABLE_MODE if self.content.startswith(b"#!") else _REGULAR_MODE


@pure
def remote_package_context(layout: RemotePackageLayout) -> RemotePackageContext:
    """The context this build renders the package with, for a machine laid out as ``layout``."""
    return RemotePackageContext(
        layout=layout,
        latchkey_version=LATCHKEY_VERSION,
        node_major_version=NODE_MAJOR_VERSION,
        minimum_node_major_version=MINIMUM_NODE_MAJOR_VERSION,
        curl_shims_repo=CURL_SHIMS_REPO,
        curl_shims_version=CURL_SHIMS_VERSION,
        curl_shims_by_uname_pattern=tuple(
            (pattern, triple, CURL_SHIMS_SHA256_BY_TRIPLE[triple])
            for pattern, triple in CURL_SHIMS_TRIPLE_BY_UNAME_PATTERN
        ),
        curl_router_bin=CURL_ROUTER_BIN,
        curl_impersonate_bin=CURL_IMPERSONATE_BIN,
        curl_router_path=layout.curl_install_dir / CURL_ROUTER_BIN,
        curl_impersonate_path=layout.curl_install_dir / CURL_IMPERSONATE_BIN,
        curl_staged_suffix=CURL_STAGED_SUFFIX,
        curl_version_stamp_path=layout.curl_install_dir / CURL_VERSION_STAMP_FILENAME,
        outer_port=OUTER_PORT,
        agent_side_port=AGENT_SIDE_LATCHKEY_PORT,
        desktop_gateway_vps_port=DESKTOP_GATEWAY_VPS_PORT,
        max_body_size_bytes=GATEWAY_MAX_BODY_SIZE_BYTES,
        remote_latchkey_dir_name=REMOTE_LATCHKEY_DIR_NAME,
        credentials_store_filename=CREDENTIALS_STORE_FILENAME,
        data_format_version_filename=UPSTREAM_DATA_FORMAT_VERSION_FILENAME,
        permissions_filename=PERMISSIONS_CONFIG_FILENAME,
        desktop_egress_rules_filename=DESKTOP_EGRESS_RULES_FILENAME,
        config_filename=CONFIG_FILENAME,
        extensions_dir_name=REMOTE_EXTENSIONS_DIR_NAME,
        extension_filename=REMOTE_GATEWAY_EXTENSION_FILENAME,
        gateway_conf_filename=GATEWAY_CONF_FILENAME,
        container_tunnel_key_filename=CONTAINER_TUNNEL_KEY_FILENAME,
        tunnel_conf_filename=TUNNEL_CONF_FILENAME,
        encryption_key_filename=GATEWAY_ENCRYPTION_KEY_FILENAME,
        listen_password_filename=GATEWAY_LISTEN_PASSWORD_FILENAME,
        desktop_gateway_password_filename=DESKTOP_GATEWAY_PASSWORD_FILENAME,
        desktop_permissions_override_filename=DESKTOP_PERMISSIONS_OVERRIDE_FILENAME,
        ram_backed_filesystem_types=_RAM_BACKED_FILESYSTEM_TYPES,
        docker_bridge_interface_name=DOCKER_BRIDGE_INTERFACE_NAME,
        bridge_service_ports=_BRIDGE_SERVICE_PORTS,
        nft_table_name=BRIDGE_SERVICES_NFT_TABLE,
        nft_binary_path=NFT_BINARY_PATH,
        nft_policy_path=layout.nftables_confd_dir / BRIDGE_SERVICES_NFT_POLICY_FILENAME,
        firewall_unit_name=BRIDGE_SERVICES_FIREWALL_UNIT_NAME,
        gateway_program_name=GATEWAY_PROGRAM_NAME,
        tunnel_program_name=TUNNEL_PROGRAM_NAME,
        gateway_log_filename=REMOTE_GATEWAY_LOG_FILENAME,
        tunnel_log_filename=REMOTE_TUNNEL_LOG_FILENAME,
        supervisor_start_seconds=_SUPERVISOR_START_SECONDS,
        supervisor_gateway_start_retries=_SUPERVISOR_GATEWAY_START_RETRIES,
        supervisor_tunnel_start_retries=_SUPERVISOR_TUNNEL_START_RETRIES,
        supervisor_log_max_bytes=_SUPERVISOR_LOG_MAX_BYTES,
        supervisor_log_backups=_SUPERVISOR_LOG_BACKUPS,
        sh_binary_path=_SH_BINARY_PATH,
        ssh_binary_path=_SSH_BINARY_PATH,
        ssh_server_alive_interval_seconds=_SSH_SERVER_ALIVE_INTERVAL_SECONDS,
        ssh_server_alive_count_max=_SSH_SERVER_ALIVE_COUNT_MAX,
        ssh_connect_timeout_seconds=_SSH_CONNECT_TIMEOUT_SECONDS,
        container_host_id_label=CONTAINER_HOST_ID_LABEL,
        different_key_message=DIFFERENT_MACHINE_KEY_MESSAGE,
        different_password_message=DIFFERENT_MACHINE_PASSWORD_MESSAGE,
        outcome_done_marker=OUTCOME_DONE_MARKER,
        answer_prefix=ANSWER_PREFIX,
        legacy_gateway_run_script_filename=_LEGACY_GATEWAY_RUN_SCRIPT_FILENAME,
        legacy_extension_candidate_suffix=_LEGACY_EXTENSION_CANDIDATE_SUFFIX,
        debian_dependencies=_DEBIAN_DEPENDENCIES,
    )


def build_remote_package(context: RemotePackageContext) -> RemotePackageArtifact:
    """Render the package tree with ``context`` and assemble it into a ``.deb``.

    Raises:
        RemoteGatewayError: when the package sources cannot be read, or a
            template names something the context does not provide.
    """
    sources = _read_package_sources()
    extension_content = bundled_gateway_extension_content(REMOTE_GATEWAY_EXTENSION_FILENAME).encode("utf-8")
    version = _package_version(sources, extension_content, context)
    filename = f"{PACKAGE_NAME}_{version}_all.deb"
    variables = _template_variables(context, version, filename)
    rendered = [
        _PackageFile(path=_strip_template_suffix(path), content=_render(path, content, variables))
        for path, content in sorted(sources.items())
        if path.startswith(f"{_TREE_DIR_NAME}/")
    ]
    files = [
        _PackageFile(path=file.path.removeprefix(f"{_TREE_DIR_NAME}/"), content=file.content) for file in rendered
    ] + [
        _PackageFile(
            path=f"{_INSTALL_DIR_IN_TREE}/{REMOTE_EXTENSIONS_DIR_NAME}/{REMOTE_GATEWAY_EXTENSION_FILENAME}",
            content=extension_content,
        )
    ]
    control_files = [file for file in files if file.path.startswith(f"{_CONTROL_DIR_NAME}/")]
    data_files = [file for file in files if not file.path.startswith(f"{_CONTROL_DIR_NAME}/")]
    control_tar = _tarball(
        [
            _PackageFile(path=file.path.removeprefix(f"{_CONTROL_DIR_NAME}/"), content=file.content)
            for file in control_files
        ],
        empty_directories=(),
    )
    data_tar = _tarball(data_files, empty_directories=(_LOG_DIR_IN_TREE,))
    return RemotePackageArtifact(version=version, filename=filename, content=_ar_archive(control_tar, data_tar))


def render_bootstrap_script(context: RemotePackageContext, artifact: RemotePackageArtifact) -> str:
    """Render the POSIX ``sh`` script that installs ``artifact`` (already uploaded) on the machine.

    Raises:
        RemoteGatewayError: when the template cannot be read or rendered.
    """
    sources = _read_package_sources()
    variables = _template_variables(context, artifact.version, artifact.filename)
    return _render(_BOOTSTRAP_TEMPLATE_NAME, sources[_BOOTSTRAP_TEMPLATE_NAME], variables).decode("utf-8")


def _read_package_sources() -> dict[str, bytes]:
    """Every file under the package sources directory, keyed by its relative POSIX path.

    Raises:
        RemoteGatewayError: when the sources cannot be read, or lack part of
            the package.
    """
    root = resources.files(_SOURCES_PACKAGE)
    try:
        sources = _collect_sources(root, "")
    except OSError as e:
        raise RemoteGatewayError(f"Failed to read the remote package sources: {e}") from e
    _require_complete_sources(sources)
    return sources


def _collect_sources(directory: Traversable, prefix: str) -> dict[str, bytes]:
    """The files under ``directory`` (dotfiles and ``__pycache__`` skipped), keyed by ``prefix`` plus their relative path."""
    sources: dict[str, bytes] = {}
    for entry in directory.iterdir():
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue
        path = f"{prefix}{entry.name}"
        if entry.is_dir():
            sources.update(_collect_sources(entry, f"{path}/"))
        else:
            sources[path] = entry.read_bytes()
    return sources


@pure
def _require_complete_sources(sources: Mapping[str, bytes]) -> None:
    """Refuse sources that would build a package the machine cannot run.

    An installed copy of this plugin that lost part of the tree (a build that
    dropped the gitignored ``usr/lib/``) would otherwise ship a package whose
    scripts source files it does not carry, and fail on the machine rather
    than here.

    Raises:
        RemoteGatewayError: naming what the sources lack.
    """
    missing = [path for path in _REQUIRED_SOURCE_PATHS if path not in sources]
    for directory in _REQUIRED_SOURCE_DIRS_IN_TREE:
        prefix = f"{_TREE_DIR_NAME}/{directory}/"
        if not any(path.startswith(prefix) for path in sources):
            missing.append(f"{prefix}*")
    if missing:
        raise RemoteGatewayError(f"The remote package sources lack {', '.join(missing)}")


def _package_version(sources: Mapping[str, bytes], extension_content: bytes, context: RemotePackageContext) -> str:
    """``<distribution version>+<hash>``, the hash covering every input of the build.

    The hash is over the sources, the extension, and the context rather than
    over the rendered output, because the rendered scripts carry the version.
    """
    digest = hashlib.sha256()
    for path, content in sorted(sources.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    digest.update(extension_content)
    digest.update(b"\0")
    digest.update(context.model_dump_json().encode("utf-8"))
    return f"{_distribution_version()}+{digest.hexdigest()[:_VERSION_HASH_CHARS]}"


def _distribution_version() -> str:
    try:
        return importlib.metadata.version(_DISTRIBUTION_NAME)
    except importlib.metadata.PackageNotFoundError as e:
        raise RemoteGatewayError(f"Cannot version the remote package: {_DISTRIBUTION_NAME} is not installed") from e


@pure
def _template_variables(context: RemotePackageContext, version: str, filename: str) -> dict[str, object]:
    return {
        **context.model_dump(),
        "package_name": PACKAGE_NAME,
        "package_version": version,
        "artifact_path": context.layout.artifact_dir / filename,
    }


def _render(path: str, content: bytes, variables: Mapping[str, object]) -> bytes:
    """Render ``content`` if ``path`` names a template, else return it as it is.

    Raises:
        RemoteGatewayError: when the template refers to something ``variables`` lacks.
    """
    if not path.endswith(_TEMPLATE_SUFFIX):
        return content
    try:
        return _TEMPLATE_ENVIRONMENT.from_string(content.decode("utf-8")).render(variables).encode("utf-8")
    except (TemplateError, UnicodeDecodeError) as e:
        raise RemoteGatewayError(f"Failed to render the remote package template {path}: {e}") from e


@pure
def _strip_template_suffix(path: str) -> str:
    return path.removesuffix(_TEMPLATE_SUFFIX)


@pure
def _tarball(files: Sequence[_PackageFile], empty_directories: tuple[str, ...]) -> bytes:
    """A reproducible gzipped tar of ``files`` (and their parent directories), as dpkg expects it."""
    directories: set[str] = set(empty_directories)
    for path in (*empty_directories, *(file.path for file in files)):
        directories.update(_parent_directories(path))
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=_BUILD_EPOCH_SECONDS) as gzipped:
        with tarfile.open(fileobj=gzipped, mode="w", format=tarfile.GNU_FORMAT) as tar:
            tar.addfile(_tar_entry("./", _DIRECTORY_MODE, tarfile.DIRTYPE, 0))
            for directory in sorted(directories):
                tar.addfile(_tar_entry(f"./{directory}/", _DIRECTORY_MODE, tarfile.DIRTYPE, 0))
            for file in sorted(files, key=lambda entry: entry.path):
                tar.addfile(
                    _tar_entry(f"./{file.path}", file.mode, tarfile.REGTYPE, len(file.content)),
                    io.BytesIO(file.content),
                )
    return buffer.getvalue()


@pure
def _parent_directories(path: str) -> set[str]:
    """Every directory above ``path`` (tree-relative), so the tarball lists each one it creates."""
    parents: set[str] = set()
    parent = Path(path).parent.as_posix()
    while parent != ".":
        parents.add(parent)
        parent = Path(parent).parent.as_posix()
    return parents


@pure
def _tar_entry(name: str, mode: int, entry_type: bytes, size: int) -> tarfile.TarInfo:
    entry = tarfile.TarInfo(name)
    entry.mode = mode
    entry.type = entry_type
    entry.size = size
    entry.mtime = _BUILD_EPOCH_SECONDS
    entry.uid = 0
    entry.gid = 0
    entry.uname = "root"
    entry.gname = "root"
    return entry


@pure
def _ar_archive(control_tar: bytes, data_tar: bytes) -> bytes:
    """The ``.deb`` container: an ``ar`` archive of the version stamp and the two tarballs, in that order."""
    return _AR_MAGIC + b"".join(
        _ar_member(name, content)
        for name, content in (
            ("debian-binary", _DEBIAN_BINARY_VERSION),
            ("control.tar.gz", control_tar),
            ("data.tar.gz", data_tar),
        )
    )


@pure
def _ar_member(name: str, content: bytes) -> bytes:
    header = (f"{name:<16}{_BUILD_EPOCH_SECONDS:<12}{0:<6}{0:<6}{_REGULAR_MODE:<8o}{len(content):<10}`\n").encode(
        "ascii"
    )
    padding = b"\n" if len(content) % 2 else b""
    return header + content + padding
