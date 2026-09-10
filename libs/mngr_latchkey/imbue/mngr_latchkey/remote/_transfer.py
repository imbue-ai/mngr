"""Moving credential material between this computer and a machine's own store.

The machine keeps its store under its own key, and the desktop keeps every
machine store under the desktop's, so every transfer re-encrypts at the
boundary: a store leaving the machine is re-encrypted for the desktop by the
machine, and a bundle heading to the machine is re-encrypted with the machine's
key here. Neither side's key is ever handed to the other.

Everything this computer *pushes* to a machine -- a connect, a disconnect, a
permissions snapshot, or a permission grant (a connect and a snapshot together)
-- travels as one POSIX ``sh`` script the machine runs in a single remote
command (:func:`build_machine_script`): the payloads ride inside it
base64-encoded, it expands ``$HOME`` itself rather than making this computer
probe for it, and it reports on its last line of output what it did. So a push
costs one round trip, whatever it is, and the machine's ``~/.latchkey`` never
has to be resolved from here for one.

The machine's own key never travels with a push. A script that has to run
``latchkey`` reads the key from the machine's tmpfs itself, and a machine whose
RAM-backed copy is gone (it rebooted) reports that instead of failing: the key
is written back over SFTP and the script runs once more, which is the only case
that costs a second round trip. A script *bringing* credential material also
checks the SHA-256 of the machine's key against the recorded key's, so a
machine re-keyed from another computer is refused rather than handed a store
its gateway could not read.

Reading a machine back (:func:`fetch_machine_state`) is one script too: it has
the machine re-encrypt its store for the desktop and prints that, its format
stamp, and the policy it enforces as base64 lines of the same command's output,
so opening a workspace's Permissions tab costs one round trip rather than one
per file.
"""

import base64
import hashlib
import shlex
import tempfile
from enum import auto
from pathlib import Path
from typing import Final
from typing import assert_never

from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr_latchkey.core import CONFIG_FILENAME
from imbue.mngr_latchkey.core import CREDENTIALS_STORE_FILENAME
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.core import PERMISSIONS_CONFIG_FILENAME
from imbue.mngr_latchkey.core import UPSTREAM_DATA_FORMAT_VERSION_FILENAME
from imbue.mngr_latchkey.core import summarize_latchkey_failure
from imbue.mngr_latchkey.encryption_key import LatchkeyEncryptionKeyPermissionError
from imbue.mngr_latchkey.encryption_key import load_or_create_encryption_key
from imbue.mngr_latchkey.remote._machine import DIFFERENT_MACHINE_KEY_MESSAGE
from imbue.mngr_latchkey.remote._machine import GATEWAY_ENCRYPTION_KEY_FILENAME
from imbue.mngr_latchkey.remote._machine import REMOTE_FILE_MODE
from imbue.mngr_latchkey.remote._machine import REMOTE_LATCHKEY_DIR_NAME
from imbue.mngr_latchkey.remote._machine import REMOTE_LATCHKEY_TIMEOUT_SECONDS
from imbue.mngr_latchkey.remote._machine import TMPFS_SECRETS_DIR
from imbue.mngr_latchkey.remote._mirror import clear_machine_credentials
from imbue.mngr_latchkey.remote._mirror import machine_store_dir
from imbue.mngr_latchkey.remote._mirror import write_machine_credentials
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import load_permissions_from_text
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir

# Prefix of the directory, under the machine's own latchkey directory, that a
# credential handover passes through: a store re-encrypted for the desktop on
# its way out, a bundle waiting to be merged on its way in. Suffixed with the
# script's PID, because two transfers can overlap on one machine (a Permissions
# tab refresh racing a grant, or another of the user's computers pushing) and
# each one both wipes its scratch before use and removes it on exit, so a shared
# name would have one transfer delete what the other was about to read.
_SCRATCH_DIR_PREFIX: Final[str] = "mngr-latchkey-transfer."

# A script that dies without running its EXIT trap (the SSH session dropped,
# a ``kill -9``) leaves its scratch behind; a scratch this old is nobody's, and
# the next transfer collects it rather than letting them pile up.
_STALE_SCRATCH_AGE_MINUTES: Final[int] = 60

# A machine script travels as one argument of the remote ``sh -c``, and Linux
# caps any one argv string at 128 KiB (``MAX_ARG_STRLEN``). The base64 payloads
# a script embeds are a few KiB each in practice; a script past this size is
# refused rather than carried by a second, slower code path.
_MAX_MACHINE_SCRIPT_BYTES: Final[int] = 100_000

# A machine script's last stdout line names its outcome, so a run that exits 0
# without doing the work (the machine's tmpfs key is gone) is told apart from
# one that applied everything.
_SCRIPT_OUTCOME_PREFIX: Final[str] = "MNGR_LATCHKEY_OUTCOME="

