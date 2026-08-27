"""Reaching a remote agent with unison: roots, SSH transport, and the remote binary.

unison is not like rsync or git, which mngr can point at a remote path and be done
with. It is a client/server protocol: the local ``unison`` starts a *second* unison
on the far side over SSH and talks to it. So pairing with a remote agent needs three
things this module provides -- an ``ssh://`` root string, a transport that carries
mngr's key and port, and a usable unison on the host.

Two separate reasons the host's unison cannot simply be assumed:

*Version.* Since 2.52 unison interoperates across versions freely (manual, "Version
interoperability"), but anything older than 2.52 has *no* interop with a modern client
-- and Ubuntu 22.04 still ships 2.51.5.

*The watcher.* Continuous sync (``-repeat watch``) needs a ``unison-fsmonitor`` helper
for each replica, and the far side's runs on the host. The unison packaged by Debian
and Ubuntu ships none, and neither archive has one to install, so a perfectly new
distro unison still syncs once and then dies with "Server: No file monitoring helper
program found".

So mngr checks for both rather than for the mere presence of the binary, and installs
a pinned static build -- which carries the watcher next to it -- when what is there is
unusable.
"""

import re
import shlex
import stat
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import model_validator

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import MngrError
from imbue.mngr.hosts.common import build_ssh_transport_command
from imbue.mngr.hosts.common import get_ssh_known_hosts_file
from imbue.mngr.interfaces.host import OnlineHostInterface

# Both ends must be at least this version or the protocol simply will not negotiate.
# See the interop matrix in the unison manual under "Version interoperability".
MINIMUM_UNISON_VERSION: Final[tuple[int, int]] = (2, 52)

# The build mngr installs on a host that has no usable unison: a fully static Linux
# x86_64 ELF, so it runs on any glibc/musl Linux without sudo -- and nowhere else.
# Pinned rather than tracking latest so the checksum below stays meaningful. Upstream
# publishes Linux binaries for x86_64 only (there has never been an aarch64 asset),
# which is why ``ensure_remote_unison`` gives up rather than installing on any other
# platform.
REMOTE_UNISON_VERSION: Final[str] = "2.54.0"
REMOTE_UNISON_SHA256: Final[str] = "d279dff18682c909d3ddb0b280ab151229b4798b9399b1d227084da424337d24"

_INSTALL_TIMEOUT_SECONDS: Final[float] = 180.0
_VERSION_PROBE_TIMEOUT_SECONDS: Final[float] = 30.0

# ``unison -version`` prints e.g. "unison version 2.54.0 (ocaml 5.4.1)".
_VERSION_RE: Final[re.Pattern[str]] = re.compile(r"version\s+(\d+)\.(\d+)")

_RESULT_PREFIX: Final[str] = "MNGR_UNISON "
_UNSUPPORTED_PREFIX: Final[str] = "MNGR_UNISON_UNSUPPORTED "


class UnisonVersionError(MngrError):
    """Raised when a unison binary is missing, too old, or has no file watcher with it."""

    user_help_text = (
        "unison 2.52 or newer is required on both ends. Versions older than 2.52 cannot "
        "talk to a modern unison at all. Continuous sync also needs the unison-fsmonitor "
        "helper alongside each unison. On macOS: brew install unison and "
        "brew install autozimu/formulas/unison-fsmonitor. Elsewhere, take both binaries "
        "from a release at https://github.com/bcpierce00/unison/releases -- the unison "
        "packaged by Debian and Ubuntu includes no unison-fsmonitor, and Ubuntu 22.04's "
        "is 2.51, which is too old regardless."
    )


