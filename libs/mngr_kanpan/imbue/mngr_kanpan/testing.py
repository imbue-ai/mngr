from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.interfaces.data_types import HostDetails
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_kanpan.data_source import CellDisplay
from imbue.mngr_kanpan.data_source import FieldValue
from imbue.mngr_kanpan.data_sources.github import AdditionalPrReference
from imbue.mngr_kanpan.data_sources.github import PrField
from imbue.mngr_kanpan.data_sources.github import PrState
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import BoardSnapshot
from imbue.mngr_kanpan.data_types import KanpanPluginConfig


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
    agent_id: AgentId | None = None,
) -> AgentDetails:
    """Create a minimal AgentDetails for testing."""
    return AgentDetails(
        id=agent_id or AgentId.generate(),
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


def make_mngr_ctx() -> MngrContext:
    """Create a bare minimal MngrContext for tests that just need the type."""
    return SimpleNamespace()  # ty: ignore[invalid-return-type]


def make_mngr_ctx_with_cg(cg: ConcurrencyGroup) -> MngrContext:
    """Create a MngrContext with a ConcurrencyGroup attached."""
    return SimpleNamespace(concurrency_group=cg)  # ty: ignore[invalid-return-type]


def make_mngr_ctx_with_config(config: KanpanPluginConfig) -> MngrContext:
    """Create a MngrContext that returns the given KanpanPluginConfig."""
    return SimpleNamespace(get_plugin_config=lambda name, cls: config)  # ty: ignore[invalid-return-type]


def make_mngr_ctx_with_profile_dir(profile_dir: Path) -> MngrContext:
    """Create a MngrContext with a profile_dir for field cache tests."""
    return SimpleNamespace(profile_dir=profile_dir)  # ty: ignore[invalid-return-type]


def make_pr_field(
    *,
    created: datetime,
    number: int = 1,
    state: PrState = PrState.OPEN,
    is_draft: bool = False,
    head_branch: str = "test-branch",
    additional_prs: tuple[AdditionalPrReference, ...] = (),
) -> PrField:
    """Create a PrField for testing."""
    return PrField(
        number=number,
        title="Test PR",
        state=state,
        url=f"https://github.com/org/repo/pull/{number}",
        head_branch=head_branch,
        is_draft=is_draft,
        additional_prs=additional_prs,
        created=created,
    )


def make_additional_pr(number: int, state: PrState = PrState.OPEN) -> AdditionalPrReference:
    """Create a reference to a PR on one of an agent's other worktree branches."""
    return AdditionalPrReference(number=number, url=f"https://github.com/org/repo/pull/{number}", state=state)


TEST_BOARD_HOST_ID: HostId = HostId("host-" + "0" * 31 + "d")


def make_board_entry(
    name: str = "test-agent",
    state: AgentLifecycleState = AgentLifecycleState.RUNNING,
    provider_name: str = "local",
    branch: str | None = None,
    is_muted: bool = False,
    section: BoardSection = BoardSection.STILL_COOKING,
    fields: Mapping[str, FieldValue] | None = None,
    cells: Mapping[str, CellDisplay] | None = None,
    agent_id: AgentId | None = None,
    host_id: HostId | None = None,
) -> AgentBoardEntry:
    """Create an AgentBoardEntry for testing."""
    return AgentBoardEntry(
        agent_id=agent_id or AgentId.generate(),
        # A fixed default host: tests that model "the same agent across two
        # snapshots" pass the same agent_id and must land on the same instance
        # key. Pass host_id explicitly to model agents on distinct hosts.
        host_id=host_id or TEST_BOARD_HOST_ID,
        name=AgentName(name),
        state=state,
        provider_name=ProviderInstanceName(provider_name),
        branch=branch,
        is_muted=is_muted,
        section=section,
        fields=dict(fields or {}),
        cells=dict(cells or {}),
    )


def make_board_snapshot(
    entries: tuple[AgentBoardEntry, ...] = (),
    errors: tuple[str, ...] = (),
    fetch_time_seconds: float = 1.5,
) -> BoardSnapshot:
    """Create a BoardSnapshot for testing."""
    return BoardSnapshot(
        entries=entries,
        errors=errors,
        fetch_time_seconds=fetch_time_seconds,
    )
