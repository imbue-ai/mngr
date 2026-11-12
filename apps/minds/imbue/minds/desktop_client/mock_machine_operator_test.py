from pydantic import ConfigDict
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.latchkey.machine_access import MachineAccess
from imbue.minds.desktop_client.latchkey.machine_operations import MachineOperationError
from imbue.minds.desktop_client.latchkey.machine_operations import MachineOperator
from imbue.minds.desktop_client.testing import device_id_for_test
from imbue.mngr_latchkey.core import Latchkey


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

    def push_permissions(self, workspace_agent_id: str) -> None:
        self.pushed_agent_ids.append(workspace_agent_id)

    def push_permissions_and_desktop_egress_rules(self, workspace_agent_id: str) -> None:
        if self.push_refusal:
            raise MachineOperationError(self.push_refusal)
        self.pushed_with_desktop_egress_rules_agent_ids.append(workspace_agent_id)

    def refresh(self, workspace_agent_id: str) -> None:
        self.refreshed_agent_ids.append(workspace_agent_id)
        if self.refresh_refusal:
            raise MachineOperationError(self.refresh_refusal)


def build_recording_machine_operator(
    latchkey: Latchkey, push_refusal: str = "", refresh_refusal: str = ""
) -> RecordingMachineOperator:
    """An operator whose machine access is never opened, because nothing here reaches one."""
    return RecordingMachineOperator(
        access=MachineAccess(
            latchkey=latchkey,
            device_id=device_id_for_test("permissions"),
            concurrency_group=ConcurrencyGroup(name="test-machine-access"),
            backend_resolver=MngrCliBackendResolver(),
        ),
        push_refusal=push_refusal,
        refresh_refusal=refresh_refusal,
    )
