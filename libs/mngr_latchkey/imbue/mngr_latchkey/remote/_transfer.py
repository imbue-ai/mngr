"""Moving credential material between this computer and a machine's own store.

The machine keeps its store under its own key, and the desktop keeps every
machine store under the desktop's, so every transfer re-encrypts at the
boundary -- always here, with the copy of the machine's key this computer
records anyway: a store read back from the machine arrives as the machine holds
it and is re-encrypted for the desktop here, and a bundle heading to the
machine is re-encrypted with the machine's key here. The desktop's own key
never leaves this computer.

Everything this computer *pushes* to a machine -- a connect (with the config
snapshot the credential needs the gateway to have), a disconnect, a permissions
snapshot (alone, or with a desktop egress rules snapshot), or a permission
grant (a connect and a snapshot together) -- is one
:class:`~imbue.mngr_latchkey.remote._machine.RemoteStateUpdate`
applied by the machine's ``mngr-latchkey apply-state`` in a single remote
command, and reading a machine back (:func:`fetch_machine_state`) is one
``mngr-latchkey read-state``. So an exchange costs one round trip, whatever it
is, and opening a host's Permissions tab costs one rather than one per
file.

A push that brings credential material carries the key this computer recorded
for the machine, and the machine refuses it if it is running under another one
(re-keyed from another computer), rather than being handed a store its gateway
could not read. A machine that lost its key to a reboot is handed it back in
the same document, so that costs nothing extra.
"""

import tempfile
from pathlib import Path

from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import log_span
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr_latchkey.core import CREDENTIALS_STORE_FILENAME
from imbue.mngr_latchkey.core import EncryptedCredentialStore
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.core import UPSTREAM_DATA_FORMAT_VERSION_FILENAME
from imbue.mngr_latchkey.desktop_egress import DesktopEgressError
from imbue.mngr_latchkey.desktop_egress import parse_desktop_egress_rules
from imbue.mngr_latchkey.encryption_key import LatchkeyEncryptionKeyPermissionError
from imbue.mngr_latchkey.encryption_key import load_or_create_encryption_key
from imbue.mngr_latchkey.remote._machine import RemoteCredentialClear
from imbue.mngr_latchkey.remote._machine import RemoteCredentialMerge
from imbue.mngr_latchkey.remote._machine import RemoteStateRequest
from imbue.mngr_latchkey.remote._machine import RemoteStateUpdate
from imbue.mngr_latchkey.remote._machine import apply_remote_state
from imbue.mngr_latchkey.remote._machine import read_remote_state
from imbue.mngr_latchkey.remote._mirror import clear_machine_credentials
from imbue.mngr_latchkey.remote._mirror import machine_store_dir
from imbue.mngr_latchkey.remote._mirror import write_machine_credentials
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import desktop_egress_rules_path_for_host
from imbue.mngr_latchkey.store import load_permissions_from_text
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir


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
    desktop_egress_rules_json: str | None = Field(
        description="The desktop egress rules the machine's router reads, or ``None`` when it has no rules file."
    )


def fetch_machine_state(
    host: OuterHostInterface,
    latchkey: Latchkey,
    host_id: HostId,
    machine_key: SecretStr,
) -> FetchedMachineState:
    """Read what a machine holds -- its credentials, its policy and its desktop egress rules -- in one round trip.

    Deliberately does not write anything here: the caller decides what to adopt
    (see :func:`adopt_machine_credentials`), and a read that lands over this
    computer's copies before the caller has looked at them would erase the
    comparison it came to make.

    A machine with no store yet (nothing has ever been connected for it), with
    no policy yet (nothing has provisioned it), or with no desktop egress rules
    file (its gateway has not run since a build that creates one) reports those
    as ``None`` rather than as an error.

    The store arrives as the machine holds it and is re-encrypted for the
    desktop here, under the key the machine reports running under -- which a
    machine that lost its own to a reboot has just been handed back.

    Raises:
        RemoteGatewayError: when the machine cannot produce its store, or what
            it produced cannot be read.
    """
    request = RemoteStateRequest(is_credential_store_included=True, fallback_encryption_key=machine_key)
    with log_span("Reading the latchkey state of host {} from VPS {}", host_id, host.get_name()):
        state = read_remote_state(host, request, failure_description=f"read the latchkey state of host {host_id}")
    store = (
        _reencrypt_machine_store_for_desktop(latchkey, host_id, state.credential_store, state.encryption_key)
        if state.credential_store is not None
        else None
    )
    return FetchedMachineState(
        credentials=store.content if store is not None else None,
        data_format_version=store.data_format_version if store is not None else "",
        permissions_json=state.permissions_json,
        desktop_egress_rules_json=state.desktop_egress_rules_json,
    )


