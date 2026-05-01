from datetime import datetime
from datetime import timezone

import pytest
from pydantic import ValidationError

from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_kanpan.data_sources.github import CiField
from imbue.mngr_kanpan.data_sources.github import CiStatus
from imbue.mngr_kanpan.data_sources.github import PrField
from imbue.mngr_kanpan.data_sources.github import PrState
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import BoardSnapshot
from imbue.mngr_kanpan.data_types import KanpanPluginConfig


def test_ci_status_color() -> None:
    assert CiStatus.PASSING.color == "light green"
    assert CiStatus.FAILING.color == "light red"
    assert CiStatus.PENDING.color == "yellow"
    assert CiStatus.UNKNOWN.color is None


def test_pr_field_display() -> None:
    pr = PrField(
        number=42,
        title="Add feature X",
        state=PrState.OPEN,
        url="https://github.com/org/repo/pull/42",
        head_branch="mngr/my-agent",
        is_draft=False,
        created=datetime(2025, 1, 1, 0, 0, 1, tzinfo=timezone.utc),
    )
    cell = pr.display()
    assert cell.text == "#42"
    assert cell.url == "https://github.com/org/repo/pull/42"


def test_ci_field_display() -> None:
    ci = CiField(status=CiStatus.FAILING, created=datetime(2025, 1, 1, 0, 0, 2, tzinfo=timezone.utc))
    cell = ci.display()
    assert cell.text == "failing"
    assert cell.color == "light red"


def test_ci_field_display_unknown() -> None:
    ci = CiField(status=CiStatus.UNKNOWN, created=datetime(2025, 1, 1, 0, 0, 3, tzinfo=timezone.utc))
    cell = ci.display()
    assert cell.text == ""


def test_pr_field_is_frozen() -> None:
    pr = PrField(
        number=42,
        title="Add feature X",
        state=PrState.OPEN,
        url="https://github.com/org/repo/pull/42",
        head_branch="mngr/my-agent",
        is_draft=False,
        created=datetime(2025, 1, 1, 0, 0, 4, tzinfo=timezone.utc),
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
    assert entry.fields == {}
    assert entry.cells == {}


def test_agent_board_entry_with_fields() -> None:
    pr = PrField(
        number=10,
        title="Fix bug",
        state=PrState.MERGED,
        url="https://github.com/org/repo/pull/10",
        head_branch="mngr/my-agent",
        is_draft=False,
        created=datetime(2025, 1, 1, 0, 0, 5, tzinfo=timezone.utc),
    )
    entry = AgentBoardEntry(
        name=AgentName("my-agent"),
        state=AgentLifecycleState.DONE,
        provider_name=ProviderInstanceName("local"),
        branch="mngr/my-agent",
        fields={"pr": pr},
    )
    assert entry.branch == "mngr/my-agent"
    assert "pr" in entry.fields


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


def test_kanpan_plugin_config_merge_with_column_order() -> None:
    base = KanpanPluginConfig(column_order=["name", "state", "ci"])
    override = KanpanPluginConfig(column_order=["name", "ci"])
    merged = base.merge_with(override)
    assert merged.column_order == ["name", "ci"]


def test_kanpan_plugin_config_merge_with_column_order_none_keeps_base() -> None:
    base = KanpanPluginConfig(column_order=["name", "state", "ci"])
    override = KanpanPluginConfig()
    merged = base.merge_with(override)
    assert merged.column_order == ["name", "state", "ci"]


def test_kanpan_plugin_config_merge_with_section_order() -> None:
    base = KanpanPluginConfig(section_order=[BoardSection.PR_MERGED, BoardSection.MUTED])
    override = KanpanPluginConfig(section_order=[BoardSection.STILL_COOKING, BoardSection.PR_MERGED])
    merged = base.merge_with(override)
    assert merged.section_order == [BoardSection.STILL_COOKING, BoardSection.PR_MERGED]


def test_kanpan_plugin_config_merge_with_section_order_none_keeps_base() -> None:
    base = KanpanPluginConfig(section_order=[BoardSection.PR_MERGED, BoardSection.MUTED])
    override = KanpanPluginConfig()
    merged = base.merge_with(override)
    assert merged.section_order == [BoardSection.PR_MERGED, BoardSection.MUTED]


def test_kanpan_config_merge_with_data_sources() -> None:
    base = KanpanPluginConfig(
        data_sources={"github": {"enabled": True}},
    )
    override = KanpanPluginConfig(
        data_sources={"github": {"enabled": False}},
    )
    merged = base.merge_with(override)
    assert merged.data_sources["github"]["enabled"] is False


def test_kanpan_config_merge_with_shell_commands() -> None:
    base = KanpanPluginConfig(
        shell_commands={
            "slack": {"name": "Slack", "header": "SLACK", "command": "find-slack"},
        },
    )
    override = KanpanPluginConfig(
        shell_commands={
            "jira": {"name": "Jira", "header": "JIRA", "command": "find-jira"},
        },
    )
    merged = base.merge_with(override)
    assert "slack" in merged.shell_commands
    assert "jira" in merged.shell_commands


def test_kanpan_plugin_config_staleness_threshold_default_unset() -> None:
    config = KanpanPluginConfig()
    assert config.staleness_threshold_seconds is None


def test_effective_staleness_threshold_defaults_to_90_percent_of_refresh_interval() -> None:
    config = KanpanPluginConfig(refresh_interval_seconds=600.0)
    assert config.effective_staleness_threshold_seconds() == 540.0


def test_effective_staleness_threshold_tracks_custom_refresh_interval() -> None:
    config = KanpanPluginConfig(refresh_interval_seconds=120.0)
    assert config.effective_staleness_threshold_seconds() == 108.0


def test_effective_staleness_threshold_uses_explicit_value_when_set() -> None:
    config = KanpanPluginConfig(refresh_interval_seconds=600.0, staleness_threshold_seconds=42.0)
    assert config.effective_staleness_threshold_seconds() == 42.0


def test_kanpan_plugin_config_merge_with_staleness_threshold() -> None:
    base = KanpanPluginConfig(staleness_threshold_seconds=600.0)
    override = KanpanPluginConfig(staleness_threshold_seconds=120.0)
    merged = base.merge_with(override)
    assert merged.staleness_threshold_seconds == 120.0
