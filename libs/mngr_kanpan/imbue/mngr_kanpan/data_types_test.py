import pytest
from pydantic import ValidationError

from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import BoardSnapshot
from imbue.mngr_kanpan.data_types import CheckStatus
from imbue.mngr_kanpan.data_types import ColumnData
from imbue.mngr_kanpan.data_types import CustomColumnConfig
from imbue.mngr_kanpan.data_types import KanpanPluginConfig
from imbue.mngr_kanpan.data_types import PrInfo
from imbue.mngr_kanpan.data_types import PrState
from imbue.mngr_kanpan.data_types import RefreshHook


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
        head_branch="mngr/my-agent",
        check_status=CheckStatus.PASSING,
        is_draft=False,
    )
    assert pr.number == 42
    assert pr.title == "Add feature X"
    assert pr.state == PrState.OPEN
    assert pr.url == "https://github.com/org/repo/pull/42"
    assert pr.head_branch == "mngr/my-agent"
    assert pr.check_status == CheckStatus.PASSING


def test_pr_info_is_frozen() -> None:
    pr = PrInfo(
        number=42,
        title="Add feature X",
        state=PrState.OPEN,
        url="https://github.com/org/repo/pull/42",
        head_branch="mngr/my-agent",
        check_status=CheckStatus.PASSING,
        is_draft=False,
    )
    with pytest.raises(ValidationError):
        pr.number = 99


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
        head_branch="mngr/my-agent",
        check_status=CheckStatus.PASSING,
        is_draft=False,
    )
    entry = AgentBoardEntry(
        name=AgentName("my-agent"),
        state=AgentLifecycleState.DONE,
        provider_name=ProviderInstanceName("local"),
        branch="mngr/my-agent",
        pr=pr,
    )
    assert entry.branch == "mngr/my-agent"
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
        repo_pr_loaded={},
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
        repo_pr_loaded={},
        fetch_time_seconds=0.3,
    )
    assert len(snapshot.entries) == 0
    assert len(snapshot.errors) == 2
    assert snapshot.errors[0] == "Connection failed"


def test_agent_board_entry_column_data_default_empty() -> None:
    entry = AgentBoardEntry(
        name=AgentName("my-agent"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("local"),
    )
    assert entry.column_data.labels == {}
    assert entry.column_data.plugin_data == {}


def test_agent_board_entry_with_column_data() -> None:
    entry = AgentBoardEntry(
        name=AgentName("my-agent"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("local"),
        column_data=ColumnData(
            labels={"blocked": "yes"},
            plugin_data={"claude": {"cost": "1.50"}},
        ),
    )
    assert entry.column_data.labels == {"blocked": "yes"}
    assert entry.column_data.plugin_data["claude"]["cost"] == "1.50"


def test_kanpan_plugin_config_merge_with_columns() -> None:
    base = KanpanPluginConfig(
        columns={"blocked": CustomColumnConfig(header="BLOCKED")},
    )
    override = KanpanPluginConfig(
        columns={"wait": CustomColumnConfig(header="WAIT", plugin_name="claude", field="reason")},
    )
    merged = base.merge_with(override)
    assert "blocked" in merged.columns
    assert "wait" in merged.columns
    assert merged.columns["wait"].plugin_name == "claude"


def test_kanpan_plugin_config_merge_with_column_order() -> None:
    base = KanpanPluginConfig(column_order=["name", "state", "link"])
    override = KanpanPluginConfig(column_order=["name", "link"])
    merged = base.merge_with(override)
    assert merged.column_order == ["name", "link"]


def test_kanpan_plugin_config_merge_with_column_order_none_keeps_base() -> None:
    base = KanpanPluginConfig(column_order=["name", "state", "link"])
    override = KanpanPluginConfig()
    merged = base.merge_with(override)
    assert merged.column_order == ["name", "state", "link"]


def test_kanpan_plugin_config_merge_with_column_override_replaces() -> None:
    base = KanpanPluginConfig(
        columns={"blocked": CustomColumnConfig(header="OLD")},
    )
    override = KanpanPluginConfig(
        columns={"blocked": CustomColumnConfig(header="NEW")},
    )
    merged = base.merge_with(override)
    assert merged.columns["blocked"].header == "NEW"


def test_refresh_hook_construction() -> None:
    hook = RefreshHook(name="Check review", command="my-script")
    assert hook.name == "Check review"
    assert hook.command == "my-script"
    assert hook.enabled is True


def test_refresh_hook_disabled() -> None:
    hook = RefreshHook(name="Disabled hook", command="my-script", enabled=False)
    assert hook.enabled is False


def test_kanpan_config_merge_with_hooks() -> None:
    base = KanpanPluginConfig(
        on_before_refresh={"a": RefreshHook(name="Hook A", command="cmd-a")},
        on_after_refresh={"x": RefreshHook(name="Hook X", command="cmd-x")},
    )
    override = KanpanPluginConfig(
        on_before_refresh={"b": RefreshHook(name="Hook B", command="cmd-b")},
        on_after_refresh={"x": RefreshHook(name="Hook X Override", command="cmd-x2")},
    )
    merged = base.merge_with(override)
    assert len(merged.on_before_refresh) == 2
    assert merged.on_before_refresh["a"].name == "Hook A"
    assert merged.on_before_refresh["b"].name == "Hook B"
    assert len(merged.on_after_refresh) == 1
    assert merged.on_after_refresh["x"].name == "Hook X Override"


def test_kanpan_config_merge_with_empty_hooks() -> None:
    base = KanpanPluginConfig(
        on_before_refresh={"a": RefreshHook(name="Hook A", command="cmd-a")},
    )
    override = KanpanPluginConfig()
    merged = base.merge_with(override)
    assert len(merged.on_before_refresh) == 1
    assert merged.on_before_refresh["a"].name == "Hook A"
    assert merged.on_after_refresh == {}
