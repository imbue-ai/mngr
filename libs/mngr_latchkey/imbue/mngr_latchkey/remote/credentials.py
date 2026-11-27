"""One machine's credentials: the store it owns, and this computer's view of it.

The machine that uses a credential owns it. Its gateway refreshes its own
tokens, so its ``credentials.json.enc`` moves under us and is the only thing
that can answer "what is connected here?". This computer keeps a machine store
beside it (:mod:`imbue.mngr_latchkey.remote._mirror`) purely as a *scratch pad*:
somewhere a browser sign-in can land before it is handed over, and somewhere the
machine's own answer is written down so the ordinary offline reads
(``latchkey auth list --offline``) have a directory to read. It is refilled from
the machine (:meth:`MachineCredentials.refresh`) whenever the answer is about to
be shown or acted on, never trusted between times.

Because the copy here is only a view, a change made here is not a change until
it reaches the machine. So changes travel as *operations* -- add this service,
clear this account -- applied to the machine and then read back, rather than as
a desired state the machine is made to match. Nothing here infers what someone
meant from a difference between two stores: an account the machine holds that
this computer has not seen belongs to another of the user's computers, and is
adopted rather than deleted. The policy the machine's gateway enforces is read
back the same way and for the same reason -- another computer may have granted
something this one has never seen. So are the desktop egress rules its router
reads, and the custom services its config registers: one computer creates a
connection to an origin on the machine, and the machine is where every other
computer learns of it.

Every exchange is synchronous and costs a single remote command: the caller
opens the machine's outer host, does what it came to do, and lets both go.
Nothing is queued and nothing is applied later, so a change the user is told
happened is a change the machine has taken -- and one that fails is reported
while there is still a caller to report it to, instead of becoming a
notification about a click made minutes ago.

Two exchanges cost two commands. A read that finds the machine's policy in a
format older than this build writes migrates the policy here, once adopted, and
carries it back in a second command (:func:`migrate_permissions_and_push`), so
the machine is never left holding a policy this build has read but cannot edit.
And a push that changes the machine's custom-service set -- a connect of a
custom service, a disconnect of one's last account -- reads the machine's config
first, so the snapshot it installs keeps what another computer registered there
(:meth:`MachineCredentials._config_registering`,
:meth:`MachineCredentials._config_deregistering`).

A push of the policy replaces the machine's whole, so the copy it is taken
from has to be the machine's own with the edit on top. So every push is
conditional. A read and every push are answered with the machine's fingerprint
of the policy it then holds, which is kept in a record beside the copies here
(:func:`can_edit_host_policy_without_reading`), and the next push hands it
back: the machine installs the snapshot only while it still holds that policy,
and the push raises
:class:`~imbue.mngr_latchkey.remote.errors.MachinePolicyChangedError` when it
does not, or when there is no record to push on. The caller then reads the
machine and makes its edit again. What stays the caller's is knowing that the
copies it edits are the ones the record is about: it either reads the machine
first, or asks :func:`can_edit_host_policy_without_reading` and reads when
the answer is no.
"""

import hashlib
import json
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path

from loguru import logger
from pydantic import Field
from pydantic import JsonValue
from pydantic import SecretStr
from pydantic import SkipValidation
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.file_utils import atomic_write
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.core import build_config_with_custom_services
from imbue.mngr_latchkey.core import custom_service_registration_entries
from imbue.mngr_latchkey.custom_services import is_custom_service_name
from imbue.mngr_latchkey.filed_permission_requests import FiledPermissionRequest
from imbue.mngr_latchkey.migrations.interface import PermissionsMigration
from imbue.mngr_latchkey.migrations.interface import PermissionsMigrationContext
from imbue.mngr_latchkey.migrations.interface import PermissionsMigrationError
from imbue.mngr_latchkey.migrations.runner import PERMISSIONS_MIGRATIONS
from imbue.mngr_latchkey.migrations.runner import migrate_permissions
from imbue.mngr_latchkey.primitives import MachinePolicyFingerprint
from imbue.mngr_latchkey.primitives import PermissionsFormatVersion
from imbue.mngr_latchkey.remote._machine import forget_permission_request
from imbue.mngr_latchkey.remote._machine import read_filed_permission_requests

# Re-exported (the redundant alias marks them as such): the read side of the
# machine store that outside consumers -- notably the Imbue Studio desktop app -- are
# meant to reach through this module rather than through the private mirror.
from imbue.mngr_latchkey.remote._mirror import is_machine_recorded_as_newer as is_machine_recorded_as_newer
from imbue.mngr_latchkey.remote._mirror import latchkey_for_machine as latchkey_for_machine
from imbue.mngr_latchkey.remote._mirror import machine_store_dir
from imbue.mngr_latchkey.remote._mirror import materialize_machine_store
from imbue.mngr_latchkey.remote._mirror import stored_machine_encryption_key as stored_machine_encryption_key

