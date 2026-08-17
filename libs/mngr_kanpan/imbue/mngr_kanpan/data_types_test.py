from datetime import datetime
from datetime import timezone
from typing import Any

import pytest
from pydantic import ValidationError

from imbue.mngr.config.data_types import LoggingConfig
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import PluginName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_kanpan.data_sources.github import CiField
from imbue.mngr_kanpan.data_sources.github import CiStatus
from imbue.mngr_kanpan.data_sources.github import PrField
from imbue.mngr_kanpan.data_sources.github import PrState
from imbue.mngr_kanpan.data_types import AgentBoardEntry
from imbue.mngr_kanpan.data_types import BoardSection
from imbue.mngr_kanpan.data_types import BoardSnapshot
from imbue.mngr_kanpan.data_types import CustomCommand
from imbue.mngr_kanpan.data_types import KanpanConfigError
from imbue.mngr_kanpan.data_types import KanpanPluginConfig
from imbue.mngr_kanpan.data_types import section_label


def test_section_label_with_suffix() -> None:
    assert section_label(BoardSection.PR_MERGED) == "Done - PR merged"
    assert section_label(BoardSection.PR_BEING_REVIEWED) == "In review - PR pending"


def test_section_label_without_suffix() -> None:
    assert section_label(BoardSection.MUTED) == "Muted"


def test_section_label_covers_every_section() -> None:
    # Every BoardSection must have prefix/suffix entries, or section_label raises KeyError.
    for section in BoardSection:
        assert section_label(section)


def test_ci_status_color() -> None:
    assert CiStatus.SUCCESS.color == "light green"
    assert CiStatus.FAILURE.color == "light red"
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
    ci = CiField(status=CiStatus.FAILURE, created=datetime(2025, 1, 1, 0, 0, 2, tzinfo=timezone.utc))
    cell = ci.display()
    assert cell.text == "failure"
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
        agent_id=AgentId.generate(),
        host_id=HostId.generate(),
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
        agent_id=AgentId.generate(),
        host_id=HostId.generate(),
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
        agent_id=AgentId.generate(),
        host_id=HostId.generate(),
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


def test_custom_command_prompt_defaults_to_empty() -> None:
    # An empty prompt is the off switch: no separate boolean, no content validation.
    command = CustomCommand(name="connect", command="mngr connect $MNGR_AGENT_NAME")
    assert command.prompt == ""


def test_custom_command_with_prompt_round_trips_through_toml_shape() -> None:
    # `_load_user_commands` re-validates raw TOML dicts through `CustomCommand(**value)`.
    raw_toml_entry: dict[str, Any] = {
        "name": "tag",
        "prompt": "tag: ",
        "command": 'mngr label "$MNGR_AGENT_NAME" -l "tag=$MNGR_INPUT"',
        "refresh_afterwards": True,
    }
    command = CustomCommand(**raw_toml_entry)
    assert command.prompt == "tag: "
    assert command.refresh_afterwards is True
    assert command.model_dump()["prompt"] == "tag: "


def test_custom_command_rejects_unknown_key() -> None:
    misspelled_toml_entry: dict[str, Any] = {"name": "tag", "prmopt": "tag: "}
    with pytest.raises(ValidationError):
        CustomCommand(**misspelled_toml_entry)


def test_custom_command_allows_prompt_with_markable_for_batch_prompting() -> None:
    # Marking asks for the value once when `x` executes, so the pair is legal: the key
    # press marks, and the prompt happens at execution rather than never opening.
    for markable in (True, "light red", ""):
        command = CustomCommand(name="tag", prompt="tag: ", command="echo hi", markable=markable)
        assert command.prompt == "tag: "
        assert command.is_markable is True


def test_custom_command_allows_markable_without_prompt() -> None:
    command = CustomCommand(name="stop", command="mngr stop $MNGR_AGENT_NAME", markable=True)
    assert command.markable is True
    assert command.prompt == ""


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