class SshEndpoint(FrozenModel):
    """Everything needed to reach one host over SSH, as mngr has it configured."""

    user: str = Field(description="SSH username")
    hostname: str = Field(description="SSH hostname or IP")
    port: int = Field(description="SSH port")
    key_path: Path = Field(description="Path to the private key mngr uses for this host")
    known_hosts_path: Path | None = Field(
        default=None, description="known_hosts file pinning this host's key, when one is configured"
    )

    @classmethod
    def from_host(cls, host: OnlineHostInterface) -> "SshEndpoint | None":
        """Build an endpoint for ``host``, or None when the host is local.

        A host that is not local has to supply connection info. Without it there is
        no way to reach its replica, and the alternative -- treating "no endpoint"
        as "local" -- would render the agent's path as a plain local root, so unison
        would sync a same-named directory on this machine instead, silently.
        """
        info = host.get_ssh_connection_info()
        if info is None:
            if not host.is_local:
                raise MngrError(
                    "The agent's host is remote but has no SSH connection info, so mngr cannot reach its files."
                )
            return None
        user, hostname, port, key_path = info
        return cls(
            user=user,
            hostname=hostname,
            port=port,
            key_path=key_path,
            known_hosts_path=get_ssh_known_hosts_file(host),
        )

    def as_transport_command(self) -> str:
        """The ``ssh ...`` prefix mngr uses for this host, shell-quoted."""
        return build_ssh_transport_command(self.key_path, self.port, self.known_hosts_path)


class UnisonRoot(FrozenModel):
    """One side of a unison sync: a local path, or an ``ssh://`` root on a host."""

    path: Path = Field(
        description="Path to the directory being synced; absolute for a remote root, as typed for a local one"
    )
    ssh: SshEndpoint | None = Field(default=None, description="SSH endpoint, or None when the path is local")

    @model_validator(mode="after")
    def _validate_remote_path_is_absolute(self) -> "UnisonRoot":
        # The doubled slash ``as_root_arg`` relies on comes from the path itself, so a
        # relative one would render an otherwise valid-looking root that unison resolves
        # against the remote home directory -- a different directory, silently. A local
        # root has no such trap and stays as typed (``mngr pair --target ./local-dir``).
        if self.ssh is not None and not self.path.is_absolute():
            raise MngrError(f"A remote unison root needs an absolute path, got: {self.path}")
        return self

    @property
    def is_remote(self) -> bool:
        return self.ssh is not None

    def as_root_arg(self) -> str:
        """Render this root exactly as unison expects it on the command line.

        The doubled slash after the hostname is unison's absolute-path marker; a
        single slash would be read as relative to the remote home directory. Note
        there is nowhere in this syntax to put a port -- that travels with the SSH
        transport instead, via ``-sshcmd``.
        """
        if self.ssh is None:
            return str(self.path)
        return f"ssh://{self.ssh.user}@{self.ssh.hostname}/{self.path}"


def parse_unison_version(output: str) -> tuple[int, int] | None:
    """Extract ``(major, minor)`` from ``unison -version`` output, or None if unparseable."""
    match = _VERSION_RE.search(output)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def is_version_compatible(version: tuple[int, int] | None) -> bool:
    """Whether a parsed version can talk to the unison versions mngr supports."""
    if version is None:
        return False
    return version >= MINIMUM_UNISON_VERSION


def check_local_unison_version(cg: ConcurrencyGroup) -> tuple[int, int]:
    """Return the local unison's ``(major, minor)``, raising if it is missing or too old.

    The local binary is the client half of the protocol, so there is no configuration
    in which mngr can substitute for it -- unlike the remote half, which mngr installs.
    """
    try:
        result = cg.run_process_to_completion(["unison", "-version"], timeout=_VERSION_PROBE_TIMEOUT_SECONDS)
    except (OSError, ProcessError) as e:
        raise UnisonVersionError(f"Could not run 'unison -version' locally: {e}") from e

    version = parse_unison_version(result.stdout)
    if not is_version_compatible(version):
        raise UnisonVersionError(
            f"Local unison is too old to pair: found {result.stdout.strip() or 'an unparseable version'}, "
            f"need {MINIMUM_UNISON_VERSION[0]}.{MINIMUM_UNISON_VERSION[1]} or newer."
        )
    assert version is not None, "is_version_compatible rejects None"
    return version


