"""How this computer talks to the machine a remote host's agents run on.

Once the package built by :mod:`imbue.mngr_latchkey.remote.package` is
installed on a machine, everything this computer does to it goes through the
two commands the package provides, each one remote command:

* ``mngr-latchkey read-state`` assembles what the machine holds -- the secrets
  its gateway runs under, its config, the policy it enforces, the desktop
  egress rules its curl router reads, how the agent's container was created
  (when asked about one), and (when asked) its credential store, still
  encrypted under the machine's own key -- and prints it as prefixed base64
  lines (:func:`read_remote_state`).
* ``mngr-latchkey apply-state`` makes the machine match a document of what this
  computer wants it to hold, applied in one ``set -e`` script on the machine
  (:func:`apply_remote_state`).

The document travels on the command's stdin (a quoted heredoc in the one
command string), as ``<key> <base64>`` lines, so a payload is visibly data
rather than code: the shell never interprets it, and no entry reaches the
``argv`` of ``mngr-latchkey``. Its scripts read each entry from a file under
the machine's RAM-backed scratch directory, and of the ``latchkey``
invocations they make only the service and account names travel in ``argv``,
never a key, a password or a store (those go by file, stdin or the
environment). Neither is the machine's own key ever spelled into a command: a
script that runs ``latchkey`` reads the key from the machine's RAM-backed
secrets directory itself, a read answers with it the same base64 way as with
everything else, and a machine that lost it to a reboot is handed it back
inside the same document rather than in a second round trip. Nothing of this
computer's own key material ever travels to the machine: a store read back is
re-encrypted here, not there.
"""

import base64
import json
import shlex
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.command_logging import commands_kept_out_of_logs
from imbue.mngr_latchkey.core import EncryptedCredentialStore
from imbue.mngr_latchkey.core import summarize_latchkey_failure
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError

# Name of the latchkey directory on the machine, under the remote user's home.
# The remote latchkey CLI runs as that user, so ``$HOME/.latchkey`` is the
# LATCHKEY_DIRECTORY it reads its credentials and permissions from.
REMOTE_LATCHKEY_DIR_NAME: Final[str] = ".latchkey"

# The command the package puts on the machine's PATH, and its two subcommands.
REMOTE_COMMAND_NAME: Final[str] = "mngr-latchkey"
_READ_STATE_SUBCOMMAND: Final[str] = "read-state"
_APPLY_STATE_SUBCOMMAND: Final[str] = "apply-state"

# One ``read-state`` or ``apply-state``. The slowest shape is provisioning's
# apply, which runs ``latchkey`` (a Node startup plus a store rewrite, a second
# or two), finds and authorizes the container, and bounces both supervisord
# programs, each ``supervisorctl restart`` blocking for the stop grace plus the
# program's ``startsecs``; the budget covers that on a loaded VPS without
# letting a wedged channel hold the reconcile open indefinitely.
REMOTE_LATCHKEY_TIMEOUT_SECONDS: Final[float] = 60.0

# A command that succeeded but took this long is a VPS degrading (a loaded
# machine, a slow store rewrite) that is worth noticing before it reaches the
# hard timeout above.
_SLOW_REMOTE_COMMAND_WARNING_THRESHOLD_SECONDS: Final[float] = 15.0

# A machine command travels as one argument of the remote ``sh -c``, and Linux
# caps any one argv string at 128 KiB (``MAX_ARG_STRLEN``). The base64 payloads
# a document embeds are a few KiB each in practice; a command past this size
# is refused rather than carried by a second, slower code path.
_MAX_REMOTE_COMMAND_BYTES: Final[int] = 100_000

# Delimits the document heredoc. The document is keys and base64, so it can
# never contain this line.
_DOCUMENT_END_MARKER: Final[str] = "MNGR_LATCHKEY_DOCUMENT_END"

# A script's last stdout line names its outcome, so a command that finished
# without running to its end (a shell that died mid-way) is told apart from one
# that applied everything.
OUTCOME_DONE_MARKER: Final[str] = "MNGR_LATCHKEY_OUTCOME=DONE"

# Prefix of every answer a read prints, one per line, each holding a base64
# payload. Prefixed rather than positional because anything else the script
# runs writes to the same stdout.
ANSWER_PREFIX: Final[str] = "MNGR_LATCHKEY_"