# Prefixes a read script's own answers carry, one per line, each holding a
# base64 payload. Prefixed rather than positional because the ``latchkey``
# invocation in the middle of the script writes to the same stdout, so an
# answer has to be recognizable among whatever the CLI chose to say.
_READ_PERMISSIONS_PREFIX: Final[str] = "MNGR_LATCHKEY_PERMISSIONS="
_READ_CREDENTIALS_PREFIX: Final[str] = "MNGR_LATCHKEY_CREDENTIALS="
_READ_DATA_FORMAT_VERSION_PREFIX: Final[str] = "MNGR_LATCHKEY_DATA_FORMAT_VERSION="

# What upstream ``latchkey auth re-encrypt`` says, on stderr and with a non-zero
# exit, when the store it was pointed at holds nothing. A machine whose last
# account was disconnected is in exactly that state: ``auth clear`` empties the
# store rather than deleting the file, so the file is still there for the next
# read to point a re-encrypt at. That is an ordinary machine holding nothing,
# not a failure, so a read that sees this answers "no credentials" instead of
# failing the whole Permissions tab.
_EMPTY_STORE_MESSAGE: Final[str] = "No stored credentials found to re-encrypt."


class _MachineScriptOutcome(UpperCaseStrEnum):
    """What a single-round-trip machine script reports having done."""

    APPLIED = auto()
    # The RAM-backed key file is gone (the machine rebooted), so nothing could
    # be decrypted; the caller writes the key back and runs the script again.
    KEY_MISSING = auto()


class _CredentialChange(FrozenModel):
    """What a machine script does to the machine's own credential store.

    Either form rewrites that store, so either runs ``latchkey`` under the key
    the machine's gateway is running under -- read from its tmpfs file by the
    script, never carried in the command line -- and either stops to report a
    machine that has lost that key to a reboot.
    """

    machine_key_file: Path = Field(description="Where the machine keeps the key its gateway runs under (tmpfs).")
    service_name: str = Field(description="Catalog service the change concerns.")
    account: str = Field(description="The one account the change concerns; see each subclass for what empty means.")


class _CredentialMerge(_CredentialChange):
    """Add one service (one account of it, when named) to what the machine already holds."""

    machine_key_sha256: str = Field(
        description=(
            "Hex SHA-256 of the key this computer recorded for the machine, which the script refuses to go on "
            "without matching: the bundle is encrypted with the recorded key, so merging it into a machine "
            "re-keyed from another computer would leave its gateway a store it cannot read. Only the hash "
            "travels, so the key itself never rides in the command line."
        )
    )
    bundle: bytes = Field(description="The credential store to merge, already encrypted with the machine's key.")
    data_format_version: str = Field(description="The upstream format stamp the bundle was written in.")


class _CredentialClear(_CredentialChange):
    """Take one account of one service away from the machine's store.

    Carries no key hash, deliberately: a clear brings nothing of this
    computer's to write, it decrypts and rewrites the machine's store under
    whatever key that machine is running under. Refusing a machine re-keyed
    from another computer would leave a credential the user signed out of
    sitting on it, which is the one outcome a sign-out must not have.
    """


class _MachineScriptInputs(FrozenModel):
    """Everything a single-round-trip machine script carries to the machine.

    Every part is optional and a script does whichever it was given, in this
    order: the config first, because a gateway with no entry for a service
    cannot route a request to it, so nothing that refers to the service means
    anything until the machine's config names it; then the credential; then the
    policy, because a rule the machine cannot yet exercise would send the agent
    back to a request it had already answered.
    """

    config_json: str | None = Field(
        default=None,
        description=(
            "This package's half of the machine's config.json -- the hidden built-in services and every "
            "registered service -- to install as a whole snapshot, or ``None`` to leave the machine's config alone."
        ),
    )
    credential_change: _CredentialMerge | _CredentialClear | None = Field(
        description="What to do to the machine's credential store, or ``None`` for a script that only sets a policy."
    )
    permissions_json: str | None = Field(
        description="The full policy the machine should enforce afterwards, or ``None`` to leave its policy alone."
    )


class FetchedMachineState(FrozenModel):
    """Everything a machine holds that this computer shows or edits, as of one read."""

    credentials: bytes | None = Field(
        description="The machine's store as this computer can read it, or ``None`` when it holds nothing yet."
    )
    data_format_version: str = Field(
        default="", description="The format stamp the store came with; empty exactly when there is no store."
    )
    permissions_json: str | None = Field(
        description="The policy the machine's gateway enforces, or ``None`` when it has none yet."
    )


