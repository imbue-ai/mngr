import pytest
from pydantic import ValidationError

from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng_kanpan.data_types import AgentBoardEntry
from imbue.mng_kanpan.data_types import BoardSection
from imbue.mng_kanpan.data_types import BoardSnapshot
from imbue.mng_kanpan.data_types import CheckStatus
from imbue.mng_kanpan.data_types import PrInfo
from imbue.mng_kanpan.data_types import PrState


def test_pr_state_values() -> None:
    assert PrState.OPEN == "OPEN"
    assert PrState.CLOSED == "CLOSED"
    assert PrState.MERGED == "MERGED"


def test_board_section_values() -> None:
    assert BoardSection.STILL_COOKING == "STILL_COOKING"
    assert BoardSection.PR_BEING_REVIEWED == "PR_BEING_REVIEWED"
    assert BoardSection.PR_MERGED == "PR_MERGED"
    assert BoardSection.PR_CLOSED == "PR_CLOSED"


def test_check_status_values() -> None:
    assert CheckStatus.PASSING == "PASSING"
    assert CheckStatus.FAILING == "FAILING"
    assert CheckStatus.PENDING == "PENDING"
    assert CheckStatus.UNKNOWN == "UNKNOWN"


def test_pr_info_construction() -> None:
    pr = PrInfo(
        number=42,
        title="Add feature X",
        state=PrState.OPEN,
        url="https://github.com/org/repo/pull/42",
        head_branch="mng/my-agent-local",
        check_status=CheckStatus.PASSING,
        is_draft=False,
    )
    assert pr.number == 42
    assert pr.title == "Add feature X"
    assert pr.state == PrState.OPEN
    assert pr.url == "https://github.com/org/repo/pull/42"
    assert pr.head_branch == "mng/my-agent-local"
    assert pr.check_status == CheckStatus.PASSING


def test_pr_info_is_frozen() -> None:
    pr = PrInfo(
        number=42,
        title="Add feature X",
        state=PrState.OPEN,
        url="https://github.com/org/repo/pull/42",
        head_branch="mng/my-agent-local",
        check_status=CheckStatus.PASSING,
        is_draft=False,
    )
    with pytest.raises(ValidationError):
        pr.number = 99  # type: ignore[misc]


def test_agent_board_entry_construction() -> None:
    entry = AgentBoardEntry(
        name=AgentName("my-agent"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("local"),
    )
    assert entry.name == AgentName("my-agent")
    assert entry.state == AgentLifecycleState.RUNNING
    assert entry.provider_name == ProviderInstanceName("local")
    assert entry.branch is None
    assert entry.pr is None


def test_agent_board_entry_with_pr() -> None:
    pr = PrInfo(
        number=10,
        title="Fix bug",
        state=PrState.MERGED,
        url="https://github.com/org/repo/pull/10",
        head_branch="mng/my-agent-local",
        check_status=CheckStatus.PASSING,
        is_draft=False,
    )
    entry = AgentBoardEntry(
        name=AgentName("my-agent"),
        state=AgentLifecycleState.DONE,
        provider_name=ProviderInstanceName("local"),
        branch="mng/my-agent-local",
        pr=pr,
    )
    assert entry.branch == "mng/my-agent-local"
    assert entry.pr is not None
    assert entry.pr.number == 10


def test_board_snapshot_construction() -> None:
    entry = AgentBoardEntry(
        name=AgentName("agent-1"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("local"),
    )
    snapshot = BoardSnapshot(
        entries=(entry,),
        fetch_time_seconds=1.5,
    )
    assert len(snapshot.entries) == 1
    assert snapshot.entries[0].name == AgentName("agent-1")
    assert snapshot.errors == ()
    assert snapshot.fetch_time_seconds == 1.5


def test_board_snapshot_with_errors() -> None:
    snapshot = BoardSnapshot(
        entries=(),
        errors=("Connection failed", "Timeout"),
        fetch_time_seconds=0.3,
    )
    assert len(snapshot.entries) == 0
    assert len(snapshot.errors) == 2
    assert snapshot.errors[0] == "Connection failed"