# Re-exported (the redundant alias marks it as such): what :meth:`MachineCredentials.refresh` answers with.
from imbue.mngr_latchkey.remote._transfer import FetchedMachineState as FetchedMachineState
from imbue.mngr_latchkey.remote._transfer import adopt_machine_credentials
from imbue.mngr_latchkey.remote._transfer import adopt_machine_custom_services
from imbue.mngr_latchkey.remote._transfer import adopt_machine_desktop_egress_rules
from imbue.mngr_latchkey.remote._transfer import adopt_machine_permissions
from imbue.mngr_latchkey.remote._transfer import adopt_machine_permissions_format_version
from imbue.mngr_latchkey.remote._transfer import clear_remote_credentials
from imbue.mngr_latchkey.remote._transfer import custom_service_registrations_of_config
from imbue.mngr_latchkey.remote._transfer import fetch_machine_config
from imbue.mngr_latchkey.remote._transfer import fetch_machine_state
from imbue.mngr_latchkey.remote._transfer import is_policy_provisioned
from imbue.mngr_latchkey.remote._transfer import push_credentials
from imbue.mngr_latchkey.remote._transfer import push_credentials_with_permissions
from imbue.mngr_latchkey.remote._transfer import push_permissions_and_desktop_egress_rules
from imbue.mngr_latchkey.remote._transfer import push_permissions_snapshot
from imbue.mngr_latchkey.remote.errors import MachinePolicyChangedError
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import desktop_egress_rules_path_for_host
from imbue.mngr_latchkey.store import machine_policy_record_path
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir
from imbue.mngr_latchkey.store import read_permissions_format_version


class MachineCredentialsError(RemoteGatewayError):
    """Raised when a credential change could not be carried to the machine."""


class SeededPolicy(FrozenModel):
    """What a read handed a machine that held no provisioned policy: this computer's copies, as pushed."""

    permissions_json: str = Field(description="The policy the machine now enforces: this computer's copy of it.")
    permissions_format_version: PermissionsFormatVersion = Field(description="The format that copy is stamped with.")
    desktop_egress_rules_json: str | None = Field(
        description="The desktop egress rules pushed with it, or ``None`` when this computer had no copy to push."
    )
    policy_fingerprint: MachinePolicyFingerprint | None = Field(
        description="The machine's fingerprint of what it holds now, or ``None`` when its package gives none."
    )


class _MachinePolicyRecord(FrozenModel):
    """What this computer last learned of the policy a host's machine holds, as kept beside its copies of it."""

    fingerprint: MachinePolicyFingerprint | None = Field(
        description=(
            "The machine's fingerprint of that policy, which the next push hands back. ``None`` when the machine's "
            "package gave none: the record then stands for one read, and covers the single push that follows it."
        )
    )
    copies_sha256: str = Field(
        description="A digest of this computer's copies of the policy as they stood when the machine held it."
    )


