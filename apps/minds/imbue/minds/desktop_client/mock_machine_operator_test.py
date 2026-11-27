from pydantic import ConfigDict
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.latchkey.machine_access import MachineAccess
from imbue.minds.desktop_client.latchkey.machine_operations import MachineOperationError
from imbue.minds.desktop_client.latchkey.machine_operations import MachineOperator
from imbue.minds.desktop_client.latchkey.machine_operations import MachinePolicyChangedOperationError
from imbue.minds.desktop_client.testing import device_id_for_test
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.primitives import MachinePolicyFingerprint
from imbue.mngr_latchkey.remote.credentials import _forget_machine_policy_record
from imbue.mngr_latchkey.remote.credentials import _write_machine_policy_record
from imbue.mngr_latchkey.store import permissions_path_for_host


class RecordingMachineOperator(MachineOperator):
    """A machine operator that records what it was told to read and carry, and carries nothing.

    The read of a workspace's machine and the push to it are the halves of a
    permissions write that nothing else here can see: this computer's copy
    looks right either way, and the difference only shows on the next read,
    when the machine's policy is adopted back over it.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    pushed_agent_ids: list[str] = Field(default_factory=list, description="Workspaces whose policy was pushed")
    pushed_with_desktop_egress_rules_agent_ids: list[str] = Field(
        default_factory=list, description="Workspaces whose policy and desktop egress rules were pushed together"
    )
    refreshed_agent_ids: list[str] = Field(
        default_factory=list, description="Workspaces whose machine was read back, one SSH round trip each"
    )
    push_refusal: str = Field(default="", description="When set, the reason every push is refused with")
    refresh_refusal: str = Field(default="", description="When set, the reason every read of a machine fails with")
    is_machine_changed_since_last_read: bool = Field(
        default=False,
        description="Whether the machine holds another policy than this computer last read, until it is read again",
    )
    policy_adopted_on_refresh: str | None = Field(
        default=None,
        description="When set, the policy a read puts over this computer's copy, as adopting the machine's does",
    )

    def push_permissions(self, workspace_agent_id: str) -> None:
        self._refuse_when_the_machine_has_changed(workspace_agent_id)
        self.pushed_agent_ids.append(workspace_agent_id)
        self._note_machine_holds_the_copies(workspace_agent_id)

    def push_permissions_and_desktop_egress_rules(self, workspace_agent_id: str) -> None:
        if self.push_refusal:
            raise MachineOperationError(self.push_refusal, None)
        self._refuse_when_the_machine_has_changed(workspace_agent_id)
        self.pushed_with_desktop_egress_rules_agent_ids.append(workspace_agent_id)
        self._note_machine_holds_the_copies(workspace_agent_id)

    def refresh(self, workspace_agent_id: str) -> None:
        self.refreshed_agent_ids.append(workspace_agent_id)
        if self.refresh_refusal:
            raise MachineOperationError(self.refresh_refusal, None)
        self.is_machine_changed_since_last_read = False
        host_id = self.access.machine_host_for(workspace_agent_id)
        if host_id is not None and self.policy_adopted_on_refresh is not None:
            permissions_path_for_host(self.access.latchkey.plugin_data_dir, host_id).write_text(
                self.policy_adopted_on_refresh
            )
        self._note_machine_holds_the_copies(workspace_agent_id)

    def _refuse_when_the_machine_has_changed(self, workspace_agent_id: str) -> None:
        """What a real push to a machine that has moved on comes to: nothing applied, and nothing left on record."""
        if not self.is_machine_changed_since_last_read:
            return
        host_id = self.access.machine_host_for(workspace_agent_id)
        if host_id is not None:
            _forget_machine_policy_record(self.access.latchkey.plugin_data_dir, host_id)
        raise MachinePolicyChangedOperationError("The machine holds another policy than was last read.", None)

    def _note_machine_holds_the_copies(self, workspace_agent_id: str) -> None:
        """What a real read or push leaves on record: the copies here are what the machine holds."""
        host_id = self.access.machine_host_for(workspace_agent_id)
        if host_id is not None:
            _write_machine_policy_record(
                self.access.latchkey.plugin_data_dir, host_id, MachinePolicyFingerprint("the-machines-fingerprint")
            )


def build_recording_machine_operator(
    latchkey: Latchkey,
    push_refusal: str = "",
    refresh_refusal: str = "",
    # Places workspaces on their hosts, for a test of what is carried to a machine; the default places none.
    backend_resolver: BackendResolverInterface | None = None,
) -> RecordingMachineOperator:
    """An operator whose machine access is never opened, because nothing here reaches one."""
    return RecordingMachineOperator(
        access=MachineAccess(
            latchkey=latchkey,
            device_id=device_id_for_test("permissions"),
            concurrency_group=ConcurrencyGroup(name="test-machine-access"),
            backend_resolver=backend_resolver if backend_resolver is not None else MngrCliBackendResolver(),
        ),
        push_refusal=push_refusal,
        refresh_refusal=refresh_refusal,
    )


def build_machine_operator_over(latchkey: Latchkey, backend_resolver: BackendResolverInterface) -> MachineOperator:
    """A real operator that places workspaces through ``backend_resolver``.

    For tests of what an operator decides before it opens a machine: whether a
    workspace has one, and whether this build leaves that machine alone.
    """
    return MachineOperator(
        access=MachineAccess(
            latchkey=latchkey,
            device_id=device_id_for_test("permissions"),
            concurrency_group=ConcurrencyGroup(name="test-machine-access"),
            backend_resolver=backend_resolver,
        )
    )