@pure
def build_machine_read_script(desktop_key: SecretStr, machine_key_file: Path) -> str:
    """Render the POSIX ``sh`` script that reads a machine's whole state in one command.

    The machine keeps its store under its own key and the desktop keeps every
    machine store under the desktop's, so the copy is re-encrypted on the way
    out -- by the machine, into a scratch directory of its own, which is what
    keeps the desktop's key from being written anywhere on the VPS. The scratch
    copy is removed by the ``EXIT`` trap whether or not the read worked.

    Each answer is printed as one prefixed, base64-encoded line, so the
    ``latchkey`` invocation in the middle of the script can say whatever it
    likes on the same stdout without being mistaken for one.

    A machine holding an emptied store answers as one holding nothing (see
    :data:`_EMPTY_STORE_MESSAGE`), so the re-encrypt's stderr is kept rather
    than let through: it is what tells that apart from a real failure, and it
    is reprinted unchanged when it turns out to be one.
    """
    return "\n".join(
        (
            "set -eu",
            "umask 077",
            f'_lk_remote_dir="$HOME/{REMOTE_LATCHKEY_DIR_NAME}"',
            *_scratch_dir_lines(),
            "trap 'rm -rf \"$_lk_scratch\"' EXIT",
            f"_lk_key_file={shlex.quote(str(machine_key_file))}",
            f"_lk_out_key={shlex.quote(desktop_key.get_secret_value())}",
            f'if [ -f "$_lk_remote_dir/{PERMISSIONS_CONFIG_FILENAME}" ]; then',
            f'  echo "{_READ_PERMISSIONS_PREFIX}$(base64 < "$_lk_remote_dir/{PERMISSIONS_CONFIG_FILENAME}" '
            "| tr -d '\\n')\"",
            "fi",
            f'if [ -s "$_lk_remote_dir/{CREDENTIALS_STORE_FILENAME}" ]; then',
            '  if ! [ -s "$_lk_key_file" ]; then',
            f"    echo {shlex.quote(_outcome_marker(_MachineScriptOutcome.KEY_MISSING))}",
            "    exit 0",
            "  fi",
            '  LATCHKEY_ENCRYPTION_KEY="$(cat "$_lk_key_file")"',
            "  export LATCHKEY_ENCRYPTION_KEY",
            '  rm -rf "$_lk_scratch"',
            '  mkdir -p "$_lk_scratch"',
            '  _lk_reencrypt_stderr="$_lk_scratch/re-encrypt.stderr"',
            '  if printf \'%s\' "$_lk_out_key" | LATCHKEY_DIRECTORY="$_lk_remote_dir" latchkey auth re-encrypt '
            '"$_lk_scratch" 2>"$_lk_reencrypt_stderr"; then',
            f'    echo "{_READ_CREDENTIALS_PREFIX}$(base64 < "$_lk_scratch/{CREDENTIALS_STORE_FILENAME}" '
            "| tr -d '\\n')\"",
            f'    echo "{_READ_DATA_FORMAT_VERSION_PREFIX}$(base64 '
            f'< "$_lk_remote_dir/{UPSTREAM_DATA_FORMAT_VERSION_FILENAME}"'
            " | tr -d '\\n')\"",
            f'  elif ! grep -qF {shlex.quote(_EMPTY_STORE_MESSAGE)} "$_lk_reencrypt_stderr"; then',
            '    cat "$_lk_reencrypt_stderr" >&2',
            "    exit 1",
            "  fi",
            "fi",
            f"echo {shlex.quote(_outcome_marker(_MachineScriptOutcome.APPLIED))}",
        )
    )


def fetch_machine_state(
    host: OuterHostInterface,
    latchkey: Latchkey,
    host_id: HostId,
    machine_key: SecretStr,
) -> FetchedMachineState:
    """Read what a machine holds -- its credentials and its policy -- in one round trip.

    Deliberately does not write anything here: the caller decides what to adopt
    (see :func:`adopt_machine_state`), and a read that lands over this
    computer's copies before the caller has looked at them would erase the
    comparison it came to make.

    A machine with no store yet (nothing has ever been connected for it), or
    with no policy yet (nothing has provisioned it), reports those as ``None``
    rather than as an error.

    Raises:
        RemoteGatewayError: when the machine cannot produce the copy, or what it
            produced cannot be read.
    """
    script = build_machine_read_script(
        _desktop_encryption_key(latchkey), TMPFS_SECRETS_DIR / GATEWAY_ENCRYPTION_KEY_FILENAME
    )
    with log_span("Reading the latchkey state of host {} from VPS {}", host_id, host.get_name()):
        stdout = _run_machine_script_with_key_retry(
            host, host_id, script, machine_key, failure_description=f"read the latchkey state of host {host_id}"
        )
    credentials = _decoded_answer(host_id, stdout, _READ_CREDENTIALS_PREFIX)
    data_format_version = _decoded_answer(host_id, stdout, _READ_DATA_FORMAT_VERSION_PREFIX)
    permissions = _decoded_answer(host_id, stdout, _READ_PERMISSIONS_PREFIX)
    if credentials is not None and not data_format_version:
        raise RemoteGatewayError(
            f"Failed to read the latchkey state of host {host_id} from VPS {host.get_name()}: its credential store "
            "came back without the format stamp that says how to read it"
        )
    return FetchedMachineState(
        credentials=credentials,
        data_format_version=data_format_version.decode("utf-8") if data_format_version is not None else "",
        permissions_json=permissions.decode("utf-8") if permissions is not None else None,
    )


