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
from imbue.mngr_kanpan.data_types import CheckStatus
from imbue.mngr_kanpan.data_types import PrInfo
from imbue.mngr_kanpan.data_types import PrState


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


def make_pr_info(
    number: int = 1,
    head_branch: str = "mngr/test",
    state: PrState = PrState.OPEN,
    is_draft: bool = False,
) -> PrInfo:
    """Create a minimal PrInfo for testing."""
    return PrInfo(
        number=number,
        title=f"PR #{number}",
        state=state,
        url=f"https://github.com/org/repo/pull/{number}",
        head_branch=head_branch,
        check_status=CheckStatus.PASSING,
        is_draft=is_draft,
    )
