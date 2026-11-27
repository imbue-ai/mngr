"""How this computer talks to the machine a remote host's agents run on.

Once the package built by :mod:`imbue.mngr_latchkey.remote.package` is
installed on a machine, everything this computer does to it goes through the
five commands the package provides, each one remote command:

* ``mngr-latchkey read-state`` assembles what the machine holds -- the secrets
  its gateway runs under, its config, the policy it enforces, the desktop
  egress rules its curl router reads, the format version the policy is written
  in, a fingerprint of those three, how the agent's container was created
  (when asked about one), and (when asked) its credential store, still
  encrypted under the machine's own key -- and prints it as prefixed base64
  lines (:func:`read_remote_state`).
* ``mngr-latchkey apply-state`` makes the machine match a document of what this
  computer wants it to hold, applied in one ``set -e`` script on the machine,
  and answers with the fingerprint of the policy it holds afterwards
  (:func:`apply_remote_state`).
* ``mngr-latchkey announce-device`` records this computer as one of the
  desktops connected to the machine: the loopback port its tunnel binds there
  and the secrets the gateway's extension and the curl router present on the
  hop back to it (:func:`announce_device`). Rewritten on every discovery cycle,
  so the record's age is what says whether the desktop is still connected.
* ``mngr-latchkey list-requests`` lists the permission requests the machine
  keeps for the user's desktops (:func:`read_filed_permission_requests`), and
  ``mngr-latchkey forget-request`` drops one the user has answered and
  withdraws it from every connected desktop (:func:`forget_permission_request`);
  see :mod:`imbue.mngr_latchkey.filed_permission_requests`.

The document travels on the command's stdin (a quoted heredoc in the one
command string), as ``<key> <base64>`` lines, so a payload is visibly data
rather than code: the shell never interprets it, and no entry reaches the
``argv`` of ``mngr-latchkey``. Its scripts read each entry from a file under
the machine's RAM-backed scratch directory, and of the ``latchkey``
invocations they make only the service and account names travel in ``argv``,
never a key, a password or a store (those go by file, stdin or the
environment). Neither is the machine's own key ever spelled into a command: a
script that runs ``latchkey`` reads the key from the machine's latchkey
directory itself, and a read answers with it the same base64 way as with
everything else. Nothing of this computer's own key material ever travels to
the machine: a store read back is re-encrypted here, not there.

Every one of the five commands is preceded by a check of the machine's
generation (:func:`machine_generation_check`), so none of them runs on a
machine whose software a newer build installed and this one does not drive;
the command raises :class:`~imbue.mngr_latchkey.remote.errors.MachineNewerError`
instead.

A policy crosses the wire in the machine's projection of it, in both
directions (:mod:`imbue.mngr_latchkey.remote._machine_policy`): what an update
carries is projected as its document is built, and what a read answers with is
restored before anything else sees it.

The fingerprint is the machine's own: it digests its policy file, its desktop
egress rules file and its format stamp, and this computer never computes one.
An update can carry the fingerprint a read or an earlier update answered with
(:attr:`RemoteStateUpdate.expected_policy_fingerprint`), and a machine that
holds any other policy by then applies none of it. That is what lets a
whole-policy push skip the read that would otherwise have to precede it. A
machine whose package does not answer with a fingerprint would not check one
either, so there is nothing to hand it back and its updates stay unconditional.
"""

import base64
import json
import shlex
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import paramiko
from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.command_logging import commands_kept_out_of_logs
from imbue.mngr_latchkey.core import EncryptedCredentialStore
from imbue.mngr_latchkey.core import summarize_latchkey_failure
from imbue.mngr_latchkey.devices import DeviceRecord
from imbue.mngr_latchkey.filed_permission_requests import FiledPermissionRequest
from imbue.mngr_latchkey.filed_permission_requests import FiledPermissionRequestsError
from imbue.mngr_latchkey.filed_permission_requests import parse_filed_permission_requests
from imbue.mngr_latchkey.primitives import MachinePolicyFingerprint
from imbue.mngr_latchkey.primitives import PermissionsFormatVersion
from imbue.mngr_latchkey.remote._machine_policy import project_policy_for_machine
from imbue.mngr_latchkey.remote._machine_policy import restore_policy_from_machine
from imbue.mngr_latchkey.remote.errors import MachineNewerError
from imbue.mngr_latchkey.remote.errors import MachinePolicyChangedError
from imbue.mngr_latchkey.remote.errors import MachineTransferError
from imbue.mngr_latchkey.remote.errors import ReceivedFromMachine
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError
from imbue.mngr_latchkey.remote.errors import SentToMachine
from imbue.mngr_latchkey.remote.errors import describe_machine_transfer

# Name of the latchkey directory on the machine, under the remote user's home.
# The remote latchkey CLI runs as that user, so ``$HOME/.latchkey`` is the
# LATCHKEY_DIRECTORY it reads its credentials and permissions from.
REMOTE_LATCHKEY_DIR_NAME: Final[str] = ".latchkey"

