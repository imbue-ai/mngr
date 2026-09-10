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
something this one has never seen.

Every exchange is synchronous and costs a single remote command: the caller
opens the machine's outer host, does what it came to do, and lets both go.
Nothing is queued and nothing is applied later, so a change the user is told
happened is a change the machine has taken -- and one that fails is reported
while there is still a caller to report it to, instead of becoming a
notification about a click made minutes ago.
"""

from pathlib import Path

from pydantic import Field
from pydantic import SecretStr
from pydantic import SkipValidation

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import custom_service_registration_entries
from imbue.mngr_latchkey.core import merge_minds_latchkey_config
from imbue.mngr_latchkey.custom_services import is_custom_service_name

# Re-exported (the redundant alias marks it as such): provisioning's own passes
# share one lazily-resolved machine directory across their steps.
from imbue.mngr_latchkey.remote._machine import RemoteLatchkeyDirectory as RemoteLatchkeyDirectory

# Re-exported (the redundant alias marks them as such): the read side of the
# machine store that outside consumers -- notably the Minds desktop app -- are
# meant to reach through this module rather than through the private mirror.
from imbue.mngr_latchkey.remote._mirror import latchkey_for_machine as latchkey_for_machine
from imbue.mngr_latchkey.remote._mirror import materialize_machine_store
from imbue.mngr_latchkey.remote._mirror import stored_machine_encryption_key as stored_machine_encryption_key

# Re-exported (the redundant alias marks it as such): what :meth:`MachineCredentials.refresh` answers with.
from imbue.mngr_latchkey.remote._transfer import FetchedMachineState as FetchedMachineState
from imbue.mngr_latchkey.remote._transfer import adopt_machine_credentials
from imbue.mngr_latchkey.remote._transfer import adopt_machine_permissions
from imbue.mngr_latchkey.remote._transfer import clear_remote_credentials
from imbue.mngr_latchkey.remote._transfer import fetch_machine_state
from imbue.mngr_latchkey.remote._transfer import push_credentials
from imbue.mngr_latchkey.remote._transfer import push_credentials_with_permissions
from imbue.mngr_latchkey.remote._transfer import push_permissions_snapshot
from imbue.mngr_latchkey.remote.errors import RemoteGatewayError
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import plugin_data_dir


class MachineCredentialsError(RemoteGatewayError):
    """Raised when a credential change could not be carried to the machine."""


class MachineCredentials(FrozenModel):
    """The credentials of one machine -- and the policy over them -- addressed through the computer managing it.

    Built for the duration of one exchange with the machine: the caller opens
    the outer host, does what it came to do, and lets both go. Every method
    costs a single remote command, in either direction (a second only when a
    rebooted machine has to be handed its key back).
    """

    model_config = {"arbitrary_types_allowed": True}

    host: SkipValidation[OuterHostInterface] = Field(description="The machine's outer host, already open.")
    latchkey: Latchkey = Field(description="This computer's latchkey, which owns the desktop store and its key.")
    host_id: HostId = Field(description="The host whose machine store caches these credentials.")

    def refresh(self) -> FetchedMachineState:
        """Read the machine back and reconcile it with this computer, and return what it held.

        Both halves come back in one command: the credential store the
        machine's gateway is refreshing tokens in, re-encrypted for this
        computer, and the policy that gateway is enforcing. They are reconciled
        differently, because they are owned differently.

        The **credentials are the machine's**. Only it can rotate the tokens it
        holds, so what it says is simply adopted, and the copy here goes back to
        being a scratch pad that matches it.

        The **policy is the machine's too**, for a different reason: the user
        may have several computers, and any of them can push a grant to it. A
        computer that treated its own copy as the truth would revert what
        another one granted, every time this ran. So the machine's answer is
        adopted here as well, and the copy here stays a cache.

        That is only safe because the copy here is never edited without being
        pushed (see :func:`read_host_permissions`), which leaves nothing local
        for an adopt to lose.
        """
        fetched = fetch_machine_state(self.host, self.latchkey, self.host_id, self._machine_key())
        adopt_machine_credentials(self.latchkey, self.host_id, fetched)
        self._reconcile_permissions(fetched.permissions_json)
        return fetched

    def _reconcile_permissions(self, machine_permissions_json: str | None) -> None:
        """Settle the one policy the machine and this computer should both hold (see :meth:`refresh`).

        The machine wins whenever it has one, because another of the user's
        computers may have granted something this one has never seen. The only
        write toward the machine is the seed: a machine with no policy at all
        permits everything, so it is handed this computer's copy rather than
        left open.
        """
        if machine_permissions_json is not None:
            adopt_machine_permissions(self.latchkey.latchkey_directory, self.host_id, machine_permissions_json)
            return
        local_permissions_json = read_host_permissions(plugin_data_dir(self.latchkey.latchkey_directory), self.host_id)
        if local_permissions_json is not None:
            self.set_permissions(local_permissions_json)

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
            config_json=self._config_for(service_name),
        )

    def connect_service_with_permissions(self, service_name: str, account: str, permissions_json: str) -> None:
        """Carry a permission grant -- one account, and the policy that grants it -- to the machine.

        Both halves of what the agent may now do land under one ``set -e`` on
        the machine, credential first (see
        :func:`~imbue.mngr_latchkey.remote._transfer.push_credentials_with_permissions`),
        so a grant costs one round trip and neither half can be reported done
        without the other.
        """
        push_credentials_with_permissions(
            self.host,
            self._machine_latchkey(),
            self.host_id,
            service_name,
            account,
            self._machine_key(),
            permissions_json,
            config_json=self._config_for(service_name),
        )

    def set_permissions(self, permissions_json: str) -> None:
        """Make ``permissions_json`` the policy the machine's gateway enforces.

        The one operation that needs no credential material, and so no key:
        a policy is not encrypted (see
        :func:`~imbue.mngr_latchkey.remote._transfer.push_permissions_snapshot`).
        """
        push_permissions_snapshot(self.host, self.host_id, permissions_json)

    def disconnect_account(self, service_name: str, account: str) -> None:
        """Clear one account from the machine's own store.

        Clearing the copy here *instead* would leave the credential on the
        machine and the machine's agents still able to use it -- which is the
        opposite of what signing out means -- so this is what a sign-out
        actually does, and the local clear beside it only keeps the scratch pad
        from showing an account the machine no longer has.
        """
        clear_remote_credentials(self.host, self.host_id, service_name, account, self._machine_key())

    def _config_for(self, service_name: str) -> str:
        """This package's half of the machine's ``config.json``, as the connect of ``service_name`` should leave it.

        A machine's config is its own file, seeded from this computer's when the
        machine is provisioned and never read back -- and on a machine nothing
        else writes it: upstream latchkey touches the file only from ``services
        register`` and from browser discovery, neither of which runs on a VPS.
        So it travels the way the policy does, as a whole snapshot taken now and
        installed ahead of the credential, and applying the newest one is always
        right: a service registered here since the machine was provisioned
        appears, and one deregistered here disappears. What travels is the
        projection this package owns -- the hidden built-in services and every
        registered service, bundled and custom -- never this computer's file
        itself, whose browser and keyring configuration belong here.

        Raises:
            MachineCredentialsError: when ``service_name`` is a custom service
                this computer has no registration for: a gateway cannot route a
                request to a service its config does not name, so a credential
                the machine could never use is refused rather than pushed.
        """
        entries = custom_service_registration_entries(self.latchkey.latchkey_directory)
        if is_custom_service_name(service_name) and service_name not in entries:
            raise MachineCredentialsError(
                f"Cannot connect {service_name} on host {self.host_id}: this computer's latchkey config has no "
                "registration for it, so the machine's gateway could not use the credentials"
            )
        return merge_minds_latchkey_config(None, entries)

    def _machine_key(self) -> SecretStr:
        return _recorded_machine_key(self.latchkey, self.host_id)

    def _machine_latchkey(self) -> Latchkey:
        return latchkey_for_machine(self.latchkey, plugin_data_dir(self.latchkey.latchkey_directory), self.host_id)


def _recorded_machine_key(latchkey: Latchkey, host_id: HostId) -> SecretStr:
    """The key the machine keeps its own store under, as recorded here.

    The record is written by the provisioning pass -- which mints a key for a
    fresh machine and adopts the key a machine another of the user's computers
    provisioned is already running under -- so its absence means no
    provisioning pass from this computer has reached the machine yet, and there
    is nothing it can encrypt for it or read from it.
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

    Every writer of this file must push what it wrote, in the same operation.
    The machine owns the policy (see :meth:`MachineCredentials.refresh`), so an
    edit that is not pushed is not merely late -- it is discarded by the next
    refresh, silently. A writer that cannot push must at least say so.

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


def has_machine_of_its_own(data_dir: Path, host_id: HostId) -> bool:
    """Whether ``host_id``'s credentials and policy live somewhere other than this computer.

    ``False`` for a local host -- its agents read the store and the permissions
    file this computer keeps, so a change made here is already where it counts
    -- and for a remote host no provisioning pass from this computer has reached
    yet, which is handed both halves when one first seeds it.

    Raises:
        LatchkeyStoreError: when the machine store cannot be read, which leaves
            it unknown whether there is anything to carry.
    """
    return stored_machine_encryption_key(data_dir, host_id) is not None