def adopt_machine_credentials(latchkey: Latchkey, host_id: HostId, fetched: FetchedMachineState) -> None:
    """Make the machine store say exactly what the machine holds."""
    data_dir = plugin_data_dir(latchkey.latchkey_directory)
    store_dir = machine_store_dir(data_dir, host_id)
    if fetched.credentials is None:
        clear_machine_credentials(store_dir)
    else:
        write_machine_credentials(store_dir, fetched.credentials, fetched.data_format_version)


def adopt_machine_permissions(latchkey_directory: Path, host_id: HostId, permissions_json: str) -> None:
    """Take the machine's policy as this computer's copy of it.

    How a second computer learns what the first one granted, and how this one
    catches up on anything granted while it was not running. Validated before it
    is stored -- a policy this build cannot read must not become this computer's
    copy -- and written only when it actually differs, so an unchanged policy
    costs nothing.

    Raises:
        RemoteGatewayError: when the policy is not one this build understands,
            or cannot be stored.
    """
    try:
        load_permissions_from_text(permissions_json)
    except LatchkeyStoreError as e:
        raise RemoteGatewayError(
            f"The machine of host {host_id} holds a permissions file this build cannot read: {e}"
        ) from e
    local_path = permissions_path_for_host(plugin_data_dir(latchkey_directory), host_id)
    try:
        if not local_path.is_file() or local_path.read_text() != permissions_json:
            atomic_write(local_path, permissions_json)
    except OSError as e:
        raise RemoteGatewayError(f"Failed to store the permissions of host {host_id} at {local_path}: {e}") from e


def _decoded_answer(host_id: HostId, stdout: str, prefix: str) -> bytes | None:
    """Decode the one line of a read script's output that carries ``prefix``, or ``None`` if it printed none.

    Raises:
        RemoteGatewayError: when the line is there but is not the base64 the
            script is supposed to have written.
    """
    for line in stdout.splitlines():
        if line.startswith(prefix):
            try:
                return base64.b64decode(line[len(prefix) :], validate=True)
            except ValueError as e:
                raise RemoteGatewayError(
                    f"Failed to read the latchkey state of host {host_id}: the machine's {prefix} answer is not "
                    f"the base64 it should be: {e}"
                ) from e
    return None


def push_credentials(
    host: OuterHostInterface,
    machine_latchkey: Latchkey,
    host_id: HostId,
    service_name: str,
    account: str,
    machine_key: SecretStr,
    config_json: str | None = None,
) -> None:
    """Add one service's credentials -- one account of it, when named -- to the machine's own store, in one round trip.

    Merged rather than written over: the machine's store is its own, and the
    accounts already in it -- with whatever its gateway has refreshed since --
    must survive gaining a new one. The bundle is re-encrypted with the
    machine's key here, so what crosses the wire is readable only by the
    machine it is for, and the merge on the far side reuses that same key.

    What the merge takes is decided by the flags on the merge itself, not by
    what happens to be in the bundle: the ``--services`` the machine is told to
    take is the only one it takes, and with ``--account`` it takes only that
    account of it, leaving the service's other accounts on the machine exactly
    as they were. That matters because the source of a wider merge would be
    this computer's copy, which is only as fresh as the last time it was read
    -- writing a stale copy of a service (or of a sibling account) the machine
    has since refreshed would hand back a refresh token the machine already
    rotated away.

    An empty ``account`` hands over the whole service (every account the
    machine store holds for it), which is what requests recorded before
    account-scoping existed mean -- and there is no other way to say "the
    unnamed default account", which is stored under the empty string.

    Raises:
        RemoteGatewayError: when the bundle cannot be built, or the machine
            refuses or fails the script.
    """
    with log_span("Adding {} to the credentials of host {} on VPS {}", service_name, host_id, host.get_name()):
        _apply_machine_script(
            host,
            host_id,
            _MachineScriptInputs(
                config_json=config_json,
                credential_change=_merge_of(machine_latchkey, host_id, service_name, account, machine_key),
                permissions_json=None,
            ),
            machine_key,
            failure_description=f"add {service_name} to the credentials of host {host_id}",
        )