class MachineCredentials(FrozenModel):
    """The credentials of one machine -- and the policy over them -- addressed through the computer managing it.

    Built for the duration of one exchange with the machine: the caller opens
    the outer host, does what it came to do, and lets both go. Every method
    costs a single remote command, in either direction.
    """

    model_config = {"arbitrary_types_allowed": True}

    host: SkipValidation[OuterHostInterface] = Field(description="The machine's outer host, already open.")
    latchkey: Latchkey = Field(description="This computer's latchkey, which owns the desktop store and its key.")
    host_id: HostId = Field(description="The host whose machine store caches these credentials.")

    def refresh(self, migration_context: PermissionsMigrationContext) -> FetchedMachineState:
        """Read the machine back and reconcile it with this computer, and return what it holds.

        Everything comes back in one command: the credential store the
        machine's gateway is refreshing tokens in, re-encrypted for this
        computer, the policy that gateway is enforcing, the desktop egress
        rules its router reads, and the format version the policy is written
        in. They are reconciled differently, because they are owned
        differently.

        The **credentials are the machine's**. Only it can rotate the tokens it
        holds, so what it says is simply adopted, and the copy here goes back to
        being a scratch pad that matches it.

        The **policy is the machine's too**, for a different reason: the user
        may have several computers, and any of them can push a grant to it. A
        computer that treated its own copy as the truth would revert what
        another one granted, every time this ran. So the machine's answer is
        adopted here as well, and the copy here stays a cache -- unless the
        machine has no provisioned policy (none, or one with no rules; see
        :func:`~imbue.mngr_latchkey.remote._transfer.is_policy_provisioned`),
        in which case the copy here is what seeds it (see
        :meth:`_reconcile_permissions`).

        That is only safe because the copy here is never edited without being
        pushed (see :func:`read_host_permissions`), which leaves nothing local
        for an adopt to lose.

        The **desktop egress rules are the machine's** for the same reason the
        policy is, and are adopted the same way. They are seeded from here only
        alongside a policy seed, when this computer holds a copy of them: a
        policy that allows forwarding needs the rules that route to it. The
        machine's gateway run script creates the file otherwise, and a machine
        without one routes nothing.

        The **format version is the machine's** because it describes the
        machine's policy. Once everything is adopted, a policy in a format older
        than this build writes is migrated here and carried back
        (:func:`migrate_permissions_and_push`), so what is returned -- and what
        the machine then holds -- is in the format this build reads.

        The **custom services are the machine's** for the reason the policy
        is: any of the user's computers may have created a connection to an
        origin on it, or signed out of one and deregistered it. The set its
        config registers is adopted into the machine store here (see
        :func:`~imbue.mngr_latchkey.remote._transfer.adopt_machine_custom_services`).

        The **fingerprint** is the machine's, of the policy it holds once all
        of that is done -- after the seed or the migration, when either ran --
        which is the policy the copies here now are. It is recorded beside
        them, for the next push to hand back (see :meth:`set_permissions`).

        Raises:
            PermissionsMigrationError: when the machine's policy is in a format
                newer than this build knows, or cannot be migrated to the one it
                does.
            RemoteGatewayError: when the machine cannot be read, or does not
                take the migrated policy back.
            LatchkeyStoreError: when a copy here cannot be written, or the
                migrated copy here cannot be read back.
        """
        materialize_machine_store(
            self.latchkey.latchkey_directory, plugin_data_dir(self.latchkey.latchkey_directory), self.host_id
        )
        fetched = fetch_machine_state(self.host, self.latchkey, self.host_id)
        adopt_machine_credentials(self.latchkey, self.host_id, fetched)
        adopt_machine_custom_services(self.latchkey, self.host_id, fetched.custom_service_registrations)
        seeded = self._reconcile_permissions(
            fetched.permissions_json, fetched.permissions_format_version, fetched.policy_fingerprint
        )
        if seeded is None or seeded.desktop_egress_rules_json is None:
            adopt_machine_desktop_egress_rules(
                self.latchkey.latchkey_directory, self.host_id, fetched.desktop_egress_rules_json
            )
        if seeded is not None:
            fetched = fetched.model_copy_update(
                to_update(fetched.field_ref().permissions_json, seeded.permissions_json),
                to_update(fetched.field_ref().permissions_format_version, seeded.permissions_format_version),
                to_update(
                    fetched.field_ref().desktop_egress_rules_json,
                    fetched.desktop_egress_rules_json
                    if seeded.desktop_egress_rules_json is None
                    else seeded.desktop_egress_rules_json,
                ),
                to_update(fetched.field_ref().policy_fingerprint, seeded.policy_fingerprint),
            )
        migrated = migrate_permissions_and_push(
            self.host, self.latchkey, self.host_id, migration_context, fetched.policy_fingerprint
        )
        refreshed = (
            fetched
            if migrated is None
            else fetched.model_copy_update(
                to_update(fetched.field_ref().permissions_json, migrated.permissions_json),
                to_update(fetched.field_ref().permissions_format_version, migrated.permissions_format_version),
                to_update(fetched.field_ref().policy_fingerprint, migrated.policy_fingerprint),
            )
        )
        _write_machine_policy_record(
            plugin_data_dir(self.latchkey.latchkey_directory), self.host_id, refreshed.policy_fingerprint
        )
        return refreshed

    def _reconcile_permissions(
        self,
        machine_permissions_json: str | None,
        machine_permissions_format_version: PermissionsFormatVersion,
        # The machine's fingerprint of that policy, which a seed hands back; ``None`` when its package gave none.
        machine_policy_fingerprint: MachinePolicyFingerprint | None,
        # what the machine was seeded with from here, or ``None`` when its own policy was adopted (or it has
        # none and neither does this computer)
    ) -> SeededPolicy | None:
        """Settle the one policy the machine and this computer should both hold (see :meth:`refresh`).

        The machine wins whenever it has a provisioned policy, because another
        of the user's computers may have granted something this one has never
        seen; the format version it stamps that policy with comes along, since
        it describes it. The only write toward the machine is the seed: a
        machine with no policy at all permits everything, and one holding only
        the rule-less default a provisioning pass handed it denies everything
        -- including the agents registered here -- so either is handed this
        computer's copy (when there is one with rules in it) rather than
        adopted over it. The seed carries this computer's copy of the desktop
        egress rules too, when it has one, so the machine never holds a policy
        that allows forwarding without the rules that route to it.

        Raises:
            RemoteGatewayError: when either policy cannot be read, or the
                machine does not take the seed.
            LatchkeyStoreError: when a copy here cannot be read or written.
        """
        if machine_permissions_json is not None and is_policy_provisioned(self.host_id, machine_permissions_json):
            adopt_machine_permissions(self.latchkey.latchkey_directory, self.host_id, machine_permissions_json)
            adopt_machine_permissions_format_version(
                self.latchkey.latchkey_directory, self.host_id, machine_permissions_format_version
            )
            return None
        data_dir = plugin_data_dir(self.latchkey.latchkey_directory)
        local_permissions_json = read_host_permissions(data_dir, self.host_id)
        if local_permissions_json is None or not is_policy_provisioned(self.host_id, local_permissions_json):
            return None
        local_desktop_egress_rules_json = read_host_desktop_egress_rules(data_dir, self.host_id)
        logger.info(
            "The machine of host {} holds no provisioned permissions; seeding it with this computer's copy",
            self.host_id,
        )
        permissions_format_version = self._permissions_format_version()
        policy_fingerprint = (
            push_permissions_snapshot(
                self.host, self.host_id, local_permissions_json, permissions_format_version, machine_policy_fingerprint
            )
            if local_desktop_egress_rules_json is None
            else push_permissions_and_desktop_egress_rules(
                self.host,
                self.host_id,
                local_permissions_json,
                local_desktop_egress_rules_json,
                permissions_format_version,
                machine_policy_fingerprint,
            )
        )
        return SeededPolicy(
            permissions_json=local_permissions_json,
            permissions_format_version=permissions_format_version,
            desktop_egress_rules_json=local_desktop_egress_rules_json,
            policy_fingerprint=policy_fingerprint,
        )

    def connect_service(self, service_name: str, account: str) -> None:
        """Carry an account of a service just connected here onto the machine.

        The sign-in itself happens on this computer -- there is no browser on a
        VPS, and the session and key that make it a consent click rather than a
        full login are here -- so the credential lands in the machine store
        first and is handed over afterwards. It is merged into what the machine
        already holds, scoped to the one account that was connected, so both the
        machine's other services and the same service's other accounts -- with
        whatever the machine refreshed in them meanwhile -- survive it gaining a
        neighbour.

        An empty ``account`` hands over every account the machine store holds
        for the service (see :func:`~imbue.mngr_latchkey.remote._transfer.push_credentials`).
        """
        push_credentials(
            self.host,
            self._machine_latchkey(),
            self.host_id,
            service_name,
            account,
            self._machine_key(),
            config_json=self._config_registering(service_name),
        )

    def connect_service_with_permissions(self, service_name: str, account: str, permissions_json: str) -> None:
        """Carry a permission grant -- one account, and the policy that grants it -- to the machine.

        Both halves of what the agent may now do land under one ``set -e`` on
        the machine, credential first (see
        :func:`~imbue.mngr_latchkey.remote._transfer.push_credentials_with_permissions`),
        so a grant costs one round trip and neither half can be reported done
        without the other. The policy travels on the condition
        :meth:`set_permissions` describes, and a machine that has moved on
        takes neither half.
        """
        self._push_grant(service_name, account, permissions_json, desktop_egress_rules_json=None)

    def connect_service_with_permissions_and_desktop_egress_rules(
        self, service_name: str, account: str, permissions_json: str, desktop_egress_rules_json: str
    ) -> None:
        """Carry a permission grant that also changes a route: the account, the policy and the desktop egress rules.

        What :meth:`connect_service_with_permissions` and
        :meth:`set_permissions_and_desktop_egress_rules` would do in two round
        trips, in one, under their contracts: all three land under one
        ``set -e``, and the two snapshots are of copies the caller has already
        edited.
        """
        self._push_grant(service_name, account, permissions_json, desktop_egress_rules_json=desktop_egress_rules_json)

    def _push_grant(
        self, service_name: str, account: str, permissions_json: str, desktop_egress_rules_json: str | None
    ) -> None:
        machine_latchkey = self._machine_latchkey()
        machine_key = self._machine_key()
        permissions_format_version = self._permissions_format_version()
        config_json = self._config_registering(service_name)
        self._push_policy(
            lambda expected_policy_fingerprint: push_credentials_with_permissions(
                self.host,
                machine_latchkey,
                self.host_id,
                service_name,
                account,
                machine_key,
                permissions_json,
                permissions_format_version,
                desktop_egress_rules_json,
                expected_policy_fingerprint,
                config_json=config_json,
            )
        )

    def set_permissions(self, permissions_json: str) -> None:
        """Make ``permissions_json`` the policy the machine's gateway enforces, if it was edited from the one it holds.

        The one operation that needs no credential material, and so no key:
        a policy is not encrypted (see
        :func:`~imbue.mngr_latchkey.remote._transfer.push_permissions_snapshot`).
        The snapshot is of this computer's copy, so it travels with that
        copy's format stamp.

        It travels on a condition: the fingerprint the machine last gave of
        its policy, as recorded here, is handed back, and a machine holding
        any other policy by now installs nothing. A machine whose package
        gives no fingerprint cannot be asked that, so a push to one is made
        only straight after a read of it, once. Either way a push with no
        record to go on is not sent at all.

        Raises:
            MachinePolicyChangedError: when the machine no longer holds the
                policy the record names, or this computer has no record to
                push on. The machine's policy is left as it was.
        """
        permissions_format_version = self._permissions_format_version()
        self._push_policy(
            lambda expected_policy_fingerprint: push_permissions_snapshot(
                self.host, self.host_id, permissions_json, permissions_format_version, expected_policy_fingerprint
            )
        )

    def set_permissions_and_desktop_egress_rules(self, permissions_json: str, desktop_egress_rules_json: str) -> None:
        """Make both snapshots what the machine holds: the policy its gateway enforces and the rules its router reads.

        A push under the contract :meth:`set_permissions` has, its condition
        included: the caller edits this computer's copies first and hands over
        snapshots of them (:func:`read_host_permissions`,
        :func:`read_host_desktop_egress_rules`), and nothing here writes those
        copies. Both land in one remote command, with the copies' format stamp,
        and neither needs a key (see
        :func:`~imbue.mngr_latchkey.remote._transfer.push_permissions_and_desktop_egress_rules`).

        Raises:
            MachinePolicyChangedError: as :meth:`set_permissions`. The
                machine's policy and rules are left as they were.
        """
        permissions_format_version = self._permissions_format_version()
        self._push_policy(
            lambda expected_policy_fingerprint: push_permissions_and_desktop_egress_rules(
                self.host,
                self.host_id,
                permissions_json,
                desktop_egress_rules_json,
                permissions_format_version,
                expected_policy_fingerprint,
            )
        )

    def _push_policy(
        self,
        # Sends the push with the fingerprint it is given as its condition (``None`` for none), and returns the
        # fingerprint the machine answers with.
        push: Callable[[MachinePolicyFingerprint | None], MachinePolicyFingerprint | None],
    ) -> None:
        """Send a push of the policy on the condition the record allows, and leave the record saying what the machine then holds.

        Raises:
            MachinePolicyChangedError: when there is no record to push on, or
                the machine holds another policy than the record names.
            RemoteGatewayError: when the machine refuses or fails the push.
            LatchkeyStoreError: when the record cannot be read or written.
        """
        data_dir = plugin_data_dir(self.latchkey.latchkey_directory)
        record = _read_machine_policy_record(data_dir, self.host_id)
        if record is None:
            raise MachinePolicyChangedError(
                self.host_id, "this computer has not read the machine since it last knew what policy it holds"
            )
        try:
            fingerprint = push(record.fingerprint)
        except MachinePolicyChangedError:
            _forget_machine_policy_record(data_dir, self.host_id)
            raise
        except RemoteGatewayError:
            # Without a fingerprint the machine checked nothing, and may or may not have taken the push.
            if record.fingerprint is None:
                _forget_machine_policy_record(data_dir, self.host_id)
            raise
        if fingerprint is None:
            _forget_machine_policy_record(data_dir, self.host_id)
        else:
            _write_machine_policy_record(data_dir, self.host_id, fingerprint)

    def disconnect_account(self, service_name: str, account: str, is_service_deregistered: bool) -> None:
        """Clear one account from the machine's own store, and deregister its service when asked to.

        Clearing the copy here *instead* would leave the credential on the
        machine and the machine's agents still able to use it -- which is the
        opposite of what signing out means -- so this is what a sign-out
        actually does, and the local clear beside it only keeps the scratch pad
        from showing an account the machine no longer has.

        ``is_service_deregistered`` says the account was a custom service's
        last, so the service goes with it: the machine's config is read, and
        installed again without the service in the same command as the clear
        (see :func:`~imbue.mngr_latchkey.remote._transfer.clear_remote_credentials`).
        The machine store here keeps its own registration of the service: the
        caller deregisters it from the store afterwards, once the grants that
        named it are gone, and stripping those grants needs the store's catalog
        to still name the service.

        Raises:
            MachineCredentialsError: when ``is_service_deregistered`` names a
                service that is not a custom one; only custom services are
                ever deregistered.
            RemoteGatewayError: when the machine refuses or fails the clear,
                or its config cannot be read for the deregistration.
        """
        clear_remote_credentials(
            self.host,
            self.host_id,
            service_name,
            account,
            config_json=self._config_deregistering(service_name) if is_service_deregistered else None,
        )

    def list_filed_permission_requests(self) -> tuple[FiledPermissionRequest, ...]:
        """The permission requests the machine keeps for the user's desktops (see :mod:`imbue.mngr_latchkey.filed_permission_requests`).

        What a desktop syncs its own pending requests against: a request the
        machine keeps that this desktop does not have is one it was offline for,
        and one this desktop has that the machine no longer keeps was answered
        on another of the user's desktops. Needs no key: the records are the
        agents' own requests, not credential material.

        Raises:
            RemoteGatewayError: when the machine cannot be read.
        """
        return read_filed_permission_requests(self.host, self.host_id)

    def forget_permission_request(self, request_id: str) -> None:
        """Drop a permission request the user answered here from the machine, and withdraw it from the other desktops.

        The machine forwards the withdrawal to every desktop connected to it
        through its own gateway, so a desktop showing the request sees it go;
        one that is not connected syncs it away when it next is. Needs no key,
        like every operation on the requests.

        Raises:
            RemoteGatewayError: when the machine does not take it.
        """
        forget_permission_request(self.host, request_id, self.host_id)

    def _config_registering(self, service_name: str) -> str | None:
        """The config a connect of ``service_name`` installs: the machine's, with a custom service registered; else ``None``.

        The custom services a machine can name are its own: any of the user's
        computers may have registered one on it since this computer last
        looked. So the machine's config is read first, here, and the snapshot
        the connect installs is that file with the one registration added --
        the registration the approval wrote to the machine store ahead of the
        sign-in. Once the snapshot is built, the set it carries is adopted into
        the store, so the store says what the machine is about to -- and a
        machine config the snapshot cannot be built on leaves the store as it
        was. A builtin or bundled service needs nothing installed, and nothing
        read; the bundled registrations reach the machine at provisioning.

        Raises:
            MachineCredentialsError: when ``service_name`` is a custom service
                the machine store has no registration for: a gateway cannot
                route a request to a service its config does not name, so a
                credential the machine could never use is refused rather than
                pushed.
            RemoteGatewayError: when the machine's config cannot be read.
            LatchkeyStoreError: when the store's config cannot be rebuilt.
        """
        if not is_custom_service_name(service_name):
            return None
        registration = custom_service_registration_entries(self._machine_store_dir()).get(service_name)
        if registration is None:
            raise MachineCredentialsError(
                f"Cannot connect {service_name} on host {self.host_id}: the machine store has no registration for "
                "it, so the machine's gateway could not use the credentials"
            )
        machine_config = fetch_machine_config(self.host, self.host_id)
        entries = {
            **custom_service_registrations_of_config(machine_config, self.host_id),
            service_name: registration,
        }
        snapshot = self._config_snapshot(machine_config, entries)
        adopt_machine_custom_services(self.latchkey, self.host_id, entries)
        return snapshot

    def _config_deregistering(self, service_name: str) -> str:
        """The config a sign-out of ``service_name``'s last account installs: the machine's, without the service.

        Read from the machine first, like :meth:`_config_registering`, so the
        services other computers registered on it since stay -- and are
        adopted into the machine store here. The store's own registration of
        ``service_name`` is kept through the adoption: the caller still has to
        strip the grants that name the service, against the store's catalog,
        before it deregisters the service from the store (see
        :meth:`disconnect_account`).

        Raises:
            MachineCredentialsError: when ``service_name`` is not a custom
                service: a builtin or bundled one is never deregistered, so
                asking for it is a caller's mistake rather than a no-op.
            RemoteGatewayError: when the machine's config cannot be read.
            LatchkeyStoreError: when the store's config cannot be rebuilt.
        """
        if not is_custom_service_name(service_name):
            raise MachineCredentialsError(
                f"Cannot deregister {service_name} on host {self.host_id}: only a custom service is deregistered "
                "with its last account"
            )
        machine_config = fetch_machine_config(self.host, self.host_id)
        entries = custom_service_registrations_of_config(machine_config, self.host_id)
        remaining = {name: entry for name, entry in entries.items() if name != service_name}
        snapshot = self._config_snapshot(machine_config, remaining)
        own_registration = custom_service_registration_entries(self._machine_store_dir()).get(service_name)
        adopt_machine_custom_services(
            self.latchkey,
            self.host_id,
            remaining if own_registration is None else {**remaining, service_name: own_registration},
        )
        return snapshot

    def _config_snapshot(self, machine_config: str | None, custom_service_entries: dict[str, JsonValue]) -> str:
        """The machine's config with its custom-service set made ``custom_service_entries``.

        Raises:
            RemoteGatewayError: when the machine's config is not a JSON object.
        """
        try:
            return build_config_with_custom_services(machine_config, custom_service_entries)
        except LatchkeyError as e:
            raise RemoteGatewayError(f"Failed to build the latchkey config of host {self.host_id}: {e}") from e

    def _machine_store_dir(self) -> Path:
        return machine_store_dir(plugin_data_dir(self.latchkey.latchkey_directory), self.host_id)

    def _machine_key(self) -> SecretStr:
        return _recorded_machine_key(self.latchkey, self.host_id)

    def _machine_latchkey(self) -> Latchkey:
        return latchkey_for_machine(self.latchkey, plugin_data_dir(self.latchkey.latchkey_directory), self.host_id)

    def _permissions_format_version(self) -> PermissionsFormatVersion:
        """The format this computer's copy of the host's policy is stamped with: what a snapshot of it travels with.

        Raises:
            MachineCredentialsError: when the stamp cannot be read.
        """
        try:
            return read_permissions_format_version(plugin_data_dir(self.latchkey.latchkey_directory), self.host_id)
        except LatchkeyStoreError as e:
            raise MachineCredentialsError(
                f"Could not read the permissions format version of host {self.host_id}: {e}"
            ) from e