# tmpfs (RAM-backed) directory holding the gateway's secrets: the machine's own
# encryption key and listen password, plus the pair the forwarding extension
# presents to the desktop it proxies to. ``/run`` is the FHS location for runtime
# state, is root-owned, and is a tmpfs under systemd (which we already require
# for the supervisor service), so it is wiped on reboot -- the key is never
# persisted to the VPS disk beside the encrypted credential store (which would
# be equivalent to storing the credentials in plaintext against a disk-snapshot
# threat model), yet it survives a process crash so supervisord can restart the
# gateway without a desktop round-trip. The package's scripts verify this is
# really a RAM-backed filesystem before writing to it.
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

# What the logs say in place of a machine command. Its document carries secret
# material -- a credential store, the gateway's secrets, the policy being
# applied -- so its text is
# exactly as sensitive as what it moves, and at a whole store base64-encoded
# on one line it is artificially large besides.
SECRET_BEARING_COMMAND_LOG_REASON: Final[str] = "a latchkey machine command carrying secret material"

# Why an operation against a machine is refused when the secret its gateway
# runs under is not the one this computer holds for it.
DIFFERENT_MACHINE_KEY_MESSAGE: Final[str] = (
    "the machine keeps its credentials under a different key than this computer recorded "
    "(re-keyed from another computer?); the next provisioning pass adopts it"
)
DIFFERENT_MACHINE_PASSWORD_MESSAGE: Final[str] = (
    "the machine's gateway holds its callers to a different listen password than this computer recorded "
    "(changed from another computer?); the next provisioning pass adopts it"
)

# The document entries ``apply-state`` and ``read-state`` understand, spelled
# the way the package's scripts read them.
_ENTRY_ABANDON_CREDENTIAL_STORE: Final[str] = "abandon_credential_store"
_ENTRY_ENCRYPTION_KEY: Final[str] = "encryption_key"
_ENTRY_FALLBACK_ENCRYPTION_KEY: Final[str] = "fallback_encryption_key"
_ENTRY_LISTEN_PASSWORD: Final[str] = "listen_password"
_ENTRY_DESKTOP_GATEWAY_PASSWORD: Final[str] = "desktop_gateway_password"
_ENTRY_DESKTOP_PERMISSIONS_OVERRIDE: Final[str] = "desktop_permissions_override"
_ENTRY_CONFIG_JSON: Final[str] = "config_json"
_ENTRY_CREDENTIAL_BUNDLE: Final[str] = "credential_bundle"
_ENTRY_CREDENTIAL_DATA_FORMAT_VERSION: Final[str] = "credential_data_format_version"
_ENTRY_CREDENTIAL_SERVICE: Final[str] = "credential_service"
_ENTRY_CREDENTIAL_ACCOUNT: Final[str] = "credential_account"
_ENTRY_CLEAR_SERVICE: Final[str] = "clear_service"
_ENTRY_CLEAR_ACCOUNT: Final[str] = "clear_account"
_ENTRY_DESKTOP_EGRESS_RULES_JSON: Final[str] = "desktop_egress_rules_json"
_ENTRY_PERMISSIONS_JSON: Final[str] = "permissions_json"
_ENTRY_GATEWAY_LISTEN_HOST: Final[str] = "gateway_listen_host"
_ENTRY_TUNNEL_HOST_ID: Final[str] = "tunnel_host_id"
_ENTRY_TUNNEL_SSH_USER: Final[str] = "tunnel_ssh_user"
_ENTRY_TUNNEL_SSH_PORT: Final[str] = "tunnel_ssh_port"
_ENTRY_RESTART_GATEWAY: Final[str] = "restart_gateway"
_ENTRY_INCLUDE_CREDENTIAL_STORE: Final[str] = "include_credential_store"
_ENTRY_CONTAINER_HOST_ID: Final[str] = "container_host_id"

