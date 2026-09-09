"""Shared plumbing for talking to the machine a remote host's agents run on.

Both provisioning a machine's gateway and moving credentials to and from it
run shell commands on the machine over its outer host. The pieces they share
live here: where the machine keeps its latchkey directory, where its gateway
secrets live (a RAM-backed tmpfs directory, so a reboot wipes them rather than
ever leaving the encryption key on the persistent disk), and how a ``latchkey``
invocation is run under the machine's own key without that key ever appearing
in a process listing.
"""

import shlex
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import SecretStr
from pydantic import SkipValidation

from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr_latchkey.core import summarize_latchkey_failure
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError

# Name of the latchkey directory on the VPS, under the remote user's home. The
# remote latchkey CLI runs as that user, so ``$HOME/.latchkey`` is the
# LATCHKEY_DIRECTORY it reads its credentials and permissions from. Re-exported
# by :mod:`imbue.mngr_latchkey.remote.provisioning` because consumers outside
# this plugin read the gateway's logs at this location.
REMOTE_LATCHKEY_DIR_NAME: Final[str] = ".latchkey"

# Quick remote command (e.g. resolving ``$HOME``); a few seconds of slack
# covers a cold SSH channel without masking a hung connection.
REMOTE_COMMAND_TIMEOUT_SECONDS: Final[float] = 15.0

# A ``latchkey`` invocation on the machine (re-encrypting its store, clearing an
# account). Node startup plus a store rewrite is a second or two in the steady
# state; the wider budget covers a loaded VPS without letting a wedged channel
# hold the reconcile open indefinitely.
REMOTE_LATCHKEY_TIMEOUT_SECONDS: Final[float] = 60.0

# Mode for files we drop on the VPS. Both the encrypted credentials and the
# permissions config are owned by the remote (root) user the gateway runs as;
# 0600 matches the local ``save_permissions`` chmod and keeps secrets private.
REMOTE_FILE_MODE: Final[str] = "0600"

# tmpfs (RAM-backed) directory holding the gateway's secrets: the machine's own
# encryption key and listen password, plus the pair the forwarding extension
# presents to the desktop it proxies to. ``/run`` is the FHS location for runtime
# state, is root-owned, and is a tmpfs under systemd (which we already require
# for the supervisor service), so it is wiped on reboot -- the key is never
# persisted to the VPS disk beside the encrypted credential store (which would
# be equivalent to storing the credentials in plaintext against a disk-snapshot
# threat model), yet it survives a process crash so supervisord can restart the
# gateway without a desktop round-trip. Provisioning verifies this is really a
# RAM-backed filesystem before writing to it. The gateway's wrapper script reads
# these 0600 files into the environment and execs the gateway, so the secrets
# never appear in the supervisord config or a process listing.
#
# Deliberately not ``/tmp``: unlike ``/run``, ``/tmp`` is not reliably a tmpfs
# (on many distros, incl. common Debian/Ubuntu VPS images, it is a normal
# directory on the root disk and is cleaned by age rather than wiped on boot),
# so the key could land on -- and survive on -- persistent disk. ``/tmp`` is
# also world-writable (1777), exposing the classic hostile-symlink attack that a
# root-owned ``/run`` avoids.
TMPFS_SECRETS_DIR: Final[Path] = Path("/run/mngr-latchkey")

# The machine's own two secrets: the key its credential store is encrypted with,
# and the listen password its gateway holds every caller to. The password
# belongs to the machine rather than to any one of the user's computers: it is
# what the workspaces on it were created with (their host env file is written
# once, at ``mngr create``), so it can never be replaced -- see
# :func:`~imbue.mngr_latchkey.remote.provisioning._resolve_machine_gateway_password`.
GATEWAY_ENCRYPTION_KEY_FILENAME: Final[str] = "gateway_encryption_key"
GATEWAY_LISTEN_PASSWORD_FILENAME: Final[str] = "gateway_listen_password"