# The command the package puts on the machine's PATH, and its subcommands.
REMOTE_COMMAND_NAME: Final[str] = "mngr-latchkey"
_READ_STATE_SUBCOMMAND: Final[str] = "read-state"
_APPLY_STATE_SUBCOMMAND: Final[str] = "apply-state"
_ANNOUNCE_DEVICE_SUBCOMMAND: Final[str] = "announce-device"
_FORGET_REQUEST_SUBCOMMAND: Final[str] = "forget-request"
_LIST_REQUESTS_SUBCOMMAND: Final[str] = "list-requests"

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
# a document embeds are a few KiB each in practice, except a policy, which
# grows with the agents registered on the host and travels without whitespace
# for that reason (:mod:`imbue.mngr_latchkey.remote._machine_policy`); a command
# past this size is refused rather than carried by a second, slower code path.
_MAX_REMOTE_COMMAND_BYTES: Final[int] = 100_000

# Delimits the document heredoc. The document is keys and base64, so it can
# never contain this line.
_DOCUMENT_END_MARKER: Final[str] = "MNGR_LATCHKEY_DOCUMENT_END"

# A script's last stdout line names its outcome, so a command that finished
# without running to its end (a shell that died mid-way) is told apart from one
# that applied everything.
OUTCOME_DONE_MARKER: Final[str] = "MNGR_LATCHKEY_OUTCOME=DONE"

# The generation of the machine software this build installs and drives. A
# generation changes only when a machine's software stops serving what a build
# of the generation before sends it, or can no longer be replaced by that
# build's package; a new build of the package is not a new generation. The
# installed package records its generation in the file below, and a machine
# without the file is generation 1. The path names no package, because whatever
# replaces this package keeps writing it.
MACHINE_GENERATION: Final[int] = 1
MACHINE_GENERATION_FILE: Final[Path] = Path("/etc/mngr-machine/generation")

# The last stdout line of a command that found the machine's generation newer
# than this build's and so did nothing.
OUTCOME_MACHINE_NEWER_MARKER: Final[str] = "MNGR_LATCHKEY_OUTCOME=MACHINE_NEWER"

# The last stdout line of an update that found the machine holding another
# policy than the one it was made from and so applied nothing.
OUTCOME_POLICY_CHANGED_MARKER: Final[str] = "MNGR_LATCHKEY_OUTCOME=POLICY_CHANGED"

# Prefix of every answer a read prints, one per line, each holding a base64
# payload. Prefixed rather than positional because anything else the script
# runs writes to the same stdout.
ANSWER_PREFIX: Final[str] = "MNGR_LATCHKEY_"

# tmpfs (RAM-backed) directory holding what should not outlive a boot: the
# records of the desktops connected to the machine (each carrying the pair
# presented on the hop back to that desktop, which is the desktop's secret
# and not the machine's to keep) and the scratch space a command's document is
# unpacked into. ``/run`` is the FHS location for runtime state, is root-owned,
# and is a tmpfs under systemd (which we already require for the supervisor
# service). The package's scripts verify this is really a RAM-backed
# filesystem before writing to it.
#
# Deliberately not ``/tmp``: unlike ``/run``, ``/tmp`` is not reliably a tmpfs
# (on many distros, incl. common Debian/Ubuntu VPS images, it is a normal
# directory on the root disk and is cleaned by age rather than wiped on boot).
# ``/tmp`` is also world-writable (1777), exposing the classic hostile-symlink
# attack that a root-owned ``/run`` avoids.
RUNTIME_DIR: Final[Path] = Path("/run/mngr-latchkey")

# The machine's own two secrets, kept in its latchkey directory beside the
# credential store: the key that store is encrypted with, and the listen
# password its gateway holds every caller to. On the machine's disk, so its
# gateway comes back after a reboot without any of the user's computers; the
# directory is root's and out of the agent container's reach, and whoever can
# read the machine's disk can read the credentials on it. The password belongs
# to the machine rather than to any one of the user's computers: it is what the
# workspaces on it were created with (their host env file is written once, at
# ``mngr create``), so it can never be replaced -- see
# :func:`~imbue.mngr_latchkey.remote.provisioning._resolve_machine_gateway_password`.
MACHINE_ENCRYPTION_KEY_FILENAME: Final[str] = "machine_encryption_key"
MACHINE_GATEWAY_PASSWORD_FILENAME: Final[str] = "machine_gateway_password"