# The answers ``read-state`` prints, by the name after :data:`ANSWER_PREFIX`.
_ANSWER_PACKAGE_VERSION: Final[str] = "PACKAGE_VERSION"
_ANSWER_HOME: Final[str] = "HOME"
_ANSWER_ENCRYPTION_KEY: Final[str] = "ENCRYPTION_KEY"
_ANSWER_LISTEN_PASSWORD: Final[str] = "LISTEN_PASSWORD"
_ANSWER_HAS_CREDENTIAL_STORE: Final[str] = "HAS_CREDENTIAL_STORE"
_ANSWER_CONFIG_JSON: Final[str] = "CONFIG_JSON"
_ANSWER_PERMISSIONS_JSON: Final[str] = "PERMISSIONS_JSON"
_ANSWER_DESKTOP_EGRESS_RULES_JSON: Final[str] = "DESKTOP_EGRESS_RULES_JSON"
_ANSWER_CREDENTIALS: Final[str] = "CREDENTIALS"
_ANSWER_DATA_FORMAT_VERSION: Final[str] = "DATA_FORMAT_VERSION"
_ANSWER_HAS_CONTAINER_TUNNEL_KEY: Final[str] = "HAS_CONTAINER_TUNNEL_KEY"
_ANSWER_CONTAINER_EXTRA_HOSTS: Final[str] = "CONTAINER_EXTRA_HOSTS"
_FLAG_TRUE: Final[bytes] = b"1"


class RemoteStateRequest(FrozenModel):
    """What a read asks the machine for beyond what it always answers."""

    is_credential_store_included: bool = Field(
        default=False,
        description=(
            "Whether the machine's credential store comes back, still encrypted under the machine's own key. "
            "Left out by a read that only needs the machine's secrets and policy."
        ),
    )
    fallback_encryption_key: SecretStr | None = Field(
        default=None,
        description=(
            "The machine's own key as recorded here, written back to a machine that lost its RAM-backed copy "
            "to a reboot, so the key it answers with is the one its store is written under; a machine still "
            "holding one keeps that."
        ),
    )
    container_host_id: HostId | None = Field(
        default=None,
        description=(
            "The host whose container to look up on the machine (by its label), answered as "
            "``container_extra_hosts``; the read fails when the machine has no such container."
        ),
    )


class RemoteMachineState(FrozenModel):
    """What one read found on the machine."""

    package_version: str = Field(description="The version of the package whose ``read-state`` answered.")
    home: Path = Field(description="The remote user's home, under which the latchkey directory lives.")
    encryption_key: SecretStr | None = Field(
        description="The key the machine's gateway is running under, or ``None`` when its RAM holds none."
    )
    listen_password: str | None = Field(
        description=(
            "The listen password the machine's gateway holds its callers to, or ``None`` when its RAM holds none."
        )
    )
    has_credential_store: bool = Field(description="Whether the machine holds a credential store file.")
    config_json: str | None = Field(
        description="The machine's latchkey ``config.json``, or ``None`` when it has none."
    )
    permissions_json: str | None = Field(
        description="The policy the machine's gateway enforces, or ``None`` when it has none yet."
    )
    desktop_egress_rules_json: str | None = Field(
        description="The desktop egress rules the machine's curl router reads, or ``None`` when it has no rules file."
    )
    credential_store: EncryptedCredentialStore | None = Field(
        description=(
            "The machine's store as it holds it, encrypted under its own key, or ``None`` when it was not asked "
            "for or the machine holds no store."
        )
    )
    has_container_tunnel_key: bool = Field(
        description=(
            "Whether the machine holds the keypair its reverse tunnel into a container authenticates with, "
            "which every build so far minted exactly when it wired that tunnel."
        )
    )
    container_extra_hosts: tuple[str, ...] | None = Field(
        description=(
            "The ``--add-host`` mappings the asked-about container was created with (empty for one created with "
            "none), or ``None`` when the read asked about no container."
        )
    )


class RemoteCredentialMerge(FrozenModel):
    """A store to merge into the machine's own, scoped to one service (and one account of it, when named)."""

    service_name: str = Field(description="The one service the machine takes from the bundle.")
    account: str = Field(
        description=(
            "The one account of that service the machine takes, leaving its other accounts as they are; empty "
            "means every account the bundle holds for the service."
        )
    )
    bundle: bytes = Field(description="The credential store to merge, already encrypted with the machine's key.")
    data_format_version: str = Field(description="The upstream format stamp the bundle was written in.")