class MigratedPermissions(FrozenModel):
    """What a migration left this computer's copy of a host's policy as, and so what its machine was handed."""

    permissions_json: str = Field(description="The host's policy, in the format the migrations end in.")
    permissions_format_version: PermissionsFormatVersion = Field(description="The format it is now stamped with.")
    policy_fingerprint: MachinePolicyFingerprint | None = Field(
        description="The machine's fingerprint of what it holds now, or ``None`` when its package gives none."
    )


def migrate_permissions_and_push(
    host: OuterHostInterface,
    latchkey: Latchkey,
    host_id: HostId,
    migration_context: PermissionsMigrationContext,
    # The machine's fingerprint of the policy just adopted from it, which the push of the migrated one hands
    # back; ``None`` when its package gave none.
    expected_policy_fingerprint: MachinePolicyFingerprint | None,
    migrations: Sequence[PermissionsMigration] = PERMISSIONS_MIGRATIONS,
    # what was migrated and pushed back, or ``None`` when the policy was already in this build's format
) -> MigratedPermissions | None:
    """Bring this computer's copy of ``host_id``'s policy to the format this build reads, and hand it to its machine.

    Run right after a read adopted what the machine holds, so what is migrated
    is the machine's own policy, and the machine is left holding what this
    build reads: the policy and its stamp travel back in one command. A policy
    already in this build's format costs nothing beyond reading the stamp.

    Raises:
        PermissionsMigrationError: when the policy is in a format newer than
            ``migrations`` know, or cannot be brought to the one they end in.
        LatchkeyStoreError: when the migrated copy cannot be read back.
        MachinePolicyChangedError: when the machine no longer holds the
            policy that was adopted from it.
        RemoteGatewayError: when the machine does not take it.
    """
    data_dir = plugin_data_dir(latchkey.latchkey_directory)
    if not migrate_permissions(data_dir, host_id, migration_context, migrations):
        return None
    permissions_json = read_host_permissions(data_dir, host_id)
    if permissions_json is None:
        raise PermissionsMigrationError(
            f"Migrated the permissions of host {host_id}, but there is no policy left to push"
        )
    permissions_format_version = read_permissions_format_version(data_dir, host_id)
    logger.info(
        "Migrated the permissions of host {} to format version {}; handing them back to the machine",
        host_id,
        permissions_format_version,
    )
    return MigratedPermissions(
        permissions_json=permissions_json,
        permissions_format_version=permissions_format_version,
        policy_fingerprint=push_permissions_snapshot(
            host, host_id, permissions_json, permissions_format_version, expected_policy_fingerprint
        ),
    )