def push_credentials_with_permissions(
    host: OuterHostInterface,
    machine_latchkey: Latchkey,
    host_id: HostId,
    service_name: str,
    account: str,
    machine_key: SecretStr,
    permissions_json: str,
    config_json: str | None = None,
) -> None:
    """Add one account to the machine's store and make ``permissions_json`` its policy, in one round trip.

    What a permission grant asks for: both halves land under one ``set -e``, so
    the policy is never enforceable before the credential it rides on is there,
    and neither half is reported done without the other.

    Raises:
        RemoteGatewayError: when the snapshot is not a policy this build can
            read, the bundle cannot be built, or the machine refuses or fails
            the script.
    """
    _validate_permissions_snapshot(host_id, permissions_json)
    with log_span(
        "Adding {} to the credentials of host {} and applying its permissions on VPS {}",
        service_name,
        host_id,
        host.get_name(),
    ):
        _apply_machine_script(
            host,
            host_id,
            _MachineScriptInputs(
                config_json=config_json,
                credential_change=_merge_of(machine_latchkey, host_id, service_name, account, machine_key),
                permissions_json=permissions_json,
            ),
            machine_key,
            failure_description=f"add {service_name} to host {host_id} and apply its permissions",
        )


def clear_remote_credentials(
    host: OuterHostInterface,
    host_id: HostId,
    service_name: str,
    account: str,
    machine_key: SecretStr,
) -> None:
    """Clear one account of one service from the machine's own credential store, in one round trip.

    How a disconnection reaches a machine: the credential is the machine's, so
    the desktop cannot simply stop shipping it -- it has to be taken away. The
    clear rewrites the machine's store, so it runs under the key the machine's
    gateway is running under -- whichever that is (see :class:`_CredentialClear`).
    ``machine_key`` is only what a machine that lost its key to a reboot is
    handed back.

    Raises:
        RemoteGatewayError: when the machine refuses or fails the script.
    """
    with log_span("Disconnecting {} of host {} from VPS {}", service_name, host_id, host.get_name()):
        _apply_machine_script(
            host,
            host_id,
            _MachineScriptInputs(
                credential_change=_CredentialClear(
                    machine_key_file=TMPFS_SECRETS_DIR / GATEWAY_ENCRYPTION_KEY_FILENAME,
                    service_name=service_name,
                    account=account,
                ),
                permissions_json=None,
            ),
            machine_key,
            failure_description=f"disconnect {service_name} of host {host_id}",
        )


def push_permissions_snapshot(host: OuterHostInterface, host_id: HostId, permissions_json: str) -> None:
    """Make ``permissions_json`` the policy ``host_id``'s machine enforces, in one round trip.

    How a permissions edit made on this computer reaches the machine: as a full
    snapshot of the canonical per-host file, pushed the moment the edit is
    made. Validated before it is written -- a snapshot this build cannot parse
    must not become a machine's policy -- and installed atomically, so the
    gateway never reads a half-written file.

    The one push that carries no key gate: a policy is not encrypted, so it is
    applied to a machine whatever key that machine is running under, and to a
    machine that rebooted and is running under none.

    Raises:
        RemoteGatewayError: when the snapshot is not a policy this build can
            read, or the machine refuses or fails the script.
    """
    _validate_permissions_snapshot(host_id, permissions_json)
    with log_span("Applying a permissions snapshot for host {} to VPS {}", host_id, host.get_name()):
        _apply_machine_script(
            host,
            host_id,
            _MachineScriptInputs(credential_change=None, permissions_json=permissions_json),
            machine_key=None,
            failure_description=f"apply the permissions of host {host_id}",
        )