def _reencrypt_machine_store_for_desktop(
    latchkey: Latchkey,
    host_id: HostId,
    machine_store: EncryptedCredentialStore,
    machine_running_key: SecretStr | None,
) -> EncryptedCredentialStore | None:
    """Re-encrypt the store a machine answered with under the desktop's key; ``None`` when it holds nothing.

    Raises:
        RemoteGatewayError: when the machine reported no key to read its store
            under, or the store cannot be re-encrypted.
    """
    if machine_running_key is None:
        raise RemoteGatewayError(
            f"Failed to read the credentials of host {host_id}: the machine answered with its store but not with "
            "the key it is written under"
        )
    desktop_key = _desktop_encryption_key(latchkey)
    with log_span("Re-encrypting the credentials of host {} for this computer", host_id):
        try:
            return latchkey.reencrypt_foreign_store(
                machine_store, store_key=machine_running_key, destination_key=desktop_key
            )
        except LatchkeyError as e:
            raise RemoteGatewayError(f"Failed to re-encrypt the credentials of host {host_id} here: {e}") from e


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


def adopt_machine_desktop_egress_rules(
    latchkey_directory: Path, host_id: HostId, desktop_egress_rules_json: str | None
) -> None:
    """Take the machine's desktop egress rules as this computer's copy of them.

    The machine always wins, for the reason its policy does (see
    :func:`adopt_machine_permissions`): another of the user's computers may
    have changed the rules. ``None`` means the machine has no rules file, and
    removes the copy here, so this computer never shows rules the machine does
    not have. Text is validated before it is stored and written only when it
    actually differs, so unchanged rules cost nothing.

    Raises:
        RemoteGatewayError: when the rules are not ones this build
            understands, or the copy cannot be stored or removed.
    """
    local_path = desktop_egress_rules_path_for_host(plugin_data_dir(latchkey_directory), host_id)
    if desktop_egress_rules_json is None:
        try:
            local_path.unlink(missing_ok=True)
        except OSError as e:
            raise RemoteGatewayError(
                f"Failed to remove the desktop egress rules of host {host_id} at {local_path}: {e}"
            ) from e
        return
    try:
        parse_desktop_egress_rules(desktop_egress_rules_json)
    except DesktopEgressError as e:
        raise RemoteGatewayError(
            f"The machine of host {host_id} holds a desktop egress rules file this build cannot read: {e}"
        ) from e
    try:
        if not local_path.is_file() or local_path.read_text() != desktop_egress_rules_json:
            atomic_write(local_path, desktop_egress_rules_json)
    except OSError as e:
        raise RemoteGatewayError(
            f"Failed to store the desktop egress rules of host {host_id} at {local_path}: {e}"
        ) from e


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

    ``config_json``, when given, is this package's half of the machine's
    ``config.json`` as a whole snapshot, installed ahead of the credential: a
    gateway with no entry for a service cannot route a request to it, so a
    credential for a service the machine's config does not name would be one
    it could never use.

    Raises:
        RemoteGatewayError: when the bundle cannot be built, or the machine
            refuses or fails the update.
    """
    with log_span("Adding {} to the credentials of host {} on VPS {}", service_name, host_id, host.get_name()):
        apply_remote_state(
            host,
            RemoteStateUpdate(
                encryption_key=machine_key,
                config_json=config_json,
                credential_merge=_merge_of(machine_latchkey, host_id, service_name, account, machine_key),
            ),
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

    What a permission grant asks for: both halves land under one ``set -e``,
    credential first, so the policy is never enforceable before the credential
    it rides on is there, and neither half is reported done without the other.
    ``config_json`` lands ahead of both (see :func:`push_credentials`).

    Raises:
        RemoteGatewayError: when the snapshot is not a policy this build can
            read, the bundle cannot be built, or the machine refuses or fails
            the update.
    """
    _validate_permissions_snapshot(host_id, permissions_json)
    with log_span(
        "Adding {} to the credentials of host {} and applying its permissions on VPS {}",
        service_name,
        host_id,
        host.get_name(),
    ):
        apply_remote_state(
            host,
            RemoteStateUpdate(
                encryption_key=machine_key,
                config_json=config_json,
                credential_merge=_merge_of(machine_latchkey, host_id, service_name, account, machine_key),
                permissions_json=permissions_json,
            ),
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
    gateway is running under -- whichever that is. ``machine_key`` is only what
    a machine that lost its key to a reboot is handed back: refusing a machine
    re-keyed from another computer would leave a credential the user signed out
    of sitting on it, which is the one outcome a sign-out must not have.

    Raises:
        RemoteGatewayError: when the machine refuses or fails the update.
    """
    with log_span("Disconnecting {} of host {} from VPS {}", service_name, host_id, host.get_name()):
        apply_remote_state(
            host,
            RemoteStateUpdate(
                fallback_encryption_key=machine_key,
                credential_clear=RemoteCredentialClear(service_name=service_name, account=account),
            ),
            failure_description=f"disconnect {service_name} of host {host_id}",
        )


def push_permissions_snapshot(host: OuterHostInterface, host_id: HostId, permissions_json: str) -> None:
    """Make ``permissions_json`` the policy ``host_id``'s machine enforces, in one round trip.

    How a permissions edit made on this computer reaches the machine: as a full
    snapshot of the canonical per-host file, pushed the moment the edit is
    made. Validated before it is written -- a snapshot this build cannot parse
    must not become a machine's policy -- and installed atomically, so the
    gateway never reads a half-written file.

    It carries no key: a policy is not encrypted, so it is applied to a
    machine whatever key that machine is running under, and to a machine that
    rebooted and is running under none.

    Raises:
        RemoteGatewayError: when the snapshot is not a policy this build can
            read, or the machine refuses or fails the update.
    """
    _validate_permissions_snapshot(host_id, permissions_json)
    with log_span("Applying a permissions snapshot for host {} to VPS {}", host_id, host.get_name()):
        apply_remote_state(
            host,
            RemoteStateUpdate(permissions_json=permissions_json),
            failure_description=f"apply the permissions of host {host_id}",
        )


def push_permissions_and_desktop_egress_rules(
    host: OuterHostInterface, host_id: HostId, permissions_json: str, desktop_egress_rules_json: str
) -> None:
    """Make both snapshots what ``host_id``'s machine holds -- its policy and its desktop egress rules -- in one round trip.

    How turning desktop egress on or off for a service reaches the machine: the
    policy carries the device-gated rule and the rules file carries the routing,
    and they change together. Both are validated before anything is sent, and
    each is installed atomically. Like :func:`push_permissions_snapshot`, this
    carries no key, because neither file is encrypted.

    Raises:
        RemoteGatewayError: when either snapshot is not one this build can
            read, or the machine refuses or fails the update.
    """
    _validate_permissions_snapshot(host_id, permissions_json)
    _validate_desktop_egress_rules_snapshot(host_id, desktop_egress_rules_json)
    with log_span(
        "Applying a permissions snapshot and desktop egress rules for host {} to VPS {}", host_id, host.get_name()
    ):
        apply_remote_state(
            host,
            RemoteStateUpdate(permissions_json=permissions_json, desktop_egress_rules_json=desktop_egress_rules_json),
            failure_description=f"apply the permissions and desktop egress rules of host {host_id}",
        )


def _merge_of(
    machine_latchkey: Latchkey,
    host_id: HostId,
    service_name: str,
    account: str,
    machine_key: SecretStr,
) -> RemoteCredentialMerge:
    """Build the merge a connect carries: the machine's own store, filtered and re-encrypted for it."""
    return RemoteCredentialMerge(
        service_name=service_name,
        account=account,
        bundle=_export_credentials(
            machine_latchkey, host_id, service_name, destination_key=machine_key, account=account or None
        ),
        data_format_version=_read_upstream_data_format_stamp(machine_latchkey.latchkey_directory),
    )


def _validate_permissions_snapshot(host_id: HostId, permissions_json: str) -> None:
    try:
        load_permissions_from_text(permissions_json)
    except LatchkeyStoreError as e:
        raise RemoteGatewayError(f"Refusing to apply an unreadable permissions snapshot to host {host_id}: {e}") from e


def _validate_desktop_egress_rules_snapshot(host_id: HostId, desktop_egress_rules_json: str) -> None:
    try:
        parse_desktop_egress_rules(desktop_egress_rules_json)
    except DesktopEgressError as e:
        raise RemoteGatewayError(f"Refusing to apply unreadable desktop egress rules to host {host_id}: {e}") from e


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
