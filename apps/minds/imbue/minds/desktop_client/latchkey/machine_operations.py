"""Carrying a change to the machine that owns it, while the user waits.

A remote workspace's credentials *and* the policy its gateway enforces live on
its own machine. Signing in happens here -- there is no browser on a VPS, and
the session that makes it a consent click is this computer's -- so the
credential lands in this computer's copy of the machine's store first, and is
then handed over. Everything else is the same shape: edit here, then carry.

The carry is **synchronous**. Every method blocks its caller until the machine
has taken the change, and raises :class:`MachineOperationError` when it has
not. Nothing is queued, nothing is applied later, and nothing is reported as
done on the strength of a local edit: what the UI shows after the call is what
the machine holds. A failure is therefore ordinary, immediate news -- the
button that was pressed says why it did not work -- rather than a notification
about a click made minutes ago.

Reading is the same in reverse: :meth:`MachineOperator.refresh` re-reads the
machine before anything about it is shown, in one round trip, so the
Permissions tab is the machine's answer rather than this computer's memory of
it. That matters most when the user has more than one computer: a grant made on
their laptop reaches this one only by being read back off the machine both are
talking to.

Which is why the carry is not optional. Every edit to a workspace's policy is
pushed as it is made, and a push that fails is reported -- to the user when a
click is waiting on it, to the log when nothing is. An edit left behind here
unpushed is not merely late: the next refresh adopts the machine's policy over
it and it is gone.

And why a push never goes out on trust. A push carries this computer's copy
whole, so the copy the edit was made on has to be the machine's own. A copy
that lagged the machine -- by a grant made from another of the user's
computers, or by anything this computer has not read since -- pushed as it
stood would erase on the machine whatever it never knew about. So every push
is conditional: it hands back the fingerprint the machine last gave of its
policy, and a machine that has moved on since applies none of it
(:class:`MachinePolicyChangedOperationError`). All of that is kept by
:mod:`imbue.mngr_latchkey.remote.credentials`; what is decided here is only
when the machine is read:

* Most writers read first: refresh, then edit, then push. Two round trips.
* A write that is one edit of the policy (:meth:`MachineOperator.carry_policy_edit`)
  is made on the copy as it stands, for as long as that copy is still what the
  machine last answered for, and is read only when it is not or when the
  machine refuses the push. One round trip, which is what a click in the
  Permissions tab pays: the tab read the machine moments ago.

A local workspace has no machine of its own -- its agents run here, on the
credentials and the permissions file this computer keeps -- so for those every
method is a no-op and the local edit is the whole change.

A workspace whose machine a newer Imbue Studio set up has one this build leaves
alone. Every method fails for it, with the sentence that tells the user to
update (:data:`~imbue.minds.desktop_client.latchkey.machine_access.MACHINE_NEWER_FAILURE`):
at once and without a remote command when the machine is recorded as newer,
and from the machine's own answer when it is not recorded yet.
"""

from collections.abc import Callable
from collections.abc import Generator
from contextlib import contextmanager
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.latchkey.machine_access import MACHINE_EXCHANGE_FAILURES
from imbue.minds.desktop_client.latchkey.machine_access import MACHINE_NEWER_FAILURE
from imbue.minds.desktop_client.latchkey.machine_access import MachineAccess
from imbue.minds.desktop_client.latchkey.machine_access import MachineRecordedAsNewerError
from imbue.minds.desktop_client.latchkey.machine_access import MachineUnreachableError
from imbue.minds.desktop_client.ui_models import ErrorRemedy
from imbue.minds.desktop_client.user_facing_failure import UserFacingFailure
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.devices import DesktopDeviceId
from imbue.mngr_latchkey.migrations.interface import PermissionsFormatNewerError
from imbue.mngr_latchkey.migrations.interface import PermissionsMigrationContext
from imbue.mngr_latchkey.remote.credentials import MachineCredentials
from imbue.mngr_latchkey.remote.credentials import can_edit_host_policy_without_reading
from imbue.mngr_latchkey.remote.credentials import read_host_desktop_egress_rules
from imbue.mngr_latchkey.remote.credentials import read_host_permissions
from imbue.mngr_latchkey.remote.errors import MachineNewerError
from imbue.mngr_latchkey.remote.errors import MachinePolicyChangedError
from imbue.mngr_latchkey.remote.errors import MachineTransferError
from imbue.mngr_latchkey.remote.errors import is_caused_by_newer_machine
from imbue.mngr_latchkey.store import LatchkeyStoreError