@pure
def build_machine_script(inputs: _MachineScriptInputs) -> str:
    """Render the POSIX ``sh`` script that carries one push to a machine in a single remote command.

    Every variable part is assigned to a ``_lk_*`` shell variable in the
    prologue and the body below refers only to those, so the body is the same
    for every push of a given shape and the payloads are visibly data rather
    than code. ``umask 077`` gives every file the script creates the 0600 an
    SFTP write would have had to ``chmod`` onto it, and the ``EXIT`` trap --
    set before anything is created, and naming only what this script can
    create -- removes the scratch material whether the script succeeds or
    fails.
    """
    prologue: list[str] = [
        "set -eu",
        "umask 077",
        f'_lk_remote_dir="$HOME/{REMOTE_LATCHKEY_DIR_NAME}"',
        'mkdir -p "$_lk_remote_dir"',
    ]
    body: list[str] = []
    scratch_variable_names: list[str] = []
    if inputs.config_json is not None:
        prologue.append(f'_lk_config_tmp="$_lk_remote_dir/.{CONFIG_FILENAME}.$$.tmp"')
        scratch_variable_names.append("_lk_config_tmp")
        body.extend(_config_lines(inputs.config_json))
    match inputs.credential_change:
        case _CredentialMerge() as merge:
            prologue.extend(_scratch_dir_lines())
            scratch_variable_names.append("_lk_scratch")
            body.extend(_machine_key_lines(merge, expected_key_sha256=merge.machine_key_sha256))
            body.extend(_merge_lines(merge))
        case _CredentialClear() as clear:
            body.extend(_machine_key_lines(clear, expected_key_sha256=None))
            body.extend(_clear_lines(clear))
        case None:
            pass
        case _ as unreachable:
            assert_never(unreachable)
    if inputs.permissions_json is not None:
        prologue.append(f'_lk_permissions_tmp="$_lk_remote_dir/.{PERMISSIONS_CONFIG_FILENAME}.$$.tmp"')
        scratch_variable_names.append("_lk_permissions_tmp")
        body.extend(_permissions_lines(inputs.permissions_json))
    if scratch_variable_names:
        removals = " ".join(f'"${name}"' for name in scratch_variable_names)
        prologue.append(f"trap 'rm -rf {removals}' EXIT")
    applied = f"echo {shlex.quote(_outcome_marker(_MachineScriptOutcome.APPLIED))}"
    return "\n".join((*prologue, *body, applied))


@pure
def _scratch_dir_lines() -> tuple[str, ...]:
    """Lines that name this script's own scratch directory and collect any stale ones.

    Only *names* it: whoever uses it creates it, right before writing into it.
    ``$$`` is unique among the shells alive on the machine at once, which is
    exactly what keeps overlapping transfers out of each other's scratch. The
    sweep prunes rather than descends, so a scratch is removed whole or left
    whole, and it ignores the scratch this script is about to create because
    that one does not exist yet.
    """
    return (
        f'_lk_scratch="$_lk_remote_dir/{_SCRATCH_DIR_PREFIX}$$"',
        f'find "$_lk_remote_dir" -maxdepth 1 -name {shlex.quote(_SCRATCH_DIR_PREFIX + "*")} '
        f"-mmin +{_STALE_SCRATCH_AGE_MINUTES} -prune -exec rm -rf {{}} + 2>/dev/null || true",
    )


@pure
def _machine_key_lines(change: _CredentialChange, expected_key_sha256: str | None) -> tuple[str, ...]:
    """Lines that read the key the machine's gateway runs under, and export it for the CLI.

    An absent key means a reboot wiped the RAM-backed copy: that is reported
    and the script stops, so the caller can write the key back and run it
    again. With ``expected_key_sha256``, a key that is *there* but not the one
    this computer recorded means another of the user's computers re-keyed the
    machine, and the script refuses exactly as
    :func:`~imbue.mngr_latchkey.remote._machine.write_machine_key_to_secrets_dir`
    refuses it.
    """
    key_must_match_lines = (
        (
            f"_lk_expected_key_sha256={shlex.quote(expected_key_sha256)}",
            "_lk_actual_key_sha256=\"$(tr -d '[:space:]' < \"$_lk_key_file\" | sha256sum | cut -d ' ' -f 1)\"",
            'if [ "$_lk_actual_key_sha256" != "$_lk_expected_key_sha256" ]; then',
            f"  echo {shlex.quote('Error: ' + DIFFERENT_MACHINE_KEY_MESSAGE)} >&2",
            "  exit 1",
            "fi",
        )
        if expected_key_sha256 is not None
        else ()
    )
    return (
        f"_lk_key_file={shlex.quote(str(change.machine_key_file))}",
        'if ! [ -s "$_lk_key_file" ]; then',
        f"  echo {shlex.quote(_outcome_marker(_MachineScriptOutcome.KEY_MISSING))}",
        "  exit 0",
        "fi",
        *key_must_match_lines,
        'LATCHKEY_ENCRYPTION_KEY="$(cat "$_lk_key_file")"',
        "export LATCHKEY_ENCRYPTION_KEY",
    )


@pure
def _merge_lines(merge: _CredentialMerge) -> tuple[str, ...]:
    """Lines that stage the bundle (with the format stamp it was written in) and merge only what was named."""
    account_scope = ' --account "$_lk_account"' if merge.account else ""
    return (
        f"_lk_service={shlex.quote(merge.service_name)}",
        f"_lk_account={shlex.quote(merge.account)}",
        f"_lk_data_format_version={shlex.quote(merge.data_format_version)}",
        f"_lk_bundle_b64={base64.b64encode(merge.bundle).decode('ascii')}",
        'rm -rf "$_lk_scratch"',
        'mkdir -p "$_lk_scratch"',
        f'printf \'%s\' "$_lk_bundle_b64" | base64 -d > "$_lk_scratch/{CREDENTIALS_STORE_FILENAME}"',
        f'printf \'%s\' "$_lk_data_format_version" > "$_lk_scratch/{UPSTREAM_DATA_FORMAT_VERSION_FILENAME}"',
        'printf \'\' | LATCHKEY_DIRECTORY="$_lk_scratch" latchkey auth re-encrypt "$_lk_remote_dir" '
        f'--services "$_lk_service"{account_scope}',
    )