def _recorded_machine_key(latchkey: Latchkey, host_id: HostId) -> SecretStr:
    """The key the machine keeps its own store under, as recorded here.

    The record is written by the provisioning pass -- which mints a key for a
    fresh machine and adopts the key a machine another of the user's computers
    provisioned is already running under -- so its absence means no
    provisioning pass from this computer has reached the machine yet, and there
    is nothing it can encrypt for it.
    """
    data_dir = plugin_data_dir(latchkey.latchkey_directory)
    try:
        materialize_machine_store(latchkey.latchkey_directory, data_dir, host_id)
        machine_key = stored_machine_encryption_key(data_dir, host_id)
    except LatchkeyStoreError as e:
        raise MachineCredentialsError(f"Could not open the machine store of host {host_id}: {e}") from e
    if machine_key is None:
        raise MachineCredentialsError(
            f"Host {host_id} has no encryption key yet; its gateway has never been provisioned from here."
        )
    return machine_key


def read_host_permissions(data_dir: Path, host_id: HostId) -> str | None:
    """Return this computer's copy of the policy for ``host_id``, or ``None`` when it has none yet.

    What a push to the machine carries: a whole snapshot, taken at the moment
    the caller asks for one, so what lands is the policy as it stood after the
    edit that prompted it. Snapshots rather than deltas, so applying the newest
    one is always right regardless of what came before it.

    Every writer of this file must push what it wrote, in the same operation,
    and the copy it wrote on must be the one the record beside it is about:
    either it read the machine (:meth:`MachineCredentials.refresh`) in that
    same operation before writing, or :func:`can_edit_host_policy_without_reading`
    said the copy was still what the machine last answered for. The machine
    owns the policy, so an edit that is not pushed is not merely late -- it is
    discarded by the next refresh, silently -- and an edit made on a copy the
    record is not about would ride to the machine on a fingerprint that never
    described it, carrying along whatever else that copy held. A writer that
    cannot read or push must at least say so.

    Raises:
        LatchkeyStoreError: when the file exists but cannot be read.
    """
    permissions_path = permissions_path_for_host(data_dir, host_id)
    if not permissions_path.is_file():
        return None
    try:
        return permissions_path.read_text()
    except OSError as e:
        raise LatchkeyStoreError(f"Failed to read the permissions of host {host_id} at {permissions_path}: {e}") from e