# The desktop-owned pair, read afresh by the forwarding extension on every
# request it proxies to the desktop gateway: that gateway's own listen password,
# and a JWT (signed by that computer's key, naming a path on its disk) targeting
# the host's desktop-side permissions file. Both belong to whichever of the
# user's computers is currently connected, so every provisioning pass overwrites
# them -- unlike the machine's own secrets above, which are adopted.
DESKTOP_GATEWAY_PASSWORD_FILENAME: Final[str] = "desktop_gateway_password"
DESKTOP_PERMISSIONS_OVERRIDE_FILENAME: Final[str] = "desktop_permissions_override"

# Why an operation against a machine is refused when the key its gateway runs
# under is not the one this computer recorded.
DIFFERENT_MACHINE_KEY_MESSAGE: Final[str] = (
    "the machine keeps its credentials under a different key than this computer recorded "
    "(re-keyed from another computer?); the next provisioning pass adopts it"
)


def resolve_remote_latchkey_directory(host: OuterHostInterface) -> Path:
    """Resolve ``$HOME/.latchkey`` on the VPS to an absolute path.

    ``write_file`` transfers over SFTP with a literal path, so ``~`` is not
    expanded for us; we ask the remote shell for ``$HOME`` and build the
    absolute latchkey directory from it.
    """
    result = host.execute_idempotent_command('echo "$HOME"', timeout_seconds=REMOTE_COMMAND_TIMEOUT_SECONDS)
    home = result.stdout.strip()
    if not result.success or not home:
        raise RemoteGatewayError(
            "Failed to resolve $HOME on VPS {}: {}".format(
                host.get_name(), result.stderr.strip() or result.stdout.strip() or "empty $HOME"
            )
        )
    return Path(home) / REMOTE_LATCHKEY_DIR_NAME


class RemoteLatchkeyDirectory(MutableModel):
    """The machine's ``~/.latchkey``, resolved over the network at most once -- and only if a step asks for it.

    A reconcile pass shares one of these across its steps: the adopt passes,
    which address the directory from Python (over SFTP), pay the ``$HOME``
    round trip once between them, while every step that runs entirely as a
    remote script -- which is everything this computer pushes -- expands
    ``$HOME`` itself and never pays it.
    """

    model_config = {"arbitrary_types_allowed": True}

    host: SkipValidation[OuterHostInterface] = Field(
        frozen=True, description="The machine's outer host, already open."
    )
    resolved_path: Path | None = Field(
        default=None,
        description="The absolute directory once resolved, or given up front by a caller that already knows it.",
    )

    def resolve(self) -> Path:
        if self.resolved_path is None:
            self.resolved_path = resolve_remote_latchkey_directory(self.host)
        return self.resolved_path


def _read_secrets_dir_file(host: OuterHostInterface, filename: str, description: str) -> str | None:
    """Return the stripped content of one of the machine's tmpfs secrets, or ``None`` when it holds none.

    Raises:
        RemoteGatewayError: when the file exists but cannot be read.
    """
    path = TMPFS_SECRETS_DIR / filename
    if not host.path_exists(path):
        return None
    try:
        content = host.read_text_file(path)
    except (OSError, MngrError) as e:
        raise RemoteGatewayError(f"Failed to read {description} on VPS {host.get_name()}: {e}") from e
    return content.strip() or None


def read_machine_key_from_secrets_dir(host: OuterHostInterface) -> SecretStr | None:
    """Return the key the machine's gateway is running under, or ``None`` when its tmpfs holds none.

    This is the machine's own copy of its key, and the source of truth while it
    exists (a reboot wipes it, which is what makes the desktops' recorded copies
    worth keeping): any of the user's computers may have provisioned the
    machine, so what this computer recorded is only a mirror of what the
    machine actually runs with.

    Raises:
        RemoteGatewayError: when the key file exists but cannot be read.
    """
    key = _read_secrets_dir_file(host, GATEWAY_ENCRYPTION_KEY_FILENAME, "the machine encryption key")
    return SecretStr(key) if key is not None else None