def test_check_refresh_intervals_rejects_what_the_config_loader_lets_through() -> None:
    # `_parse_plugins` builds plugin config with `model_construct`, so the `gt=0` declared on the
    # field never runs on the path a `mngr.toml` takes -- which is why this check exists at all.
    # An interval that is not positive leaves the board's alarm permanently due, and urwid
    # repaints only on the idle iteration that then never comes.
    KanpanPluginConfig().check_refresh_intervals()
    KanpanPluginConfig.model_construct(local_refresh_interval_seconds=5.0).check_refresh_intervals()
    # Zero asks the local refresh for no timer, so it arms no alarm and is not the spin case.
    KanpanPluginConfig.model_construct(local_refresh_interval_seconds=0.0).check_refresh_intervals()
    for setting, config in (
        ("local_refresh_interval_seconds", KanpanPluginConfig.model_construct(local_refresh_interval_seconds=-3.0)),
        ("refresh_interval_seconds", KanpanPluginConfig.model_construct(refresh_interval_seconds=0.0)),
        ("refresh_interval_seconds", KanpanPluginConfig.model_construct(refresh_interval_seconds=-3.0)),
    ):
        with pytest.raises(KanpanConfigError, match=setting):
            config.check_refresh_intervals()


# The kanpan plugin no longer carries a custom ``merge_with`` that unions its dict fields
# across config scopes. Cross-scope merges now go through the standard overlay pipeline,
# which assigns-by-default and surfaces any cross-scope drop through the narrowing paths
# returned by ``MngrConfig.merge_with``. These tests lock in that the per-field
# paths (``plugins.kanpan.<field>``) are flagged on a drop and pass on a pure superset addition.


def _kanpan_mngr_config_base(plugin_config: KanpanPluginConfig) -> MngrConfig:
    # ``model_construct`` mirrors how the loader builds the lower (base) scope: every
    # top-level container is populated so the narrowing walk has a fully-formed base.
    return MngrConfig.model_construct(
        prefix="mngr-",
        default_host_dir="~/.mngr",
        agent_types={},
        providers={},
        plugins={PluginName("kanpan"): plugin_config},
        logging=LoggingConfig(),
        commands={},
    )


def _kanpan_mngr_config_override(plugin_config: KanpanPluginConfig) -> MngrConfig:
    # ``model_construct`` mirrors the higher (override) scope: only ``plugins`` is set, so
    # ``model_fields_set`` reflects exactly the field the higher scope wrote.
    return MngrConfig.model_construct(plugins={PluginName("kanpan"): plugin_config})


def test_kanpan_commands_cross_scope_drop_is_flagged_as_narrowing() -> None:
    base = _kanpan_mngr_config_base(
        KanpanPluginConfig.model_construct(
            commands={
                "a": CustomCommand.model_construct(name="A"),
                "b": CustomCommand.model_construct(name="B"),
            }
        )
    )
    override = _kanpan_mngr_config_override(
        KanpanPluginConfig.model_construct(commands={"a": CustomCommand.model_construct(name="A")})
    )
    _, narrowings = base.merge_with(override)
    assert "plugins.kanpan.commands" in narrowings


def test_kanpan_commands_cross_scope_superset_does_not_narrow() -> None:
    base = _kanpan_mngr_config_base(
        KanpanPluginConfig.model_construct(commands={"a": CustomCommand.model_construct(name="A")})
    )
    override = _kanpan_mngr_config_override(
        KanpanPluginConfig.model_construct(
            commands={
                "a": CustomCommand.model_construct(name="A"),
                "b": CustomCommand.model_construct(name="B"),
            }
        )
    )
    _, narrowings = base.merge_with(override)
    assert "plugins.kanpan.commands" not in narrowings


def test_kanpan_shell_commands_cross_scope_drop_is_flagged_as_narrowing() -> None:
    base = _kanpan_mngr_config_base(
        KanpanPluginConfig.model_construct(
            shell_commands={
                "slack": {"name": "Slack", "header": "SLACK", "command": "find-slack"},
                "jira": {"name": "Jira", "header": "JIRA", "command": "find-jira"},
            }
        )
    )
    override = _kanpan_mngr_config_override(
        KanpanPluginConfig.model_construct(
            shell_commands={"slack": {"name": "Slack", "header": "SLACK", "command": "find-slack"}}
        )
    )
    _, narrowings = base.merge_with(override)
    assert "plugins.kanpan.shell_commands" in narrowings


def test_kanpan_shell_commands_cross_scope_superset_does_not_narrow() -> None:
    base = _kanpan_mngr_config_base(
        KanpanPluginConfig.model_construct(
            shell_commands={"slack": {"name": "Slack", "header": "SLACK", "command": "find-slack"}}
        )
    )
    override = _kanpan_mngr_config_override(
        KanpanPluginConfig.model_construct(
            shell_commands={
                "slack": {"name": "Slack", "header": "SLACK", "command": "find-slack"},
                "jira": {"name": "Jira", "header": "JIRA", "command": "find-jira"},
            }
        )
    )
    _, narrowings = base.merge_with(override)
    assert "plugins.kanpan.shell_commands" not in narrowings