def read_host_desktop_egress_rules(data_dir: Path, host_id: HostId) -> str | None:
    """Return this computer's copy of the desktop egress rules for ``host_id``, or ``None`` when it has none.

    What a push to the machine carries, as a whole snapshot, under the rule
    :func:`read_host_permissions` states: every writer of this file must push
    what it wrote in the same operation, because the next refresh adopts the
    machine's rules over it.

    Raises:
        LatchkeyStoreError: when the file exists but cannot be read.
    """
    rules_path = desktop_egress_rules_path_for_host(data_dir, host_id)
    if not rules_path.is_file():
        return None
    try:
        return rules_path.read_text()
    except OSError as e:
        raise LatchkeyStoreError(
            f"Failed to read the desktop egress rules of host {host_id} at {rules_path}: {e}"
        ) from e


def can_edit_host_policy_without_reading(data_dir: Path, host_id: HostId) -> bool:
    """Whether an edit of this computer's copies of ``host_id``'s policy can be pushed without reading the machine first.

    True while the machine's fingerprint of its policy is on record here and
    the copies are still as they stood when it gave it: the push of an edit
    made now hands that fingerprint back, and the machine itself refuses it
    if it has moved on. False when there is nothing on record, when the
    machine's package gives no fingerprint to check, and when the copies have
    been changed since -- by an edit whose push failed, or by anything else --
    because an edit made on top of that would be pushed along with it.

    Raises:
        LatchkeyStoreError: when the record or a copy exists but cannot be read.
    """
    record = _read_machine_policy_record(data_dir, host_id)
    return (
        record is not None
        and record.fingerprint is not None
        and record.copies_sha256 == _digest_host_policy_copies(data_dir, host_id)
    )