def build_remote_unison_script() -> str:
    """POSIX sh that resolves a usable unison on a host, installing one only if needed.

    Emits exactly one result line: ``MNGR_UNISON <path> <version>`` on success, or
    ``MNGR_UNISON_UNSUPPORTED <os>/<arch>`` when the pinned build cannot run on the
    host. Runs as a single command because every step is a network round trip.

    The order matters: an already-present usable unison is used as-is, a
    previously-installed mngr copy is next, and only then does anything get
    downloaded. "Usable" means new enough *and* accompanied by a ``unison-fsmonitor``,
    since ``-repeat watch`` needs a watcher on the far side; a distro unison has none,
    so on a host that has never been paired with the download rung is the usual one.

    Note that nothing sets ``UNISON`` for the server, so its archive files land in
    the agent user's ``~/.unison``. That is deliberate: unison names archives by a
    hash of the two roots, so mngr's cannot collide with a user's own, and relocating
    them would mean wrapping ``-servercmd`` in a shell just to set an environment
    variable.

    Note there is no version-stamp file. Every candidate -- including the one this
    script has just installed -- is checked by running ``-version`` on it, which is
    cheap here and strictly more truthful than a stamp: it also catches a binary
    that is present but broken, where a stamp would claim everything is fine.
    """
    # A tarball whose bin/ holds both `unison` and `unison-fsmonitor`, extracted under
    # a single version-named directory -- hence the `*/bin/` glob below.
    tarball = f"unison-{REMOTE_UNISON_VERSION}-ubuntu-22.04-x86_64-static.tar.gz"
    url = f"https://github.com/bcpierce00/unison/releases/download/v{REMOTE_UNISON_VERSION}/{tarball}"
    min_major, min_minor = MINIMUM_UNISON_VERSION
    return f"""
set -eu
VER={shlex.quote(REMOTE_UNISON_VERSION)}
SHA={shlex.quote(REMOTE_UNISON_SHA256)}
DIR="$HOME/.mngr/bin"

is_compatible() {{
    maj=${{1%%.*}}
    rest=${{1#*.}}
    min=${{rest%%.*}}
    case "$maj" in ''|*[!0-9]*) return 1 ;; esac
    case "$min" in ''|*[!0-9]*) return 1 ;; esac
    [ "$maj" -gt {min_major} ] && return 0
    [ "$maj" -eq {min_major} ] && [ "$min" -ge {min_minor} ] && return 0
    return 1
}}

# ``-repeat watch`` needs a file watcher for the *remote* replica too, and the server
# runs it there. So a candidate is only usable if a ``unison-fsmonitor`` comes with it:
# unison looks beside its own binary and on PATH, and finds it either way. This is not
# a precaution -- Debian's and Ubuntu's unison packages ship no watcher at all, and
# neither archive has one to install, so an otherwise-new distro unison syncs once and
# then dies with "Server: No file monitoring helper program found". Rejecting it here
# falls through to the install rung, which lays both binaries down together.
has_watcher() {{
    candidate_dir=${{1%/*}}
    [ -x "$candidate_dir/unison-fsmonitor" ] && return 0
    command -v unison-fsmonitor >/dev/null 2>&1 && return 0
    return 1
}}

report_if_usable() {{
    v=$("$1" -version 2>/dev/null | awk '{{print $3}}') || return 1
    [ -n "$v" ] || return 1
    is_compatible "$v" || return 1
    has_watcher "$1" || return 1
    printf '{_RESULT_PREFIX}%s %s\\n' "$1" "$v"
    exit 0
}}

if command -v unison >/dev/null 2>&1; then
    report_if_usable "$(command -v unison)" || true
fi
if [ -x "$DIR/unison" ]; then
    report_if_usable "$DIR/unison" || true
fi

# The pinned asset is a static Linux ELF, so the architecture alone is not enough to
# go on: an x86_64 macOS or BSD host would happily download it and end up with a
# binary it cannot exec.
platform="$(uname -s)/$(uname -m)"
case "$platform" in
    Linux/x86_64|Linux/amd64) ;;
    *) printf '{_UNSUPPORTED_PREFIX}%s\\n' "$platform"; exit 0 ;;
esac

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL --retry 3 --retry-delay 2 -o "$tmp/unison.tar.gz" {shlex.quote(url)}
if command -v sha256sum >/dev/null 2>&1; then
    printf '%s  %s\\n' "$SHA" "$tmp/unison.tar.gz" | sha256sum -c - >/dev/null
else
    printf '%s  %s\\n' "$SHA" "$tmp/unison.tar.gz" | shasum -a 256 -c - >/dev/null
fi
tar -xzf "$tmp/unison.tar.gz" -C "$tmp"
mkdir -p "$DIR"
install -m 0755 "$tmp"/*/bin/unison "$DIR/unison.new"
install -m 0755 "$tmp"/*/bin/unison-fsmonitor "$DIR/unison-fsmonitor.new"
mv -f "$DIR/unison.new" "$DIR/unison"
mv -f "$DIR/unison-fsmonitor.new" "$DIR/unison-fsmonitor"

# Report the installed binary the same way as one that was already there: by running
# it. report_if_usable exits on success, so reaching the line below means the install
# produced something that cannot run on this host, which has to fail loudly rather
# than hand unison a -servercmd path it will choke on much later.
report_if_usable "$DIR/unison" || true
printf 'mngr installed unison %s at %s but it does not run on this host\\n' "$VER" "$DIR/unison" >&2
exit 1
""".strip()