class RemoteCredentialClear(FrozenModel):
    """One account of one service to take away from the machine's own store."""

    service_name: str = Field(description="The service the account belongs to.")
    account: str = Field(description="The account to clear; empty is upstream's unnamed default account.")


class RemoteTunnelTarget(FrozenModel):
    """The agent's container the machine's reverse tunnel is to reach."""

    host_id: HostId = Field(description="The host whose container is looked up on the machine by its label.")
    ssh_user: str = Field(description="The container's ssh user.")
    ssh_port: int = Field(description="The loopback port the container's sshd is published on, on the machine.")


class RemoteStateUpdate(FrozenModel):
    """What one apply makes the machine hold; every part is optional and the present ones land together."""

    encryption_key: SecretStr | None = Field(
        default=None,
        description=(
            "The machine's own key. Adopted, never replaced: a machine already running under a different one "
            "refuses the whole update (see :data:`DIFFERENT_MACHINE_KEY_MESSAGE`)."
        ),
    )
    fallback_encryption_key: SecretStr | None = Field(
        default=None,
        description="The machine's own key, written only to a machine that has none (a reboot wiped it).",
    )
    listen_password: str | None = Field(
        default=None, description="The machine's own gateway listen password, adopted the way the key is."
    )
    desktop_gateway_password: str | None = Field(
        default=None, description="This computer's gateway password, for the forwarding extension's hop back here."
    )
    desktop_permissions_override: str | None = Field(
        default=None, description="This computer's desktop-target JWT, for the same hop."
    )
    config_json: str | None = Field(default=None, description="The gateway's ``config.json`` to install.")
    credential_merge: RemoteCredentialMerge | None = Field(
        default=None, description="A store to merge into the machine's own."
    )
    credential_clear: RemoteCredentialClear | None = Field(
        default=None, description="An account to take away from the machine's own store."
    )
    desktop_egress_rules_json: str | None = Field(
        default=None, description="The desktop egress rules the machine's curl router is to read."
    )
    permissions_json: str | None = Field(default=None, description="The policy the gateway is to enforce.")
    gateway_listen_host: str | None = Field(
        default=None,
        description=(
            "The address the gateway binds (the machine's docker bridge address, which the agent's container "
            "reaches by name), read by the gateway program on every start and by the tunnel as its far end."
        ),
    )
    tunnel: RemoteTunnelTarget | None = Field(
        default=None,
        description="The container to wire the reverse tunnel into; left out for a container that needs none.",
    )
    is_credential_store_abandoned: bool = Field(
        default=False, description="Whether to remove the machine's store first: nobody present holds its key."
    )
    is_gateway_restarted: bool = Field(
        default=False, description="Whether to restart the gateway afterwards so rewritten secrets take effect."
    )


def read_remote_state(
    host: OuterHostInterface, request: RemoteStateRequest, failure_description: str
) -> RemoteMachineState:
    """Ask the machine what it holds, in one round trip.

    Raises:
        RemoteGatewayError: when the machine refuses or fails the command, or
            answers with something this build cannot read.
    """
    stdout = _run_remote_command(
        host, _remote_command(_READ_STATE_SUBCOMMAND, _request_document(request)), failure_description
    )
    answers = _parse_answers(host, stdout, failure_description)
    return _machine_state_from_answers(host, answers, failure_description)


def apply_remote_state(host: OuterHostInterface, update: RemoteStateUpdate, failure_description: str) -> None:
    """Make the machine hold ``update``, in one round trip.

    Raises:
        RemoteGatewayError: when the update does not fit one command, or the
            machine refuses or fails it.
    """
    _run_remote_command(host, _remote_command(_APPLY_STATE_SUBCOMMAND, _update_document(update)), failure_description)


@pure
def _request_document(request: RemoteStateRequest) -> dict[str, bytes]:
    document: dict[str, bytes] = {}
    if request.is_credential_store_included:
        document[_ENTRY_INCLUDE_CREDENTIAL_STORE] = _FLAG_TRUE
    if request.fallback_encryption_key is not None:
        document[_ENTRY_FALLBACK_ENCRYPTION_KEY] = request.fallback_encryption_key.get_secret_value().encode("utf-8")
    if request.container_host_id is not None:
        document[_ENTRY_CONTAINER_HOST_ID] = str(request.container_host_id).encode("utf-8")
    return document