# What a workspace whose permissions a newer Imbue Studio wrote is told: only a newer one can read them.
PERMISSIONS_FORMAT_NEWER_FAILURE: Final[UserFacingFailure] = UserFacingFailure(
    message=(
        "This workspace's permissions were changed by a newer Imbue Studio. Update Imbue Studio to see and "
        "change them here."
    ),
    remedy=ErrorRemedy.UPDATE_APP,
)


class MachineOperationError(Exception):
    """Raised when a change did not reach the machine that owns it.

    Terminal by the time it is raised: nothing retries behind the user's back,
    so whatever was asked for has not happened on the machine and the caller
    has to say so. The message is the technical account; ``explained_failure``
    is the sentence for a cause that explains itself to the user (the machine
    could not be located, its permissions are in a newer format, the machine
    itself was set up by a newer Imbue Studio), and None when
    the caller's own sentence for the action it attempted is the right one.
    """

    def __init__(self, message: str, explained_failure: UserFacingFailure | None) -> None:
        super().__init__(message)
        self.explained_failure = explained_failure

    @property
    def transfer(self) -> MachineTransferError | None:
        """The machine command that failed, naming what it was sending and what it expected back; ``None`` when
        no machine command is what failed (the machine could not be located, or a copy here was missing or
        unreadable)."""
        link = self.__cause__
        while link is not None:
            if isinstance(link, MachineTransferError):
                return link
            link = link.__cause__
        return None


class MachinePolicyChangedOperationError(MachineOperationError):
    """Raised when a push was not made from the policy the machine holds, so the machine applied none of it.

    Nothing is wrong with the machine: its policy was changed from somewhere
    else since this computer's copy was taken, so the edit has to be made
    again on a copy read back from it.
    """


def _explained_failure(cause: Exception) -> UserFacingFailure | None:
    """The sentence for a failure that explains itself, looked for in ``cause`` and in every error that caused it.

    A cause that explains itself is routinely wrapped on its way here (a
    machine command's failure, in the error of the transfer it was part of),
    so the first one found along the chain is what the user is told.
    """
    link: BaseException | None = cause
    while link is not None:
        if isinstance(link, MachineUnreachableError):
            return link.failure
        if isinstance(link, PermissionsFormatNewerError):
            return PERMISSIONS_FORMAT_NEWER_FAILURE
        if isinstance(link, MachineNewerError):
            return MACHINE_NEWER_FAILURE
        link = link.__cause__
    return None


def describe_machine_failure(error: MachineOperationError, sentence: str) -> UserFacingFailure:
    """The failure to show for ``error``: its own explanation when it has one, else ``sentence``."""
    return error.explained_failure or UserFacingFailure(message=sentence, remedy=None)