def ensure_remote_unison(host: OnlineHostInterface) -> Path:
    """Return the path to a unison on ``host`` that can talk to the local one.

    "Usable" means unison ``MINIMUM_UNISON_VERSION`` or newer *with* a
    ``unison-fsmonitor`` beside it or on the host's PATH, because ``-repeat watch``
    needs a watcher on the far side as well. Installs a pinned static build (which
    carries both) if nothing usable is present. Raises :class:`UnisonVersionError`
    when that build -- a static Linux x86_64 ELF -- cannot run on the host, notably
    Linux arm64, for which unison has never published a binary.
    """
    result = host.execute_idempotent_command(
        f"sh -c {shlex.quote(build_remote_unison_script())}",
        timeout_seconds=_INSTALL_TIMEOUT_SECONDS,
    )
    if not result.success:
        raise UnisonVersionError(f"Failed to resolve unison on the remote host: {result.stderr.strip()}")

    for line in result.stdout.splitlines():
        if line.startswith(_RESULT_PREFIX):
            remote_path, _, version = line[len(_RESULT_PREFIX) :].strip().partition(" ")
            logger.debug("Using unison {} ({}) on the remote host", remote_path, version)
            return Path(remote_path)
        if line.startswith(_UNSUPPORTED_PREFIX):
            platform = line[len(_UNSUPPORTED_PREFIX) :].strip()
            raise UnisonVersionError(
                f"The remote host has no unison {MINIMUM_UNISON_VERSION[0]}.{MINIMUM_UNISON_VERSION[1]}+ "
                "with a 'unison-fsmonitor' watcher alongside it (continuous sync needs a watcher on "
                f"both ends), and mngr only installs one for Linux x86_64, not for {platform} "
                "(upstream publishes no Linux arm64 build at all). Note that the unison packaged by "
                "Debian and Ubuntu ships no watcher, so a new enough unison on the host is not by "
                "itself enough. Install unison and unison-fsmonitor on the host and mngr will use them."
            )
    raise UnisonVersionError(f"Could not determine the remote unison path from: {result.stdout.strip()}")


def write_ssh_wrapper_script(endpoint: SshEndpoint, directory: Path) -> Path:
    """Write an executable ``ssh`` wrapper carrying mngr's options, and return its path.

    This exists because unison's ``-sshargs`` is split on bare whitespace with no
    shell-quote processing -- it execs ssh directly, no shell involved -- so a key
    path containing a space is torn into several arguments and cannot be escaped.
    Putting the options in a script instead means a shell does interpret the quoting
    that ``build_ssh_transport_command`` already applies.

    ``"$@"`` matters: unison appends its own arguments (``-l <user> <host> -e none
    <servercmd> -server ...``) after whatever ``-sshcmd`` names. Those land after
    the destination, which looks wrong but is fine -- OpenSSH re-enters its option
    loop once it has consumed the hostname.
    """
    directory.mkdir(parents=True, exist_ok=True)
    script_path = directory / "ssh-wrapper.sh"
    script_path.write_text(f'#!/bin/sh\nexec {endpoint.as_transport_command()} "$@"\n')
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)
    return script_path
