from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_kanpan.data_source import CiField
from imbue.mngr_kanpan.data_source import CiStatus
from imbue.mngr_kanpan.data_source import PrField
from imbue.mngr_kanpan.data_source import PrState


def make_host_details(provider_name: str = "local") -> HostDetails:
    """Create a minimal HostDetails for testing."""
    return HostDetails(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName(provider_name),
    )


def make_agent_details(
    name: str = "test-agent",
    state: AgentLifecycleState = AgentLifecycleState.RUNNING,
    work_dir: Path = Path("/tmp/test-work-dir"),
    provider_name: str = "local",
    initial_branch: str | None = None,
    labels: dict[str, str] | None = None,
    plugin: dict[str, Any] | None = None,
) -> AgentDetails:
    """Create a minimal AgentDetails for testing."""
    return AgentDetails(
        id=AgentId.generate(),
        name=AgentName(name),
        type="claude",
        command=CommandString("claude"),
        work_dir=work_dir,
        initial_branch=initial_branch,
        create_time=datetime.now(tz=timezone.utc),
        start_on_boot=False,
        state=state,
        host=make_host_details(provider_name),
        labels=labels or {},
        plugin=plugin or {},
    )


def make_pr_field(
    number: int = 1,
    head_branch: str = "mngr/test",
    state: PrState = PrState.OPEN,
    is_draft: bool = False,
) -> PrField:
    """Create a minimal PrField for testing."""
    return PrField(
        number=number,
        title=f"PR #{number}",
        state=state,
        url=f"https://github.com/org/repo/pull/{number}",
        head_branch=head_branch,
        is_draft=is_draft,
    )


def make_ci_field(
    status: CiStatus = CiStatus.PASSING,
) -> CiField:
    """Create a minimal CiField for testing."""
    return CiField(status=status)