class MachineOperator(MutableModel):
    """Everything the desktop does to a workspace's machine, synchronously.

    A machine owns both halves of what a workspace may do: the credentials it
    holds, and the policy its gateway checks requests against. So both are
    carried the same way -- edited on this computer, then pushed to the machine
    in a single round trip, with the caller blocked until it lands.

    Resolves, per workspace, whether there is a machine to hand anything to: a
    workspace whose agents run on this computer already has both halves where
    its gateway reads them.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    access: MachineAccess = Field(frozen=True, description="Opens the machine behind a remote workspace.")

    def refresh(self, workspace_agent_id: str) -> None:
        """Make this computer's copies of a workspace's machine say what the machine holds.

        Run before the Permissions tab is built, so what it shows is the
        machine's own credentials and policy -- including anything another of
        the user's computers granted -- rather than whatever was left here by
        the last change; and before an edit of copies that are not known to be
        the machine's, so the push carries those grants rather than this
        computer's stale copy (see the module docstring). One round trip for
        both halves.

        Raises:
            MachineOperationError: when the machine cannot be read. The caller
                decides whether that is worth failing the whole view, or the
                write, for.
        """
        with self._machine(workspace_agent_id) as machine:
            if machine is not None:
                machine.refresh(PermissionsMigrationContext(device_id=DesktopDeviceId(self.access.device_id)))

    def connect_service(self, workspace_agent_id: str, service_name: str, account: str) -> None:
        """Hand an account just connected here to the workspace's machine.

        ``account`` scopes the handover to the one account the user just signed
        in, so the machine's other accounts of the same service -- which it may
        have refreshed since this computer last read it -- are never written
        over. An empty ``account`` hands over every stored account of the
        service, for the callers that cannot tell which account a sign-in
        produced.

        Raises:
            MachineOperationError: when the machine does not take it.
        """
        with self._machine(workspace_agent_id) as machine:
            if machine is not None:
                machine.connect_service(service_name, account)

    def disconnect_account(
        self, workspace_agent_id: str, service_name: str, account: str, is_service_deregistered: bool
    ) -> None:
        """Clear one account of one service from the workspace's machine, deregistering the service when asked to.

        The caller has already cleared this computer's copy; this is what takes
        the credential away from the machine itself, whose agents would
        otherwise keep using it. ``is_service_deregistered`` says the account
        was a custom service's last, so the machine's gateway stops naming the
        service in the same change; the caller drops it from the machine store
        here once the grants that named it are gone.

        Raises:
            MachineOperationError: when the machine does not take it.
        """
        with self._machine(workspace_agent_id) as machine:
            if machine is not None:
                machine.disconnect_account(service_name, account, is_service_deregistered)

    def connect_service_with_permissions(self, workspace_agent_id: str, service_name: str, account: str) -> None:
        """Carry a granted account and the policy that grants it as one change.

        Both halves of what the agent may now do travel together, applied
        credential-first on the machine and failed together, so the grant costs
        one round trip rather than two -- and neither half can land without the
        other.

        Raises:
            MachineOperationError: when the policy cannot be read here, or the
                machine does not take the pair.
        """
        with self._machine(workspace_agent_id) as machine:
            if machine is None:
                return
            permissions_json = self._host_permissions(machine.host_id)
            if permissions_json is None:
                machine.connect_service(service_name, account)
            else:
                machine.connect_service_with_permissions(service_name, account, permissions_json)

    def connect_service_with_permissions_and_desktop_egress_rules(
        self, workspace_agent_id: str, service_name: str, account: str
    ) -> None:
        """Carry a granted account, the policy that grants it and the desktop egress rules as one change.

        What a grant that also changes the service's route asks for, once both
        copies have been edited here: one round trip instead of
        :meth:`connect_service_with_permissions` and then
        :meth:`push_permissions_and_desktop_egress_rules`, and nothing of the
        grant or the route lands without the rest.

        Raises:
            MachineOperationError: when a snapshot cannot be read, when there
                is no copy of the policy or of the rules to push, or when the
                machine does not take the change.
        """
        with self._machine(workspace_agent_id) as machine:
            if machine is None:
                return
            permissions_json = self._host_permissions(machine.host_id)
            if permissions_json is None:
                raise MachineOperationError(
                    f"This computer has no copy of the permissions of host {machine.host_id}.", None
                )
            machine.connect_service_with_permissions_and_desktop_egress_rules(
                service_name, account, permissions_json, self._host_desktop_egress_rules(machine.host_id)
            )

    def push_permissions(self, workspace_agent_id: str) -> None:
        """Make this computer's copy of a workspace's policy the one its machine enforces.

        Called once the edit has been made to the canonical file here, because
        what travels is a snapshot of that file: a whole policy rather than a
        delta, so what lands is exactly what the edit left behind. The edit
        has to have been made on a copy :meth:`refresh` brought up to date in
        the same operation, or one :meth:`carry_policy_edit` found still
        current: the machine takes the snapshot only while it holds the policy
        that copy was.

        Raises:
            MachinePolicyChangedOperationError: when the machine holds another
                policy by now. Nothing was applied.
            MachineOperationError: when the snapshot cannot be read, or the
                machine does not take it.
        """
        with self._machine(workspace_agent_id) as machine:
            if machine is None:
                return
            permissions_json = self._host_permissions(machine.host_id)
            if permissions_json is not None:
                machine.set_permissions(permissions_json)

    def push_permissions_and_desktop_egress_rules(self, workspace_agent_id: str) -> None:
        """Make this computer's copies of a workspace's policy and desktop egress rules the ones its machine holds.

        Called once both copies have been edited here. Both travel as whole
        snapshots in one round trip, so the machine never holds a policy that
        allows forwarding without the rules that route to it, or the reverse.
        A host with no copy of the policy here pushes nothing, as in
        :meth:`push_permissions`.

        Raises:
            MachineOperationError: when a snapshot cannot be read, when there
                is no copy of the rules to push, or when the machine does not
                take the pair.
        """
        with self._machine(workspace_agent_id) as machine:
            if machine is None:
                return
            permissions_json = self._host_permissions(machine.host_id)
            if permissions_json is None:
                return
            desktop_egress_rules_json = self._host_desktop_egress_rules(machine.host_id)
            machine.set_permissions_and_desktop_egress_rules(permissions_json, desktop_egress_rules_json)

    def carry_policy_edit(self, workspace_agent_id: str, edit: Callable[[], object]) -> None:
        """Make an edit to a workspace's policy and carry it to its machine, reading the machine only when that is needed.

        ``edit`` changes this computer's copies and pushes them, or finds
        nothing to change and pushes nothing. It runs on the copies as they
        stand while they are still what the machine last answered for, which
        costs the one round trip of its push. Copies that are anything else --
        never read, left edited by a push that failed, written by another
        process -- are read from the machine first. A machine that has moved
        on refuses the push; it is then read, which puts its policy over the
        copies here, and ``edit`` runs a second time on what was read. So
        ``edit`` has to be safe to run twice.

        Raises:
            MachineOperationError: when the machine has to be read and cannot
                be, or does not take the edit.
        """
        if not self._is_policy_edit_possible_without_reading(workspace_agent_id):
            self.refresh(workspace_agent_id)
        try:
            edit()
        except MachinePolicyChangedOperationError as e:
            logger.debug(
                "Reading the machine of workspace {} to make the edit again on the policy it holds: {}",
                workspace_agent_id,
                e,
            )
        else:
            return
        self.refresh(workspace_agent_id)
        edit()

    def _is_policy_edit_possible_without_reading(self, workspace_agent_id: str) -> bool:
        """Whether the workspace's policy can be edited here as it stands; true as well when it has no machine to read.

        Raises:
            MachineOperationError: when it is unknown whether the workspace
                has a machine, its machine is one this build leaves alone, or
                what is on record of it cannot be read.
        """
        with self._reporting_exchange_failures(workspace_agent_id):
            host_id = self.access.machine_host_for(workspace_agent_id)
            if host_id is None:
                return True
            if self.access.is_machine_recorded_as_newer(host_id):
                raise MachineRecordedAsNewerError(host_id)
            return can_edit_host_policy_without_reading(self.access.latchkey.plugin_data_dir, host_id)

    def _host_permissions(self, host_id: HostId) -> str | None:
        try:
            return read_host_permissions(self.access.latchkey.plugin_data_dir, host_id)
        except LatchkeyStoreError as e:
            raise MachineOperationError(
                f"Could not read this computer's copy of the permissions of host {host_id}: {e}", None
            ) from e

    def _host_desktop_egress_rules(self, host_id: HostId) -> str:
        try:
            desktop_egress_rules_json = read_host_desktop_egress_rules(self.access.latchkey.plugin_data_dir, host_id)
        except LatchkeyStoreError as e:
            raise MachineOperationError(
                f"Could not read this computer's copy of the desktop egress rules of host {host_id}: {e}", None
            ) from e
        if desktop_egress_rules_json is None:
            # The caller writes the copy immediately before pushing it.
            raise MachineOperationError(
                f"This computer has no copy of the desktop egress rules of host {host_id}.", None
            )
        return desktop_egress_rules_json

    @contextmanager
    def _machine(self, workspace_agent_id: str) -> Generator[MachineCredentials | None, None, None]:
        """Open the workspace's machine for one exchange, or yield ``None`` when it has none.

        A workspace with no machine of its own is the whole of the no-op case:
        its agents read what this computer already holds. Everything the
        exchange can fail with -- the machine unreachable, or reachable and
        refusing -- comes back to the caller as one error, whose
        :attr:`MachineOperationError.transfer` names the command that failed
        when one did.

        Raises:
            MachineOperationError: when there is a machine and the exchange
                against it did not succeed.
        """
        with self._reporting_exchange_failures(workspace_agent_id):
            host_id = self.access.machine_host_for(workspace_agent_id)
            if host_id is None:
                logger.debug("Workspace {} has no machine of its own; nothing to carry", workspace_agent_id)
                yield None
                return
            with self.access.open_machine(workspace_agent_id, host_id) as machine:
                yield machine

    @contextmanager
    def _reporting_exchange_failures(self, workspace_agent_id: str) -> Generator[None, None, None]:
        """Turn whatever an exchange with the workspace's machine fails with into the one error callers handle.

        Raises:
            MachineOperationError: for every failure of the exchange.
        """
        try:
            yield
        except MachinePolicyChangedError as e:
            # Nothing went wrong: the machine was changed from somewhere else, and the caller makes its edit again.
            logger.debug("Left the policy of workspace {} as its machine holds it: {}", workspace_agent_id, e)
            raise MachinePolicyChangedOperationError(str(e), None) from e
        except MACHINE_EXCHANGE_FAILURES as e:
            if is_caused_by_newer_machine(e) or isinstance(e, MachineRecordedAsNewerError):
                # Nothing went wrong: this build leaves that machine alone, and the caller says so.
                logger.debug("Left the newer machine of workspace {} alone: {}", workspace_agent_id, e)
            else:
                logger.warning("Failed a machine operation for workspace {}: {}", workspace_agent_id, e)
            raise MachineOperationError(str(e), _explained_failure(e)) from e
