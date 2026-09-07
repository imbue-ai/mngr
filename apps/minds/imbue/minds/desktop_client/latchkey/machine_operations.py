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

A local workspace has no machine of its own -- its agents run here, on the
credentials and the permissions file this computer keeps -- so for those every
method is a no-op and the local edit is the whole change.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import paramiko
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.desktop_client.latchkey.machine_access import MachineAccess
from imbue.minds.desktop_client.latchkey.machine_access import MachineUnreachableError
from imbue.mngr.errors import MngrError
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.remote.credentials import MachineCredentials
from imbue.mngr_latchkey.remote.credentials import read_host_permissions
from imbue.mngr_latchkey.store import LatchkeyStoreError

# What each kind of change is called when it has to be reported to a user.
# One phrasing for every edit to a host's policy (a grant, a toggle, a revoke),
# because the user thinks of them as one kind of thing.
PERMISSIONS_FAILURE_DESCRIPTION = "apply the permission change on that workspace"


class MachineOperationError(Exception):
    """Raised when a change did not reach the machine that owns it.

    Terminal by the time it is raised: nothing retries behind the user's back,
    so whatever was asked for has not happened on the machine and the caller
    has to say so.
    """


def connect_failure_description(service_name: str) -> str:
    return f"connect {service_name} on that workspace"


def disconnect_failure_description(service_name: str) -> str:
    return f"disconnect {service_name} on that workspace"


def grant_failure_description(service_name: str) -> str:
    return f"apply the {service_name} grant on that workspace"


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
        the last change. One round trip for both halves.

        Raises:
            MachineOperationError: when the machine cannot be read. The caller
                decides whether that is worth failing the whole view for.
        """
        with self._machine(workspace_agent_id, "read that workspace's connections") as machine:
            if machine is not None:
                machine.refresh()

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
        with self._machine(workspace_agent_id, connect_failure_description(service_name)) as machine:
            if machine is not None:
                machine.connect_service(service_name, account)

    def disconnect_account(self, workspace_agent_id: str, service_name: str, account: str) -> None:
        """Clear one account of one service from the workspace's machine.

        The caller has already cleared this computer's copy; this is what takes
        the credential away from the machine itself, whose agents would
        otherwise keep using it.

        Raises:
            MachineOperationError: when the machine does not take it.
        """
        with self._machine(workspace_agent_id, disconnect_failure_description(service_name)) as machine:
            if machine is not None:
                machine.disconnect_account(service_name, account)

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
        failure_description = grant_failure_description(service_name)
        with self._machine(workspace_agent_id, failure_description) as machine:
            if machine is None:
                return
            permissions_json = self._host_permissions(machine.host_id, failure_description)
            if permissions_json is None:
                machine.connect_service(service_name, account)
            else:
                machine.connect_service_with_permissions(service_name, account, permissions_json)

    def push_permissions(self, workspace_agent_id: str) -> None:
        """Make this computer's copy of a workspace's policy the one its machine enforces.

        Called once the edit has been made to the canonical file here, because
        what travels is a snapshot of that file: a whole policy rather than a
        delta, so what lands is exactly what the edit left behind.

        Raises:
            MachineOperationError: when the snapshot cannot be read, or the
                machine does not take it.
        """
        with self._machine(workspace_agent_id, PERMISSIONS_FAILURE_DESCRIPTION) as machine:
            if machine is None:
                return
            permissions_json = self._host_permissions(machine.host_id, PERMISSIONS_FAILURE_DESCRIPTION)
            if permissions_json is not None:
                machine.set_permissions(permissions_json)

    def _host_permissions(self, host_id: HostId, failure_description: str) -> str | None:
        try:
            return read_host_permissions(self.access.latchkey.plugin_data_dir, host_id)
        except LatchkeyStoreError as e:
            raise MachineOperationError(f"Could not {failure_description}: {e}") from e

    @contextmanager
    def _machine(self, workspace_agent_id: str, failure_description: str) -> Iterator[MachineCredentials | None]:
        """Open the workspace's machine for one exchange, or yield ``None`` when it has none.

        A workspace with no machine of its own is the whole of the no-op case:
        its agents read what this computer already holds. Everything the
        exchange can fail with -- the machine unreachable, or reachable and
        refusing -- comes back to the caller as one error, described in terms of
        what they were trying to do.

        Raises:
            MachineOperationError: when there is a machine and the exchange
                against it did not succeed.
        """
        try:
            host_id = self.access.machine_host_for(workspace_agent_id)
            if host_id is None:
                logger.debug("Workspace {} has no machine of its own; nothing to carry", workspace_agent_id)
                yield None
                return
            with self.access.open_machine(workspace_agent_id, host_id) as machine:
                yield machine
        # ``LatchkeyError`` covers the machine refusing an exchange
        # (``RemoteGatewayError``); ``LatchkeyStoreError`` covers this
        # computer's own copies -- the machine store an adopt writes, the
        # canonical policy a push reads -- being unusable, which fails the same
        # action for the same user and must read the same way.
        except (
            MachineUnreachableError,
            LatchkeyError,
            LatchkeyStoreError,
            MngrError,
            OSError,
            paramiko.SSHException,
        ) as e:
            logger.warning("Could not {} for workspace {}: {}", failure_description, workspace_agent_id, e)
            raise MachineOperationError(f"Could not {failure_description}: {e}") from e