@pure
def _clear_lines(clear: _CredentialClear) -> tuple[str, ...]:
    return (
        f"_lk_service={shlex.quote(clear.service_name)}",
        f"_lk_account={shlex.quote(clear.account)}",
        'LATCHKEY_DIRECTORY="$_lk_remote_dir" latchkey auth clear -y "$_lk_service" --account "$_lk_account"',
    )


@pure
def _config_lines(config_json: str) -> tuple[str, ...]:
    """Lines that install this package's half of the machine's config atomically, so the gateway never reads a half-written file."""
    return (
        f"_lk_config_b64={base64.b64encode(config_json.encode('utf-8')).decode('ascii')}",
        'printf \'%s\' "$_lk_config_b64" | base64 -d > "$_lk_config_tmp"',
        f'mv -f "$_lk_config_tmp" "$_lk_remote_dir/{CONFIG_FILENAME}"',
    )


@pure
def _permissions_lines(permissions_json: str) -> tuple[str, ...]:
    """Lines that install the policy atomically, so the gateway never reads a half-written file."""
    return (
        f"_lk_permissions_b64={base64.b64encode(permissions_json.encode('utf-8')).decode('ascii')}",
        'printf \'%s\' "$_lk_permissions_b64" | base64 -d > "$_lk_permissions_tmp"',
        f'mv -f "$_lk_permissions_tmp" "$_lk_remote_dir/{PERMISSIONS_CONFIG_FILENAME}"',
    )


def _apply_machine_script(
    host: OuterHostInterface,
    host_id: HostId,
    inputs: _MachineScriptInputs,
    machine_key: SecretStr | None,
    failure_description: str,
) -> None:
    """Run one push against the machine, writing its key back if a reboot lost it.

    ``machine_key`` is the key the script's gate expects, and is what makes the
    retry possible; it is ``None`` exactly for a push that needs no key at all,
    which therefore can never be told the key is missing.

    Raises:
        RemoteGatewayError: when the script does not fit one command, the
            machine refuses or fails it, or its key is still missing right
            after it was written back.
    """
    script = build_machine_script(inputs)
    script_size = len(script.encode("utf-8"))
    if script_size > _MAX_MACHINE_SCRIPT_BYTES:
        raise RemoteGatewayError(
            f"Failed to {failure_description} on VPS {host.get_name()}: the single command that would carry it is "
            f"{script_size} bytes, past the {_MAX_MACHINE_SCRIPT_BYTES} a remote shell accepts"
        )
    _run_machine_script_with_key_retry(host, host_id, script, machine_key, failure_description)


def _run_machine_script_with_key_retry(
    host: OuterHostInterface,
    host_id: HostId,
    script: str,
    machine_key: SecretStr | None,
    failure_description: str,
) -> str:
    """Run one script against the machine, writing its key back if a reboot lost it, and return its output.

    ``machine_key`` is the key the script expects to find on the machine, and is
    what makes the retry possible; it is ``None`` exactly for a script that
    needs no key at all, which therefore can never be told the key is missing.

    Raises:
        RemoteGatewayError: when the machine refuses or fails the script, or its
            key is still missing right after it was written back.
    """
    outcome, stdout = _run_machine_script(host, script, failure_description)
    match outcome:
        case _MachineScriptOutcome.APPLIED:
            return stdout
        case _MachineScriptOutcome.KEY_MISSING:
            pass
        case _ as unreachable:
            assert_never(unreachable)
    if machine_key is None:
        raise RemoteGatewayError(
            f"Failed to {failure_description} on VPS {host.get_name()}: the machine reported its encryption key "
            "missing for a command that does not use one"
        )
    # The script found no key to check against (the machine rebooted since it
    # was provisioned), so the write-back is not a blind overwrite.
    logger.debug("The machine of host {} has lost its tmpfs key; writing it back before retrying", host_id)
    host.write_file(
        TMPFS_SECRETS_DIR / GATEWAY_ENCRYPTION_KEY_FILENAME,
        machine_key.get_secret_value().encode("utf-8"),
        mode=REMOTE_FILE_MODE,
    )
    retried_outcome, retried_stdout = _run_machine_script(host, script, failure_description)
    if retried_outcome is not _MachineScriptOutcome.APPLIED:
        raise RemoteGatewayError(
            f"Failed to {failure_description} on VPS {host.get_name()}: the machine still reports its encryption "
            "key missing right after it was written back"
        )
    return retried_stdout