@pure
def _update_document(update: RemoteStateUpdate) -> dict[str, bytes]:
    document: dict[str, bytes] = {}
    if update.is_credential_store_abandoned:
        document[_ENTRY_ABANDON_CREDENTIAL_STORE] = _FLAG_TRUE
    if update.encryption_key is not None:
        document[_ENTRY_ENCRYPTION_KEY] = update.encryption_key.get_secret_value().encode("utf-8")
    if update.fallback_encryption_key is not None:
        document[_ENTRY_FALLBACK_ENCRYPTION_KEY] = update.fallback_encryption_key.get_secret_value().encode("utf-8")
    if update.listen_password is not None:
        document[_ENTRY_LISTEN_PASSWORD] = update.listen_password.encode("utf-8")
    if update.desktop_gateway_password is not None:
        document[_ENTRY_DESKTOP_GATEWAY_PASSWORD] = update.desktop_gateway_password.encode("utf-8")
    if update.desktop_permissions_override is not None:
        document[_ENTRY_DESKTOP_PERMISSIONS_OVERRIDE] = update.desktop_permissions_override.encode("utf-8")
    if update.config_json is not None:
        document[_ENTRY_CONFIG_JSON] = update.config_json.encode("utf-8")
    if update.credential_merge is not None:
        document[_ENTRY_CREDENTIAL_BUNDLE] = update.credential_merge.bundle
        document[_ENTRY_CREDENTIAL_DATA_FORMAT_VERSION] = update.credential_merge.data_format_version.encode("utf-8")
        document[_ENTRY_CREDENTIAL_SERVICE] = update.credential_merge.service_name.encode("utf-8")
        # An empty account means the whole service, which the script reads as
        # the entry being absent.
        if update.credential_merge.account:
            document[_ENTRY_CREDENTIAL_ACCOUNT] = update.credential_merge.account.encode("utf-8")
    if update.credential_clear is not None:
        document[_ENTRY_CLEAR_SERVICE] = update.credential_clear.service_name.encode("utf-8")
        document[_ENTRY_CLEAR_ACCOUNT] = update.credential_clear.account.encode("utf-8")
    if update.desktop_egress_rules_json is not None:
        document[_ENTRY_DESKTOP_EGRESS_RULES_JSON] = update.desktop_egress_rules_json.encode("utf-8")
    if update.permissions_json is not None:
        document[_ENTRY_PERMISSIONS_JSON] = update.permissions_json.encode("utf-8")
    if update.gateway_listen_host is not None:
        document[_ENTRY_GATEWAY_LISTEN_HOST] = update.gateway_listen_host.encode("utf-8")
    if update.tunnel is not None:
        document[_ENTRY_TUNNEL_HOST_ID] = str(update.tunnel.host_id).encode("utf-8")
        document[_ENTRY_TUNNEL_SSH_USER] = update.tunnel.ssh_user.encode("utf-8")
        document[_ENTRY_TUNNEL_SSH_PORT] = str(update.tunnel.ssh_port).encode("utf-8")
    if update.is_gateway_restarted:
        document[_ENTRY_RESTART_GATEWAY] = _FLAG_TRUE
    return document


@pure
def _remote_command(subcommand: str, document: Mapping[str, bytes]) -> str:
    """The one command string that runs ``subcommand`` on the machine with ``document`` on its stdin."""
    lines = [f"{key} {base64.b64encode(value).decode('ascii')}" for key, value in document.items()]
    return "\n".join(
        (
            f"{REMOTE_COMMAND_NAME} {shlex.quote(subcommand)} <<'{_DOCUMENT_END_MARKER}'",
            *lines,
            _DOCUMENT_END_MARKER,
        )
    )