# CLEANUP: drop these two names (with the postinst and gateway-run blocks
# that use them) once none of the user's computers runs a build that kept the
# machine's secrets in RAM. Under :data:`RUNTIME_DIR` they are where such a
# build reads and writes the machine's secrets. The current names differ from
# them because the postinst of such a build deletes these names from the
# latchkey directory.
LEGACY_RAM_ENCRYPTION_KEY_FILENAME: Final[str] = "gateway_encryption_key"
LEGACY_RAM_LISTEN_PASSWORD_FILENAME: Final[str] = "gateway_listen_password"

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
_ENTRY_EXPECTED_POLICY_FINGERPRINT: Final[str] = "expected_policy_fingerprint"
_ENTRY_ABANDON_CREDENTIAL_STORE: Final[str] = "abandon_credential_store"
_ENTRY_ENCRYPTION_KEY: Final[str] = "encryption_key"
_ENTRY_LISTEN_PASSWORD: Final[str] = "listen_password"
_ENTRY_CONFIG_JSON: Final[str] = "config_json"
_ENTRY_CREDENTIAL_BUNDLE: Final[str] = "credential_bundle"
_ENTRY_CREDENTIAL_DATA_FORMAT_VERSION: Final[str] = "credential_data_format_version"
_ENTRY_CREDENTIAL_SERVICE: Final[str] = "credential_service"
_ENTRY_CREDENTIAL_ACCOUNT: Final[str] = "credential_account"
_ENTRY_CLEAR_SERVICE: Final[str] = "clear_service"
_ENTRY_CLEAR_ACCOUNT: Final[str] = "clear_account"
_ENTRY_DESKTOP_EGRESS_RULES_JSON: Final[str] = "desktop_egress_rules_json"
_ENTRY_PERMISSIONS_JSON: Final[str] = "permissions_json"
_ENTRY_PERMISSIONS_FORMAT_VERSION: Final[str] = "permissions_format_version"
_ENTRY_GATEWAY_LISTEN_HOST: Final[str] = "gateway_listen_host"
_ENTRY_TUNNEL_HOST_ID: Final[str] = "tunnel_host_id"
_ENTRY_TUNNEL_SSH_USER: Final[str] = "tunnel_ssh_user"
_ENTRY_TUNNEL_SSH_PORT: Final[str] = "tunnel_ssh_port"
_ENTRY_RESTART_GATEWAY: Final[str] = "restart_gateway"
_ENTRY_INCLUDE_CREDENTIAL_STORE: Final[str] = "include_credential_store"
_ENTRY_CONTAINER_HOST_ID: Final[str] = "container_host_id"
_ENTRY_DEVICE_ID: Final[str] = "device_id"
_ENTRY_DEVICE_RECORD_JSON: Final[str] = "device_record_json"
_ENTRY_REQUEST_ID: Final[str] = "request_id"

# The answers ``read-state`` prints, by the name after :data:`ANSWER_PREFIX`.
_ANSWER_PACKAGE_VERSION: Final[str] = "PACKAGE_VERSION"
_ANSWER_HOME: Final[str] = "HOME"
_ANSWER_ENCRYPTION_KEY: Final[str] = "ENCRYPTION_KEY"
_ANSWER_LISTEN_PASSWORD: Final[str] = "LISTEN_PASSWORD"
_ANSWER_HAS_CREDENTIAL_STORE: Final[str] = "HAS_CREDENTIAL_STORE"
_ANSWER_CONFIG_JSON: Final[str] = "CONFIG_JSON"
_ANSWER_PERMISSIONS_JSON: Final[str] = "PERMISSIONS_JSON"
_ANSWER_DESKTOP_EGRESS_RULES_JSON: Final[str] = "DESKTOP_EGRESS_RULES_JSON"
_ANSWER_PERMISSIONS_FORMAT_VERSION: Final[str] = "PERMISSIONS_FORMAT_VERSION"
# Answered by ``read-state`` and by ``apply-state``; a machine whose package gives none is read as having none.
_ANSWER_POLICY_FINGERPRINT: Final[str] = "POLICY_FINGERPRINT"
_ANSWER_CREDENTIALS: Final[str] = "CREDENTIALS"
_ANSWER_DATA_FORMAT_VERSION: Final[str] = "DATA_FORMAT_VERSION"
_ANSWER_HAS_CONTAINER_TUNNEL_KEY: Final[str] = "HAS_CONTAINER_TUNNEL_KEY"
_ANSWER_CONTAINER_EXTRA_HOSTS: Final[str] = "CONTAINER_EXTRA_HOSTS"
# The one answer ``list-requests`` prints.
_ANSWER_FILED_PERMISSION_REQUESTS: Final[str] = "FILED_PERMISSION_REQUESTS"
_FLAG_TRUE: Final[bytes] = b"1"