def _run_machine_script(
    host: OuterHostInterface, script: str, failure_description: str
) -> tuple[_MachineScriptOutcome, str]:
    """Run a machine script once and read the outcome off its last line, with its whole output.

    Raises:
        RemoteGatewayError: when the script fails, or finishes without naming
            an outcome (which means it did not run to either of its ends).
    """
    result = host.execute_idempotent_command(script, timeout_seconds=REMOTE_LATCHKEY_TIMEOUT_SECONDS)
    if not result.success:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RemoteGatewayError(
            "Failed to {} on VPS {}: {}".format(
                failure_description,
                host.get_name(),
                summarize_latchkey_failure(detail, "the command reported no reason"),
            )
        )
    stdout_lines = result.stdout.strip().splitlines()
    last_line = stdout_lines[-1] if stdout_lines else ""
    for outcome in _MachineScriptOutcome:
        if last_line == _outcome_marker(outcome):
            return outcome, result.stdout
    raise RemoteGatewayError(
        f"Failed to {failure_description} on VPS {host.get_name()}: the command finished without reporting an "
        f"outcome (last output line: {last_line!r})"
    )


def _merge_of(
    machine_latchkey: Latchkey,
    host_id: HostId,
    service_name: str,
    account: str,
    machine_key: SecretStr,
) -> _CredentialMerge:
    """Build the merge a connect carries: the machine's own store, filtered and re-encrypted for it."""
    return _CredentialMerge(
        machine_key_file=TMPFS_SECRETS_DIR / GATEWAY_ENCRYPTION_KEY_FILENAME,
        machine_key_sha256=_sha256_hex(machine_key.get_secret_value()),
        service_name=service_name,
        account=account,
        bundle=_export_credentials(
            machine_latchkey, host_id, service_name, destination_key=machine_key, account=account or None
        ),
        data_format_version=_read_upstream_data_format_stamp(machine_latchkey.latchkey_directory),
    )


@pure
def _outcome_marker(outcome: _MachineScriptOutcome) -> str:
    return f"{_SCRIPT_OUTCOME_PREFIX}{outcome.value}"


@pure
def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_permissions_snapshot(host_id: HostId, permissions_json: str) -> None:
    try:
        load_permissions_from_text(permissions_json)
    except LatchkeyStoreError as e:
        raise RemoteGatewayError(f"Refusing to apply an unreadable permissions snapshot to host {host_id}: {e}") from e


def _desktop_encryption_key(latchkey: Latchkey) -> SecretStr:
    try:
        return load_or_create_encryption_key(latchkey.latchkey_directory)
    except LatchkeyEncryptionKeyPermissionError as e:
        raise RemoteGatewayError(str(e)) from e


def _read_upstream_data_format_stamp(latchkey_directory: Path) -> str:
    """Return the desktop's upstream latchkey ``data-format-version`` stamp.

    The stamp is guaranteed to exist by the time a transfer reads it: the
    ``latchkey auth re-encrypt`` invocation that produces the bundle runs the
    upstream migrations (and stamps) before doing anything else. A missing or
    unreadable stamp therefore indicates a real problem and raises
    :class:`RemoteGatewayError`.
    """
    stamp_path = latchkey_directory / UPSTREAM_DATA_FORMAT_VERSION_FILENAME
    try:
        return stamp_path.read_text()
    except OSError as e:
        raise RemoteGatewayError(f"Failed to read the local latchkey data-format stamp at {stamp_path}: {e}") from e


def _export_credentials(
    latchkey: Latchkey,
    host_id: HostId,
    service_name: str,
    *,
    destination_key: SecretStr | None,
    account: str | None,
) -> bytes:
    """Return a credential store holding only ``service_name`` (one account of it, when named).

    Exported into a scratch directory and read back rather than written where it
    is going: ``auth re-encrypt`` refuses a destination that already holds a
    store, and a caller that deleted the old one first would lose it to a failed
    export.

    Raises:
        RemoteGatewayError: when the export or the read-back fails.
    """
    with tempfile.TemporaryDirectory(prefix="mngr-latchkey-creds-") as tmpdir:
        try:
            latchkey.export_credentials_subset(
                Path(tmpdir), {service_name}, destination_key=destination_key, account=account
            )
        except LatchkeyError as e:
            raise RemoteGatewayError(f"Failed to export filtered latchkey credentials for host {host_id}: {e}") from e
        subset_path = Path(tmpdir) / CREDENTIALS_STORE_FILENAME
        try:
            return subset_path.read_bytes()
        except OSError as e:
            raise RemoteGatewayError(f"Failed to read filtered latchkey credentials at {subset_path}: {e}") from e