def _run_remote_command(host: OuterHostInterface, command: str, failure_description: str) -> str:
    """Run one machine command and return its stdout, once its last line says it ran to its end.

    The command's text is kept out of the logs (see
    :data:`SECRET_BEARING_COMMAND_LOG_REASON`); the host layer traces a
    stand-in naming the kind of command and its size instead.

    Raises:
        RemoteGatewayError: when the command does not fit one remote command,
            the machine refuses or fails it, or it finishes without reporting an
            outcome (which means it did not run to its end).
    """
    command_size = len(command.encode("utf-8"))
    if command_size > _MAX_REMOTE_COMMAND_BYTES:
        raise RemoteGatewayError(
            f"Failed to {failure_description} on VPS {host.get_name()}: the single command that would carry it is "
            f"{command_size} bytes, past the {_MAX_REMOTE_COMMAND_BYTES} a remote shell accepts"
        )
    started_at = time.monotonic()
    with commands_kept_out_of_logs(SECRET_BEARING_COMMAND_LOG_REASON):
        result = host.execute_idempotent_command(command, timeout_seconds=REMOTE_LATCHKEY_TIMEOUT_SECONDS)
    elapsed_seconds = time.monotonic() - started_at
    # The answers a read prints before it can fail carry the machine's secrets,
    # so a failure is described by everything else the command printed.
    diagnostic_lines = _diagnostic_lines(result.stdout)
    if result.success and elapsed_seconds > _SLOW_REMOTE_COMMAND_WARNING_THRESHOLD_SECONDS:
        logger.warning(
            "Ran a latchkey command to {} on VPS {} in {:.0f}s", failure_description, host.get_name(), elapsed_seconds
        )
    # A script that ran to its end may still have something to say: a warning
    # from a latchkey invocation in the middle of it. The log guard hides the command's text,
    # not what the machine printed, and no script writes a secret to stderr.
    if result.success and result.stderr.strip():
        logger.debug(
            "The machine said on stderr while a latchkey command to {} ran on VPS {}: {}",
            failure_description,
            host.get_name(),
            result.stderr.strip(),
        )
    if not result.success:
        detail = result.stderr.strip() or "\n".join(diagnostic_lines)
        raise RemoteGatewayError(
            "Failed to {} on VPS {}: {}".format(
                failure_description,
                host.get_name(),
                summarize_latchkey_failure(detail, "the command reported no reason"),
            )
        )
    stdout_lines = result.stdout.strip().splitlines()
    last_line = stdout_lines[-1] if stdout_lines else ""
    if last_line != OUTCOME_DONE_MARKER:
        last_diagnostic_line = diagnostic_lines[-1] if diagnostic_lines else ""
        raise RemoteGatewayError(
            f"Failed to {failure_description} on VPS {host.get_name()}: the command finished without reporting an "
            f"outcome (last output line: {last_diagnostic_line!r})"
        )
    return result.stdout


@pure
def _diagnostic_lines(stdout: str) -> list[str]:
    """The non-empty lines of a command's stdout that are not answers (nor the outcome marker)."""
    return [line for line in stdout.splitlines() if line.strip() and not line.startswith(ANSWER_PREFIX)]


def _parse_answers(host: OuterHostInterface, stdout: str, failure_description: str) -> dict[str, bytes]:
    """The prefixed, base64-encoded lines of a read's output, decoded and keyed by answer name.

    Raises:
        RemoteGatewayError: when an answer is not the base64 the script is
            supposed to have written.
    """
    answers: dict[str, bytes] = {}
    for line in stdout.splitlines():
        if not line.startswith(ANSWER_PREFIX) or line == OUTCOME_DONE_MARKER:
            continue
        name, _, encoded = line.removeprefix(ANSWER_PREFIX).partition("=")
        try:
            answers[name] = base64.b64decode(encoded, validate=True)
        except ValueError as e:
            raise RemoteGatewayError(
                f"Failed to {failure_description} on VPS {host.get_name()}: the machine's {name} answer is not "
                f"the base64 it should be: {e}"
            ) from e
    return answers