# The part of what is sent that each document entry belongs to (see :func:`_command_parts`).
_SENT_PART_BY_ENTRY: Final[Mapping[str, SentToMachine]] = {
    _ENTRY_EXPECTED_POLICY_FINGERPRINT: SentToMachine.EXPECTED_POLICY_FINGERPRINT,
    _ENTRY_ENCRYPTION_KEY: SentToMachine.ENCRYPTION_KEY,
    _ENTRY_LISTEN_PASSWORD: SentToMachine.LISTEN_PASSWORD,
    _ENTRY_CONFIG_JSON: SentToMachine.CONFIG,
    _ENTRY_CREDENTIAL_BUNDLE: SentToMachine.CREDENTIAL_MERGE,
    _ENTRY_CREDENTIAL_DATA_FORMAT_VERSION: SentToMachine.CREDENTIAL_MERGE,
    _ENTRY_CREDENTIAL_SERVICE: SentToMachine.CREDENTIAL_MERGE,
    _ENTRY_CREDENTIAL_ACCOUNT: SentToMachine.CREDENTIAL_MERGE,
    _ENTRY_CLEAR_SERVICE: SentToMachine.CREDENTIAL_CLEAR,
    _ENTRY_CLEAR_ACCOUNT: SentToMachine.CREDENTIAL_CLEAR,
    _ENTRY_ABANDON_CREDENTIAL_STORE: SentToMachine.CREDENTIAL_STORE_ABANDONMENT,
    _ENTRY_DESKTOP_EGRESS_RULES_JSON: SentToMachine.DESKTOP_EGRESS_RULES,
    _ENTRY_PERMISSIONS_JSON: SentToMachine.PERMISSIONS,
    _ENTRY_PERMISSIONS_FORMAT_VERSION: SentToMachine.PERMISSIONS_FORMAT_VERSION,
    _ENTRY_GATEWAY_LISTEN_HOST: SentToMachine.GATEWAY_LISTEN_HOST,
    _ENTRY_TUNNEL_HOST_ID: SentToMachine.TUNNEL,
    _ENTRY_TUNNEL_SSH_USER: SentToMachine.TUNNEL,
    _ENTRY_TUNNEL_SSH_PORT: SentToMachine.TUNNEL,
    _ENTRY_RESTART_GATEWAY: SentToMachine.GATEWAY_RESTART,
    _ENTRY_DEVICE_ID: SentToMachine.DEVICE_RECORD,
    _ENTRY_DEVICE_RECORD_JSON: SentToMachine.DEVICE_RECORD,
    _ENTRY_REQUEST_ID: SentToMachine.PERMISSION_REQUEST_WITHDRAWAL,
}

# The document entries that ask a read for an answer it gives only when asked, by that answer.
_EXPECTED_PART_BY_QUERY_ENTRY: Final[Mapping[str, ReceivedFromMachine]] = {
    _ENTRY_INCLUDE_CREDENTIAL_STORE: ReceivedFromMachine.CREDENTIAL_STORE,
    _ENTRY_CONTAINER_HOST_ID: ReceivedFromMachine.CONTAINER_EXTRA_HOSTS,
}

# What each subcommand answers with, whatever its document asks for.
_ANSWERED_PARTS_BY_SUBCOMMAND: Final[Mapping[str, frozenset[ReceivedFromMachine]]] = {
    _READ_STATE_SUBCOMMAND: frozenset(
        {
            ReceivedFromMachine.ENCRYPTION_KEY,
            ReceivedFromMachine.LISTEN_PASSWORD,
            ReceivedFromMachine.CONFIG,
            ReceivedFromMachine.PERMISSIONS,
            ReceivedFromMachine.PERMISSIONS_FORMAT_VERSION,
            ReceivedFromMachine.DESKTOP_EGRESS_RULES,
        }
    ),
    _APPLY_STATE_SUBCOMMAND: frozenset(),
    _ANNOUNCE_DEVICE_SUBCOMMAND: frozenset(),
    _FORGET_REQUEST_SUBCOMMAND: frozenset(),
    _LIST_REQUESTS_SUBCOMMAND: frozenset({ReceivedFromMachine.FILED_PERMISSION_REQUESTS}),
}

# What running a command raises when it never reached the machine or lost it midway. The first command a machine
# runs is what opens its SSH connection, so a connect that fails is that command's failure.
_COMMAND_TRANSPORT_FAILURES: Final[tuple[type[Exception], ...]] = (
    MngrError,
    OSError,
    paramiko.SSHException,
    EOFError,
)


class _CommandParts(FrozenModel):
    """What one machine command carries each way, as read off the document it sends (see :func:`_command_parts`)."""

    sent: frozenset[SentToMachine] = Field(description="What the document hands the machine to hold or act on.")
    expected: frozenset[ReceivedFromMachine] = Field(description="What the command answers with.")

    def describe(self) -> str:
        return describe_machine_transfer(self.sent, self.expected)

    def failure(self, host: OuterHostInterface, host_id: HostId, reason: str) -> MachineTransferError:
        """The error for this command having failed on ``host``, for ``reason``."""
        return MachineTransferError(
            host_id=host_id, vps_name=host.get_name(), sent=self.sent, expected=self.expected, reason=reason
        )


class _UnreadableAnswerError(RemoteGatewayError):
    """Raised when a command's answers are not what its script writes; the command that read them reports it."""