def _digest_host_policy_copies(data_dir: Path, host_id: HostId) -> str:
    """A digest of this computer's copies of everything that makes up ``host_id``'s policy, as they stand.

    Raises:
        LatchkeyStoreError: when a copy exists but cannot be read.
    """
    copies = (
        read_host_permissions(data_dir, host_id),
        read_host_desktop_egress_rules(data_dir, host_id),
        int(read_permissions_format_version(data_dir, host_id)),
    )
    return hashlib.sha256(json.dumps(copies).encode("utf-8")).hexdigest()


def _read_machine_policy_record(data_dir: Path, host_id: HostId) -> _MachinePolicyRecord | None:
    """What is on record of the policy ``host_id``'s machine holds, or ``None`` when nothing usable is.

    Raises:
        LatchkeyStoreError: when the record exists but cannot be read.
    """
    record_path = machine_policy_record_path(data_dir, host_id)
    if not record_path.is_file():
        return None
    try:
        record_json = record_path.read_text()
    except OSError as e:
        raise LatchkeyStoreError(f"Failed to read the machine policy record of host {host_id}: {e}") from e
    try:
        return _MachinePolicyRecord.model_validate_json(record_json)
    except ValidationError as e:
        # Having no record only costs the next push a read of the machine.
        logger.warning("Ignored the machine policy record of host {} at {}: {}", host_id, record_path, e)
        return None