def _machine_state_from_answers(
    host: OuterHostInterface, answers: Mapping[str, bytes], failure_description: str
) -> RemoteMachineState:
    """Raises :class:`RemoteGatewayError` when an answer the read always gives is missing, or one is not text."""
    package_version = _text(answers, _ANSWER_PACKAGE_VERSION, host, failure_description)
    home = _text(answers, _ANSWER_HOME, host, failure_description)
    has_credential_store = _flag(answers, _ANSWER_HAS_CREDENTIAL_STORE, host, failure_description)
    has_container_tunnel_key = _flag(answers, _ANSWER_HAS_CONTAINER_TUNNEL_KEY, host, failure_description)
    container_extra_hosts_json = _optional_text(answers, _ANSWER_CONTAINER_EXTRA_HOSTS, host, failure_description)
    data_format_version = _optional_text(answers, _ANSWER_DATA_FORMAT_VERSION, host, failure_description)
    encryption_key = _optional_text(answers, _ANSWER_ENCRYPTION_KEY, host, failure_description)
    listen_password = _optional_text(answers, _ANSWER_LISTEN_PASSWORD, host, failure_description)
    config_json = _optional_text(answers, _ANSWER_CONFIG_JSON, host, failure_description)
    permissions_json = _optional_text(answers, _ANSWER_PERMISSIONS_JSON, host, failure_description)
    desktop_egress_rules_json = _optional_text(answers, _ANSWER_DESKTOP_EGRESS_RULES_JSON, host, failure_description)
    credentials = answers.get(_ANSWER_CREDENTIALS)
    if (credentials is None) != (data_format_version is None):
        raise RemoteGatewayError(
            f"Failed to {failure_description} on VPS {host.get_name()}: the machine's credential store came back "
            "without the format stamp that says how to read it"
        )
    return RemoteMachineState(
        package_version=package_version,
        home=Path(home),
        encryption_key=SecretStr(encryption_key) if encryption_key else None,
        listen_password=listen_password or None,
        has_credential_store=has_credential_store,
        config_json=config_json,
        permissions_json=permissions_json,
        desktop_egress_rules_json=desktop_egress_rules_json,
        credential_store=(
            EncryptedCredentialStore(content=credentials, data_format_version=data_format_version)
            if credentials is not None and data_format_version is not None
            else None
        ),
        has_container_tunnel_key=has_container_tunnel_key,
        container_extra_hosts=(
            _container_extra_hosts(host, container_extra_hosts_json, failure_description)
            if container_extra_hosts_json is not None
            else None
        ),
    )


def _container_extra_hosts(host: OuterHostInterface, answer: str, failure_description: str) -> tuple[str, ...]:
    """The mappings ``docker inspect`` printed for the container, as it prints them: a JSON list, or ``null`` for none.

    Raises:
        RemoteGatewayError: when the answer is not that.
    """
    try:
        extra_hosts = json.loads(answer)
    except json.JSONDecodeError as e:
        raise RemoteGatewayError(
            f"Failed to {failure_description} on VPS {host.get_name()}: the machine's container extra hosts answer "
            f"is not the JSON docker prints: {e}"
        ) from e
    if extra_hosts is None:
        return ()
    if not isinstance(extra_hosts, list) or not all(isinstance(entry, str) for entry in extra_hosts):
        raise RemoteGatewayError(
            f"Failed to {failure_description} on VPS {host.get_name()}: the machine's container extra hosts answer "
            f"is not the list of strings docker prints: {answer!r}"
        )
    return tuple(extra_hosts)


def _required_answer(
    answers: Mapping[str, bytes], name: str, host: OuterHostInterface, failure_description: str
) -> bytes:
    """Raises :class:`RemoteGatewayError` when the machine did not answer ``name``, which every read must."""
    try:
        return answers[name]
    except KeyError as e:
        raise RemoteGatewayError(
            f"Failed to {failure_description} on VPS {host.get_name()}: the machine answered without {name}"
        ) from e


def _decoded_answer(value: bytes, name: str, host: OuterHostInterface, failure_description: str) -> str:
    """Raises :class:`RemoteGatewayError` when the ``name`` answer is not the UTF-8 text it should be."""
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as e:
        raise RemoteGatewayError(
            f"Failed to {failure_description} on VPS {host.get_name()}: the machine's {name} answer is not the "
            f"UTF-8 text it should be: {e}"
        ) from e


def _text(answers: Mapping[str, bytes], name: str, host: OuterHostInterface, failure_description: str) -> str:
    return _decoded_answer(_required_answer(answers, name, host, failure_description), name, host, failure_description)


def _flag(answers: Mapping[str, bytes], name: str, host: OuterHostInterface, failure_description: str) -> bool:
    return _required_answer(answers, name, host, failure_description) == _FLAG_TRUE


def _optional_text(
    answers: Mapping[str, bytes], name: str, host: OuterHostInterface, failure_description: str
) -> str | None:
    value = answers.get(name)
    return _decoded_answer(value, name, host, failure_description) if value is not None else None