class RemoteStateRequest(FrozenModel):
    """What a read asks the machine for beyond what it always answers."""

    is_credential_store_included: bool = Field(
        default=False,
        description=(
            "Whether the machine's credential store comes back, still encrypted under the machine's own key. "
            "Left out by a read that only needs the machine's secrets and policy."
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
        description="The key the machine keeps its credential store under, or ``None`` when it holds none."
    )
    listen_password: str | None = Field(
        description=("The listen password the machine's gateway holds its callers to, or ``None`` when it holds none.")
    )
    has_credential_store: bool = Field(description="Whether the machine holds a credential store file.")
    config_json: str | None = Field(
        description="The machine's latchkey ``config.json``, or ``None`` when it has none."
    )
    permissions_json: str | None = Field(
        description=(
            "The policy the machine's gateway enforces, restored to the form this computer keeps (see "
            "``_machine_policy``), or ``None`` when it has none yet."
        )
    )
    desktop_egress_rules_json: str | None = Field(
        description="The desktop egress rules the machine's curl router reads, or ``None`` when it has no rules file."
    )
    permissions_format_version: PermissionsFormatVersion = Field(
        description=(
            "The format the machine's policy is written in (see "
            ":mod:`imbue.mngr_latchkey.migrations.runner`); 0 for a machine never stamped."
        )
    )
    policy_fingerprint: MachinePolicyFingerprint | None = Field(
        description=(
            "What the machine says identifies its policy, its desktop egress rules and its format stamp as they "
            "are now, or ``None`` when its package does not say."
        )
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
    listen_password: str | None = Field(
        default=None, description="The machine's own gateway listen password, adopted the way the key is."
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
    permissions_json: str | None = Field(
        default=None,
        description=(
            "The policy the gateway is to enforce, as this computer keeps it; the machine's projection of it is "
            "what travels (see ``_machine_policy``)."
        ),
    )
    permissions_format_version: PermissionsFormatVersion | None = Field(
        default=None,
        description="The format the policy this update carries is written in, installed after it.",
    )
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
    expected_policy_fingerprint: MachinePolicyFingerprint | None = Field(
        default=None,
        description=(
            "The fingerprint the machine gave of the policy this update was made from. A machine holding any "
            "other applies none of the update; ``None`` applies it over whatever the machine holds."
        ),
    )


def announce_device(host: OuterHostInterface, record: DeviceRecord, host_id: HostId) -> None:
    """Record this computer as connected to ``host_id``'s machine (or refresh the record), in one round trip.

    Raises:
        MachineNewerError: when the machine's software is of a newer generation than this build's.
        MachineTransferError: when the command fails.
    """
    _run_remote_command(host, host_id, _ANNOUNCE_DEVICE_SUBCOMMAND, _announcement_document(record))


def forget_permission_request(host: OuterHostInterface, request_id: str, host_id: HostId) -> None:
    """Drop the machine's copy of a permission request and withdraw it from every connected desktop, in one round trip.

    The withdrawal is best-effort on the machine's side: a desktop that could
    not be reached syncs the request away itself when it next connects.

    Raises:
        MachineNewerError: when the machine's software is of a newer generation than this build's.
        MachineTransferError: when the command fails.
    """
    _run_remote_command(host, host_id, _FORGET_REQUEST_SUBCOMMAND, {_ENTRY_REQUEST_ID: request_id.encode("utf-8")})


def read_filed_permission_requests(host: OuterHostInterface, host_id: HostId) -> tuple[FiledPermissionRequest, ...]:
    """The permission requests ``host_id``'s machine keeps for the user's desktops, in one round trip.

    Raises:
        MachineNewerError: when the machine's software is of a newer generation than this build's.
        MachineTransferError: when the command fails, or answers with
            something this build cannot read.
    """
    document: dict[str, bytes] = {}
    stdout = _run_remote_command(host, host_id, _LIST_REQUESTS_SUBCOMMAND, document)
    try:
        return parse_filed_permission_requests(_text(_parse_answers(stdout), _ANSWER_FILED_PERMISSION_REQUESTS))
    except (_UnreadableAnswerError, FiledPermissionRequestsError) as e:
        raise _command_parts(_LIST_REQUESTS_SUBCOMMAND, document).failure(host, host_id, str(e)) from e


def read_remote_state(host: OuterHostInterface, request: RemoteStateRequest, host_id: HostId) -> RemoteMachineState:
    """Ask ``host_id``'s machine what it holds, in one round trip.

    Raises:
        MachineNewerError: when the machine's software is of a newer generation than this build's.
        MachineTransferError: when the command fails, or answers with
            something this build cannot read.
    """
    document = _request_document(request)
    stdout = _run_remote_command(host, host_id, _READ_STATE_SUBCOMMAND, document)
    try:
        return _machine_state_from_answers(_parse_answers(stdout))
    except _UnreadableAnswerError as e:
        raise _command_parts(_READ_STATE_SUBCOMMAND, document).failure(host, host_id, str(e)) from e


def apply_remote_state(
    host: OuterHostInterface,
    update: RemoteStateUpdate,
    host_id: HostId,
    # what the machine says identifies the policy it holds once the update is applied, or ``None`` when its
    # package does not say
) -> MachinePolicyFingerprint | None:
    """Make ``host_id``'s machine hold ``update``, in one round trip.

    An update that names the policy it was made from cannot be replayed the
    way the host layer replays a command whose connection dropped: a second
    run of one that had applied finds the policy its own first run installed,
    and reports it changed. The caller's read of the machine then adopts what
    that first run left.

    Raises:
        MachineNewerError: when the machine's software is of a newer generation than this build's.
        MachinePolicyChangedError: when the update names the policy it was
            made from and the machine holds another. Nothing was applied.
        MachineTransferError: when the update does not fit one command, the
            command fails, or it answers with something this build cannot read.
    """
    document = _update_document(update)
    stdout = _run_remote_command(host, host_id, _APPLY_STATE_SUBCOMMAND, document)
    try:
        return _policy_fingerprint(_parse_answers(stdout))
    except _UnreadableAnswerError as e:
        raise _command_parts(_APPLY_STATE_SUBCOMMAND, document).failure(host, host_id, str(e)) from e


@pure
def _command_parts(subcommand: str, document: Mapping[str, bytes]) -> _CommandParts:
    """What ``subcommand`` carries each way with ``document`` on its stdin.

    Every entry is a part of what is sent or asks for an answer, so an entry
    added to a document without a part in :data:`_SENT_PART_BY_ENTRY` fails
    every command that sends it rather than going unnamed.
    """
    return _CommandParts(
        sent=frozenset(_SENT_PART_BY_ENTRY[entry] for entry in document if entry not in _EXPECTED_PART_BY_QUERY_ENTRY),
        expected=_ANSWERED_PARTS_BY_SUBCOMMAND[subcommand]
        | frozenset(
            _EXPECTED_PART_BY_QUERY_ENTRY[entry] for entry in document if entry in _EXPECTED_PART_BY_QUERY_ENTRY
        ),
    )


@pure
def _request_document(request: RemoteStateRequest) -> dict[str, bytes]:
    document: dict[str, bytes] = {}
    if request.is_credential_store_included:
        document[_ENTRY_INCLUDE_CREDENTIAL_STORE] = _FLAG_TRUE
    if request.container_host_id is not None:
        document[_ENTRY_CONTAINER_HOST_ID] = str(request.container_host_id).encode("utf-8")
    return document


@pure
def _update_document(update: RemoteStateUpdate) -> dict[str, bytes]:
    document: dict[str, bytes] = {}
    if update.expected_policy_fingerprint is not None:
        document[_ENTRY_EXPECTED_POLICY_FINGERPRINT] = update.expected_policy_fingerprint.encode("utf-8")
    if update.is_credential_store_abandoned:
        document[_ENTRY_ABANDON_CREDENTIAL_STORE] = _FLAG_TRUE
    if update.encryption_key is not None:
        document[_ENTRY_ENCRYPTION_KEY] = update.encryption_key.get_secret_value().encode("utf-8")
    if update.listen_password is not None:
        document[_ENTRY_LISTEN_PASSWORD] = update.listen_password.encode("utf-8")
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
        document[_ENTRY_PERMISSIONS_JSON] = project_policy_for_machine(update.permissions_json).encode("utf-8")
    if update.permissions_format_version is not None:
        document[_ENTRY_PERMISSIONS_FORMAT_VERSION] = str(update.permissions_format_version).encode("utf-8")
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
def _announcement_document(record: DeviceRecord) -> dict[str, bytes]:
    return {
        _ENTRY_DEVICE_ID: str(record.device_id).encode("utf-8"),
        _ENTRY_DEVICE_RECORD_JSON: record.model_dump_json().encode("utf-8"),
    }


@pure
def machine_generation_check(
    generation_file: Path,
    # What a command that stops at a newer machine runs before it exits, or ``None`` for nothing.
    command_before_exit: str | None,
) -> str:
    """The POSIX ``sh`` lines that stop a command on a machine whose software is of a newer generation than this build's.

    Put at the front of a remote command, so the check costs no round trip. On
    a newer machine the command exits successfully with
    :data:`OUTCOME_MACHINE_NEWER_MARKER` as its last line, having run nothing
    else. A machine with no generation file, an empty one, or one that does not
    hold a whole number is of generation 1.
    """
    return "\n".join(
        (
            f'_lk_generation="$(cat {shlex.quote(str(generation_file))} 2>/dev/null || true)"',
            'case "$_lk_generation" in',
            "  ''|*[!0-9]*) _lk_generation=1 ;;",
            "esac",
            f'if [ "$_lk_generation" -gt {MACHINE_GENERATION} ]; then',
            *((f"  {command_before_exit}",) if command_before_exit is not None else ()),
            f"  printf '%s\\n' '{OUTCOME_MACHINE_NEWER_MARKER}'",
            "  exit 0",
            "fi",
        )
    )


@pure
def is_machine_newer_outcome(stdout: str) -> bool:
    """Whether a command's output says it stopped at a machine of a newer generation (see :func:`machine_generation_check`)."""
    lines = stdout.strip().splitlines()
    return bool(lines) and lines[-1] == OUTCOME_MACHINE_NEWER_MARKER


@pure
def _remote_command(subcommand: str, document: Mapping[str, bytes]) -> str:
    """The one command string that runs ``subcommand`` on the machine with ``document`` on its stdin.

    The generation check goes first, so the subcommand is never run on a
    machine whose software this build does not drive.
    """
    lines = [f"{key} {base64.b64encode(value).decode('ascii')}" for key, value in document.items()]
    return "\n".join(
        (
            machine_generation_check(MACHINE_GENERATION_FILE, None),
            f"{REMOTE_COMMAND_NAME} {shlex.quote(subcommand)} <<'{_DOCUMENT_END_MARKER}'",
            *lines,
            _DOCUMENT_END_MARKER,
        )
    )


def _run_remote_command(
    host: OuterHostInterface, host_id: HostId, subcommand: str, document: Mapping[str, bytes]
) -> str:
    """Run ``subcommand`` with ``document`` on its stdin and return its stdout, once its last line says it ran to its end.

    The command's text is kept out of the logs (see
    :data:`SECRET_BEARING_COMMAND_LOG_REASON`); the host layer traces a
    stand-in naming the kind of command and its size instead.

    Raises:
        MachineNewerError: when the machine's software is of a newer
            generation than this build's, so the command did not run.
        MachinePolicyChangedError: when the command was an update made from
            another policy than the machine holds, so the machine applied none
            of it.
        MachineTransferError: when the command does not fit one remote
            command, never reaches the machine or loses it midway, is refused
            or failed by the machine, or finishes without reporting an outcome
            (which means it did not run to its end).
    """
    parts = _command_parts(subcommand, document)
    command = _remote_command(subcommand, document)
    command_size = len(command.encode("utf-8"))
    if command_size > _MAX_REMOTE_COMMAND_BYTES:
        raise parts.failure(
            host,
            host_id,
            f"the single command that would carry it is {command_size} bytes, past the {_MAX_REMOTE_COMMAND_BYTES} "
            "a remote shell accepts",
        )
    started_at = time.monotonic()
    try:
        with commands_kept_out_of_logs(SECRET_BEARING_COMMAND_LOG_REASON):
            result = host.execute_idempotent_command(command, timeout_seconds=REMOTE_LATCHKEY_TIMEOUT_SECONDS)
    except _COMMAND_TRANSPORT_FAILURES as e:
        raise parts.failure(host, host_id, str(e)) from e
    elapsed_seconds = time.monotonic() - started_at
    # The answers a read prints before it can fail carry the machine's secrets,
    # so a failure is described by everything else the command printed.
    diagnostic_lines = _diagnostic_lines(result.stdout)
    if result.success and elapsed_seconds > _SLOW_REMOTE_COMMAND_WARNING_THRESHOLD_SECONDS:
        logger.warning(
            "Ran a latchkey command to {} VPS {} for host {} in {:.0f}s",
            parts.describe(),
            host.get_name(),
            host_id,
            elapsed_seconds,
        )
    # A script that ran to its end may still have something to say: a warning
    # from a latchkey invocation in the middle of it. The log guard hides the command's text,
    # not what the machine printed, and no script writes a secret to stderr.
    if result.success and result.stderr.strip():
        logger.debug(
            "The machine said on stderr while a latchkey command to {} VPS {} for host {} ran: {}",
            parts.describe(),
            host.get_name(),
            host_id,
            result.stderr.strip(),
        )
    if not result.success:
        detail = result.stderr.strip() or "\n".join(diagnostic_lines)
        raise parts.failure(host, host_id, summarize_latchkey_failure(detail, "the command reported no reason"))
    if is_machine_newer_outcome(result.stdout):
        raise MachineNewerError(host.get_name())
    stdout_lines = result.stdout.strip().splitlines()
    last_line = stdout_lines[-1] if stdout_lines else ""
    if last_line == OUTCOME_POLICY_CHANGED_MARKER:
        raise MachinePolicyChangedError(
            host_id, f"VPS {host.get_name()} holds another policy than the one the push was made from"
        )
    if last_line != OUTCOME_DONE_MARKER:
        last_diagnostic_line = diagnostic_lines[-1] if diagnostic_lines else ""
        raise parts.failure(
            host,
            host_id,
            f"the command finished without reporting an outcome (last output line: {last_diagnostic_line!r})",
        )
    return result.stdout


@pure
def _diagnostic_lines(stdout: str) -> list[str]:
    """The non-empty lines of a command's stdout that are not answers (nor the outcome marker)."""
    return [line for line in stdout.splitlines() if line.strip() and not line.startswith(ANSWER_PREFIX)]


def _parse_answers(stdout: str) -> dict[str, bytes]:
    """The prefixed, base64-encoded lines of a read's output, decoded and keyed by answer name.

    Raises:
        _UnreadableAnswerError: when an answer is not the base64 the script is
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
            raise _UnreadableAnswerError(f"the machine's {name} answer is not the base64 it should be: {e}") from e
    return answers


def _machine_state_from_answers(answers: Mapping[str, bytes]) -> RemoteMachineState:
    """Raises :class:`_UnreadableAnswerError` when an answer the read always gives is missing, or one is not text."""
    package_version = _text(answers, _ANSWER_PACKAGE_VERSION)
    home = _text(answers, _ANSWER_HOME)
    has_credential_store = _flag(answers, _ANSWER_HAS_CREDENTIAL_STORE)
    has_container_tunnel_key = _flag(answers, _ANSWER_HAS_CONTAINER_TUNNEL_KEY)
    container_extra_hosts_json = _optional_text(answers, _ANSWER_CONTAINER_EXTRA_HOSTS)
    data_format_version = _optional_text(answers, _ANSWER_DATA_FORMAT_VERSION)
    encryption_key = _optional_text(answers, _ANSWER_ENCRYPTION_KEY)
    listen_password = _optional_text(answers, _ANSWER_LISTEN_PASSWORD)
    config_json = _optional_text(answers, _ANSWER_CONFIG_JSON)
    machine_permissions_json = _optional_text(answers, _ANSWER_PERMISSIONS_JSON)
    permissions_json = (
        restore_policy_from_machine(machine_permissions_json) if machine_permissions_json is not None else None
    )
    desktop_egress_rules_json = _optional_text(answers, _ANSWER_DESKTOP_EGRESS_RULES_JSON)
    permissions_format_version = _permissions_format_version(answers)
    policy_fingerprint = _policy_fingerprint(answers)
    credentials = answers.get(_ANSWER_CREDENTIALS)
    if (credentials is None) != (data_format_version is None):
        raise _UnreadableAnswerError(
            "the machine's credential store came back without the format stamp that says how to read it"
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
        permissions_format_version=permissions_format_version,
        policy_fingerprint=policy_fingerprint,
        credential_store=(
            EncryptedCredentialStore(content=credentials, data_format_version=data_format_version)
            if credentials is not None and data_format_version is not None
            else None
        ),
        has_container_tunnel_key=has_container_tunnel_key,
        container_extra_hosts=(
            _container_extra_hosts(container_extra_hosts_json) if container_extra_hosts_json is not None else None
        ),
    )


def _permissions_format_version(answers: Mapping[str, bytes]) -> PermissionsFormatVersion:
    """The format version the machine's policy is stamped with; 0 when it never was.

    Raises:
        _UnreadableAnswerError: when the stamp is not the non-negative integer
            the desktop writes.
    """
    answer = _optional_text(answers, _ANSWER_PERMISSIONS_FORMAT_VERSION)
    if answer is None:
        return PermissionsFormatVersion(0)
    try:
        return PermissionsFormatVersion(int(answer.strip()))
    except ValueError as e:
        raise _UnreadableAnswerError(
            f"the machine's permissions format version is not the non-negative integer it should be: {answer!r}"
        ) from e


def _policy_fingerprint(answers: Mapping[str, bytes]) -> MachinePolicyFingerprint | None:
    """The fingerprint the machine gave of its policy; ``None`` when its package gives none.

    Raises:
        _UnreadableAnswerError: when the fingerprint is not text.
    """
    answer = _optional_text(answers, _ANSWER_POLICY_FINGERPRINT)
    return MachinePolicyFingerprint(answer) if answer else None


def _container_extra_hosts(answer: str) -> tuple[str, ...]:
    """The mappings ``docker inspect`` printed for the container, as it prints them: a JSON list, or ``null`` for none.

    Raises:
        _UnreadableAnswerError: when the answer is not that.
    """
    try:
        extra_hosts = json.loads(answer)
    except json.JSONDecodeError as e:
        raise _UnreadableAnswerError(
            f"the machine's container extra hosts answer is not the JSON docker prints: {e}"
        ) from e
    if extra_hosts is None:
        return ()
    if not isinstance(extra_hosts, list) or not all(isinstance(entry, str) for entry in extra_hosts):
        raise _UnreadableAnswerError(
            f"the machine's container extra hosts answer is not the list of strings docker prints: {answer!r}"
        )
    return tuple(extra_hosts)


def _required_answer(answers: Mapping[str, bytes], name: str) -> bytes:
    """Raises :class:`_UnreadableAnswerError` when the machine did not answer ``name``, which every read must."""
    try:
        return answers[name]
    except KeyError as e:
        raise _UnreadableAnswerError(f"the machine answered without {name}") from e


def _decoded_answer(value: bytes, name: str) -> str:
    """Raises :class:`_UnreadableAnswerError` when the ``name`` answer is not the UTF-8 text it should be."""
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as e:
        raise _UnreadableAnswerError(f"the machine's {name} answer is not the UTF-8 text it should be: {e}") from e


def _text(answers: Mapping[str, bytes], name: str) -> str:
    return _decoded_answer(_required_answer(answers, name), name)


def _flag(answers: Mapping[str, bytes], name: str) -> bool:
    return _required_answer(answers, name) == _FLAG_TRUE


def _optional_text(answers: Mapping[str, bytes], name: str) -> str | None:
    value = answers.get(name)
    return _decoded_answer(value, name) if value is not None else None