def _write_machine_policy_record(
    data_dir: Path,
    host_id: HostId,
    # ``None`` when the machine's package gave none.
    fingerprint: MachinePolicyFingerprint | None,
) -> None:
    """Record that ``host_id``'s machine holds this computer's copies as they stand, under ``fingerprint``.

    Raises:
        LatchkeyStoreError: when a copy cannot be read or the record cannot be written.
    """
    record = _MachinePolicyRecord(fingerprint=fingerprint, copies_sha256=_digest_host_policy_copies(data_dir, host_id))
    record_path = machine_policy_record_path(data_dir, host_id)
    try:
        atomic_write(record_path, record.model_dump_json())
    except OSError as e:
        raise LatchkeyStoreError(f"Failed to write the machine policy record of host {host_id}: {e}") from e


def _forget_machine_policy_record(data_dir: Path, host_id: HostId) -> None:
    """Drop what is on record of the policy ``host_id``'s machine holds, so the next push waits for a read.

    Raises:
        LatchkeyStoreError: when the record cannot be removed.
    """
    try:
        machine_policy_record_path(data_dir, host_id).unlink(missing_ok=True)
    except OSError as e:
        raise LatchkeyStoreError(f"Failed to remove the machine policy record of host {host_id}: {e}") from e


def has_machine_of_its_own(data_dir: Path, host_id: HostId) -> bool:
    """Whether ``host_id``'s credentials and policy live somewhere other than this computer.

    ``False`` for a local host -- its agents read the store and the permissions
    file this computer keeps, so a change made here is already where it counts
    -- and for a remote host no provisioning pass from this computer has reached
    yet, which is handed both halves when one first seeds it.

    ``True`` as well for a host whose machine is recorded as running software
    of a newer generation than this build's
    (:func:`is_machine_recorded_as_newer`), whether or not a key is recorded
    for it: its credentials and policy live on that machine, which this build
    cannot manage, and are never this computer's.

    Raises:
        LatchkeyStoreError: when the machine store cannot be read, which leaves
            it unknown whether there is anything to carry.
    """
    return (
        is_machine_recorded_as_newer(data_dir, host_id) or stored_machine_encryption_key(data_dir, host_id) is not None
    )
