from datetime import datetime
from datetime import timezone

import pytest

from imbue.mngr.config.data_types import PluginConfig
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_kanpan.data_sources.github import CiField
from imbue.mngr_kanpan.data_sources.github import CiStatus
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import CustomCommand
from imbue.mngr_kanpan.data_types import KanpanPluginConfig
from imbue.mngr_kanpan.data_types import section_label
from imbue.mngr_kanpan.fetcher import compute_section
from imbue.mngr_kanpan.testing import make_pr_field


def test_section_label_with_suffix() -> None:
    assert section_label(BoardSection.PR_MERGED) == "Done - PR merged"
    assert section_label(BoardSection.PR_BEING_REVIEWED) == "In review - PR pending"


def test_section_label_without_suffix() -> None:
    assert section_label(BoardSection.MUTED) == "Muted"


def test_section_label_covers_every_section() -> None:
    # Every BoardSection must have prefix/suffix entries, or section_label raises KeyError.
    for section in BoardSection:
        assert section_label(section)


def test_pr_field_display() -> None:
    pr = make_pr_field(number=42, created=datetime(2025, 1, 1, 0, 0, 1, tzinfo=timezone.utc))
    cell = pr.display()
    assert cell.text == "#42"
    assert cell.url == "https://github.com/org/repo/pull/42"


@pytest.mark.parametrize(
    ("status", "expected_text", "expected_color"),
    [
        (CiStatus.SUCCESS, "success", "light green"),
        (CiStatus.FAILURE, "failure", "light red"),
        (CiStatus.PENDING, "pending", "yellow"),
        (CiStatus.UNKNOWN, "", None),
    ],
)
def test_ci_field_display(status: CiStatus, expected_text: str, expected_color: str | None) -> None:
    ci = CiField(status=status, created=datetime(2025, 1, 1, 0, 0, 2, tzinfo=timezone.utc))
    cell = ci.display()
    assert cell.text == expected_text
    assert cell.color == expected_color


def test_agent_board_entry_default_section_is_still_cooking() -> None:
    # An entry with no fields has no PR data; its default section must match
    # what compute_section() assigns to a fields-less agent, so a freshly
    # constructed entry lands under STILL_COOKING during board grouping.
    entry = AgentBoardEntry(
        name=AgentName("my-agent"),
        state=AgentLifecycleState.RUNNING,
        provider_name=ProviderInstanceName("local"),
    )
    assert entry.section == BoardSection.STILL_COOKING
    assert compute_section(entry.fields) == BoardSection.STILL_COOKING


def test_kanpan_plugin_config_merge_with_column_order_override_wins() -> None:
    base = KanpanPluginConfig(column_order=["name", "state", "ci"])
    override = KanpanPluginConfig(column_order=["name", "ci"])
    merged = base.merge_with(override)
    assert merged.column_order == ["name", "ci"]


def test_kanpan_plugin_config_merge_with_column_order_none_keeps_base() -> None:
    base = KanpanPluginConfig(column_order=["name", "state", "ci"])
    override = KanpanPluginConfig()
    merged = base.merge_with(override)
    assert merged.column_order == ["name", "state", "ci"]


def test_kanpan_plugin_config_merge_with_section_order_override_wins() -> None:
    base = KanpanPluginConfig(section_order=[BoardSection.PR_MERGED, BoardSection.MUTED])
    override = KanpanPluginConfig(section_order=[BoardSection.STILL_COOKING, BoardSection.PR_MERGED])
    merged = base.merge_with(override)
    assert merged.section_order == [BoardSection.STILL_COOKING, BoardSection.PR_MERGED]


def test_kanpan_plugin_config_merge_with_section_order_none_keeps_base() -> None:
    base = KanpanPluginConfig(section_order=[BoardSection.PR_MERGED, BoardSection.MUTED])
    override = KanpanPluginConfig()
    merged = base.merge_with(override)
    assert merged.section_order == [BoardSection.PR_MERGED, BoardSection.MUTED]


@pytest.mark.parametrize(
    ("base_field", "override_field"),
    [
        ({"github": {"enabled": True}}, {"repo_paths": {"enabled": False}}),
        ({"slack": {"name": "Slack"}}, {"jira": {"name": "Jira"}}),
        ({"col_a": {"header": "A"}}, {"col_b": {"header": "B"}}),
    ],
)
def test_kanpan_plugin_config_merge_with_raw_dict_field_unions_disjoint_keys(
    base_field: dict[str, dict[str, object]],
    override_field: dict[str, dict[str, object]],
) -> None:
    # data_sources / shell_commands / columns all merge via {**self, **override}.
    base = KanpanPluginConfig(data_sources=base_field, shell_commands=base_field, columns=base_field)
    override = KanpanPluginConfig(data_sources=override_field, shell_commands=override_field, columns=override_field)
    merged = base.merge_with(override)
    expected_keys = set(base_field) | set(override_field)
    assert set(merged.data_sources) == expected_keys
    assert set(merged.shell_commands) == expected_keys
    assert set(merged.columns) == expected_keys


def test_kanpan_plugin_config_merge_with_data_sources_override_wins_on_collision() -> None:
    base = KanpanPluginConfig(data_sources={"github": {"enabled": True}})
    override = KanpanPluginConfig(data_sources={"github": {"enabled": False}})
    merged = base.merge_with(override)
    assert merged.data_sources == {"github": {"enabled": False}}


def test_kanpan_plugin_config_merge_with_commands_unions_disjoint_keys() -> None:
    base = KanpanPluginConfig(commands={"a": CustomCommand(name="A", command="echo a")})
    override = KanpanPluginConfig(commands={"b": CustomCommand(name="B", command="echo b")})
    merged = base.merge_with(override)
    assert set(merged.commands) == {"a", "b"}


def test_kanpan_plugin_config_merge_with_commands_override_wins_on_collision() -> None:
    base = KanpanPluginConfig(commands={"x": CustomCommand(name="Base", command="echo base")})
    override = KanpanPluginConfig(commands={"x": CustomCommand(name="Override", command="echo override")})
    merged = base.merge_with(override)
    assert merged.commands == {"x": CustomCommand(name="Override", command="echo override")}


def test_kanpan_plugin_config_merge_with_hooks_union() -> None:
    base = KanpanPluginConfig(on_before_refresh={"a": 1}, on_after_refresh={"x": 1})
    override = KanpanPluginConfig(on_before_refresh={"b": 2}, on_after_refresh={"y": 2})
    merged = base.merge_with(override)
    assert merged.on_before_refresh == {"a": 1, "b": 2}
    assert merged.on_after_refresh == {"x": 1, "y": 2}


def test_kanpan_plugin_config_merge_with_enabled_override_wins() -> None:
    base = KanpanPluginConfig(enabled=True)
    override = KanpanPluginConfig(enabled=False)
    merged = base.merge_with(override)
    assert merged.enabled is False


def test_kanpan_plugin_config_merge_with_non_kanpan_override_returns_self() -> None:
    base = KanpanPluginConfig(column_order=["name", "ci"], enabled=False)
    merged = base.merge_with(PluginConfig(enabled=True))
    assert merged is base


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