def read_machine_gateway_password_from_secrets_dir(host: OuterHostInterface) -> str | None:
    """Return the listen password the machine's gateway is running under, or ``None`` when its tmpfs holds none.

    Read for the same reason the key is: it is the machine's own, and the
    machine is the only place a second computer can learn it from -- the
    workspaces it serves present it from an env file written once at creation,
    so a computer that decided a password of its own here would lock them out.

    Raises:
        RemoteGatewayError: when the password file exists but cannot be read.
    """
    return _read_secrets_dir_file(host, GATEWAY_LISTEN_PASSWORD_FILENAME, "the machine gateway listen password")


def write_machine_key_to_secrets_dir(host: OuterHostInterface, machine_key: SecretStr) -> None:
    """Make sure the machine's key is where its CLI invocations read it from.

    It lives in RAM (see :data:`TMPFS_SECRETS_DIR`), so a reboot leaves it
    behind; writing it back costs one round trip and turns "the machine rebooted
    since it was provisioned" from a failed reconcile into a working one.

    Never a blind overwrite: a *different* key already there means the machine
    was re-keyed out from under this computer's record (by another of the
    user's computers), and replacing it would break the gateway running with it
    and every transfer that machine can still serve. The operation fails
    instead, and stays failed until the next provisioning pass adopts the
    machine's key into this computer's record.

    Raises:
        RemoteGatewayError: when the machine already runs under a different key,
            or its tmpfs copy cannot be read.
    """
    existing_key = read_machine_key_from_secrets_dir(host)
    if existing_key is not None:
        if existing_key.get_secret_value() != machine_key.get_secret_value():
            raise RemoteGatewayError(f"Refusing to operate on VPS {host.get_name()}: {DIFFERENT_MACHINE_KEY_MESSAGE}")
        return
    host.write_file(
        TMPFS_SECRETS_DIR / GATEWAY_ENCRYPTION_KEY_FILENAME,
        machine_key.get_secret_value().encode("utf-8"),
        mode=REMOTE_FILE_MODE,
    )


def run_remote_latchkey(
    host: OuterHostInterface,
    setup: str,
    command: str,
    *,
    out_key: SecretStr,
    failure_description: str,
) -> None:
    """Run a ``latchkey`` command on the machine, under its own encryption key.

    The key is read from its tmpfs file by the shell rather than passed as an
    argument or an environment assignment we transmit, so it never appears in a
    process listing on the machine. ``out_key`` -- the key a ``re-encrypt``
    should write *out* with -- is the one secret that does travel, and it goes in
    a shell variable (``$_lk_out_key``) that the command pipes to the CLI's
    stdin, never in argv. ``setup`` is whatever shell has to run first.
    """
    key_file_q = shlex.quote(str(TMPFS_SECRETS_DIR / GATEWAY_ENCRYPTION_KEY_FILENAME))
    lines = [
        "set -e",
        f'LATCHKEY_ENCRYPTION_KEY="$(cat {key_file_q})"',
        "export LATCHKEY_ENCRYPTION_KEY",
        f"_lk_out_key={shlex.quote(out_key.get_secret_value())}",
        setup,
        command,
    ]
    run_remote_command(host, "\n".join(lines), failure_description=failure_description, is_secret_bearing=True)


def run_remote_command(
    host: OuterHostInterface,
    script: str,
    *,
    failure_description: str,
    is_secret_bearing: bool = False,
) -> None:
    """Run ``script`` on the machine, raising with its own explanation if it fails."""
    result = host.execute_idempotent_command(script, timeout_seconds=REMOTE_LATCHKEY_TIMEOUT_SECONDS)
    if not result.success:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RemoteGatewayError(
            "Failed to {} on VPS {}: {}".format(
                failure_description,
                host.get_name(),
                summarize_latchkey_failure(detail, "the command reported no reason") if is_secret_bearing else detail,
            )
        )


def remove_remote_path(host: OuterHostInterface, path: Path) -> None:
    """Remove a scratch path, reporting rather than raising: the work it served is already done."""
    result = host.execute_idempotent_command(
        f"rm -rf {shlex.quote(str(path))}", timeout_seconds=REMOTE_COMMAND_TIMEOUT_SECONDS
    )
    if not result.success:
        logger.warning("Could not remove {} on VPS {}: {}", path, host.get_name(), result.stderr.strip())
