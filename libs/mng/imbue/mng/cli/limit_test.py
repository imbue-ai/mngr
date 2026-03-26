import json

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.cli.limit import LimitCliOptions
from imbue.mng.cli.limit import _build_updated_activity_config
from imbue.mng.cli.limit import _build_updated_permissions
from imbue.mng.cli.limit import _has_agent_level_settings
from imbue.mng.cli.limit import _has_any_setting
from imbue.mng.cli.limit import _has_host_level_settings
from imbue.mng.cli.limit import _output_result
from imbue.mng.cli.limit import limit
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.interfaces.data_types import ActivityConfig
from imbue.mng.primitives import ActivitySource
from imbue.mng.primitives import IdleMode
from imbue.mng.primitives import OutputFormat
from imbue.mng.primitives import Permission


def _make_limit_opts(
    idle_timeout: str | None = None,
    idle_mode: str | None = None,
    activity_sources: str | None = None,
    add_activity_source: tuple[str, ...] = (),
    remove_activity_source: tuple[str, ...] = (),
    start_on_boot: bool | None = None,
    grant: tuple[str, ...] = (),
    revoke: tuple[str, ...] = (),
) -> LimitCliOptions:
    """Create a LimitCliOptions with sensible defaults, allowing overrides."""
    return LimitCliOptions(
        agents=(),
        agent_list=(),
        hosts=(),
        limit_all=False,
        dry_run=False,
        include=(),
        exclude=(),
        start_on_boot=start_on_boot,
        idle_timeout=idle_timeout,
        idle_mode=idle_mode,
        activity_sources=activity_sources,
        add_activity_source=add_activity_source,
        remove_activity_source=remove_activity_source,
        grant=grant,
        revoke=revoke,
        refresh_ssh_keys=False,
        add_ssh_key=(),
        remove_ssh_key=(),
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
    )


def test_limit_cli_options_fields() -> None:
    """Test LimitCliOptions has required fields."""
    opts = LimitCliOptions(
        agents=("agent1", "agent2"),
        agent_list=("agent3",),
        hosts=(),
        limit_all=False,
        dry_run=True,
        include=(),
        exclude=(),
        start_on_boot=None,
        idle_timeout=None,
        idle_mode=None,
        activity_sources=None,
        add_activity_source=(),
        remove_activity_source=(),
        grant=(),
        revoke=(),
        refresh_ssh_keys=False,
        add_ssh_key=(),
        remove_ssh_key=(),
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
    )
    assert opts.agents == ("agent1", "agent2")
    assert opts.agent_list == ("agent3",)
    assert opts.limit_all is False
    assert opts.dry_run is True
    assert opts.hosts == ()


def test_limit_requires_target(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that limit requires at least one agent, host, or --all."""
    result = cli_runner.invoke(
        limit,
        ["--idle-timeout", "300"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Must specify at least one agent, --host, or --all" in result.output


def test_limit_requires_setting(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that limit requires at least one setting to change."""
    result = cli_runner.invoke(
        limit,
        ["my-agent"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Must specify at least one setting to change" in result.output


def test_limit_cannot_combine_agents_and_all(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --all cannot be combined with agent names."""
    result = cli_runner.invoke(
        limit,
        ["my-agent", "--all", "--idle-timeout", "300"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Cannot specify both agent names and --all" in result.output


def test_limit_all_with_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test --all when no agents exist (should succeed with 'no agents found')."""
    result = cli_runner.invoke(
        limit,
        ["--all", "--idle-timeout", "300"],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "No agents found to configure" in result.output


def test_limit_host_only_rejects_agent_settings(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that agent-level settings are rejected when only --host is specified."""
    result = cli_runner.invoke(
        limit,
        ["--host", "some-host", "--start-on-boot"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Agent-level settings" in result.output


def test_build_updated_permissions_grant() -> None:
    """Test that grant adds permissions."""
    current = [Permission("read")]
    result = _build_updated_permissions(
        current=current,
        grant=("write", "execute"),
        revoke=(),
    )
    result_strs = [str(p) for p in result]
    assert "read" in result_strs
    assert "write" in result_strs
    assert "execute" in result_strs


def test_build_updated_permissions_revoke() -> None:
    """Test that revoke removes permissions."""
    current = [Permission("read"), Permission("write"), Permission("execute")]
    result = _build_updated_permissions(
        current=current,
        grant=(),
        revoke=("write",),
    )
    result_strs = [str(p) for p in result]
    assert "read" in result_strs
    assert "write" not in result_strs
    assert "execute" in result_strs


def test_build_updated_permissions_grant_and_revoke() -> None:
    """Test grant and revoke in one call."""
    current = [Permission("read"), Permission("write")]
    result = _build_updated_permissions(
        current=current,
        grant=("network",),
        revoke=("write",),
    )
    result_strs = [str(p) for p in result]
    assert "read" in result_strs
    assert "network" in result_strs
    assert "write" not in result_strs


def test_build_updated_activity_config_idle_timeout() -> None:
    """Test changing just the idle timeout with a plain integer string."""
    current = ActivityConfig(
        idle_timeout_seconds=3600,
        activity_sources=(ActivitySource.CREATE, ActivitySource.BOOT),
    )
    result = _build_updated_activity_config(
        current=current,
        idle_timeout_str="300",
        idle_mode_str=None,
        activity_sources_str=None,
        add_activity_source=(),
        remove_activity_source=(),
    )
    assert result.idle_timeout_seconds == 300
    assert set(result.activity_sources) == {ActivitySource.CREATE, ActivitySource.BOOT}


def test_build_updated_activity_config_idle_timeout_duration_string() -> None:
    """Test changing idle timeout with a duration string like '5m'."""
    current = ActivityConfig(
        idle_timeout_seconds=3600,
        activity_sources=(ActivitySource.CREATE, ActivitySource.BOOT),
    )
    result = _build_updated_activity_config(
        current=current,
        idle_timeout_str="5m",
        idle_mode_str=None,
        activity_sources_str=None,
        add_activity_source=(),
        remove_activity_source=(),
    )
    assert result.idle_timeout_seconds == 300
    assert set(result.activity_sources) == {ActivitySource.CREATE, ActivitySource.BOOT}


def test_build_updated_activity_config_idle_mode() -> None:
    """Test changing the idle mode replaces activity sources with the mode's canonical set."""
    current = ActivityConfig(
        idle_timeout_seconds=3600,
        activity_sources=(ActivitySource.CREATE,),
    )
    result = _build_updated_activity_config(
        current=current,
        idle_timeout_str=None,
        idle_mode_str="disabled",
        activity_sources_str=None,
        add_activity_source=(),
        remove_activity_source=(),
    )
    assert result.idle_mode == IdleMode.DISABLED
    assert result.activity_sources == ()
    assert result.idle_timeout_seconds == 3600


def test_build_updated_activity_config_idle_mode_ssh() -> None:
    """Test that --idle-mode ssh sets the correct activity sources."""
    current = ActivityConfig(
        idle_timeout_seconds=3600,
        activity_sources=(ActivitySource.CREATE,),
    )
    result = _build_updated_activity_config(
        current=current,
        idle_timeout_str=None,
        idle_mode_str="ssh",
        activity_sources_str=None,
        add_activity_source=(),
        remove_activity_source=(),
    )
    assert result.idle_mode == IdleMode.SSH
    assert set(result.activity_sources) == {
        ActivitySource.SSH,
        ActivitySource.CREATE,
        ActivitySource.START,
        ActivitySource.BOOT,
    }


def test_build_updated_activity_config_replace_sources() -> None:
    """Test replacing activity sources entirely."""
    current = ActivityConfig(
        idle_timeout_seconds=3600,
        activity_sources=(ActivitySource.CREATE, ActivitySource.BOOT),
    )
    result = _build_updated_activity_config(
        current=current,
        idle_timeout_str=None,
        idle_mode_str=None,
        activity_sources_str="ssh,agent",
        add_activity_source=(),
        remove_activity_source=(),
    )
    assert set(result.activity_sources) == {ActivitySource.SSH, ActivitySource.AGENT}


def test_build_updated_activity_config_add_remove_source() -> None:
    """Test adding and removing activity sources."""
    current = ActivityConfig(
        idle_timeout_seconds=3600,
        activity_sources=(ActivitySource.CREATE, ActivitySource.BOOT),
    )
    result = _build_updated_activity_config(
        current=current,
        idle_timeout_str=None,
        idle_mode_str=None,
        activity_sources_str=None,
        add_activity_source=("ssh",),
        remove_activity_source=("boot",),
    )
    assert ActivitySource.SSH in result.activity_sources
    assert ActivitySource.CREATE in result.activity_sources
    assert ActivitySource.BOOT not in result.activity_sources


def test_activity_sources_mutually_exclusive_with_add_remove(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --activity-sources cannot be combined with --add/--remove-activity-source."""
    result = cli_runner.invoke(
        limit,
        [
            "--all",
            "--activity-sources",
            "ssh,agent",
            "--add-activity-source",
            "boot",
        ],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert result.exit_code != 0
    assert "Cannot combine --activity-sources with --add-activity-source" in result.output


def test_limit_all_dry_run_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--all --dry-run --idle-timeout with no agents reports none found."""
    result = cli_runner.invoke(
        limit,
        ["--all", "--dry-run", "--idle-timeout", "300"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "No agents found to configure" in result.output


def test_limit_all_json_format_no_agents(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--all --format json --idle-timeout with no agents exits 0."""
    result = cli_runner.invoke(
        limit,
        ["--all", "--format", "json", "--idle-timeout", "300"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    assert result.exit_code == 0


# =============================================================================
# _has_* helper function tests
# =============================================================================


@pytest.mark.parametrize(
    ("opts", "expected"),
    [
        pytest.param(_make_limit_opts(idle_timeout="300"), True, id="idle_timeout"),
        pytest.param(_make_limit_opts(idle_mode="ssh"), True, id="idle_mode"),
        pytest.param(_make_limit_opts(activity_sources="ssh,agent"), True, id="activity_sources"),
        pytest.param(_make_limit_opts(add_activity_source=("ssh",)), True, id="add_activity_source"),
        pytest.param(_make_limit_opts(remove_activity_source=("boot",)), True, id="remove_activity_source"),
        pytest.param(_make_limit_opts(), False, id="none"),
    ],
)
def test_has_host_level_settings(opts: LimitCliOptions, expected: bool) -> None:
    """_has_host_level_settings should detect whether any host-level setting is set."""
    assert _has_host_level_settings(opts) is expected


@pytest.mark.parametrize(
    ("opts", "expected"),
    [
        pytest.param(_make_limit_opts(start_on_boot=True), True, id="start_on_boot"),
        pytest.param(_make_limit_opts(grant=("read",)), True, id="grant"),
        pytest.param(_make_limit_opts(revoke=("write",)), True, id="revoke"),
        pytest.param(_make_limit_opts(), False, id="none"),
    ],
)
def test_has_agent_level_settings(opts: LimitCliOptions, expected: bool) -> None:
    """_has_agent_level_settings should detect whether any agent-level setting is set."""
    assert _has_agent_level_settings(opts) is expected


def test_has_any_setting_with_host_settings() -> None:
    """_has_any_setting should return True when host settings are set."""
    opts = _make_limit_opts(idle_timeout="300")
    assert _has_any_setting(opts) is True


def test_has_any_setting_with_agent_settings() -> None:
    """_has_any_setting should return True when agent settings are set."""
    opts = _make_limit_opts(start_on_boot=False)
    assert _has_any_setting(opts) is True


def test_has_any_setting_with_no_settings() -> None:
    """_has_any_setting should return False when no settings are changed."""
    opts = _make_limit_opts()
    assert _has_any_setting(opts) is False


# =============================================================================
# _output_result tests
# =============================================================================


def test_limit_output_result_human_with_changes(capsys: pytest.CaptureFixture[str]) -> None:
    """_output_result should show change count in HUMAN format."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    changes = [{"setting": "idle_timeout", "value": 300}]
    _output_result(changes, output_opts)
    captured = capsys.readouterr()
    assert "Applied 1 change(s)" in captured.out


def test_limit_output_result_human_empty(capsys: pytest.CaptureFixture[str]) -> None:
    """_output_result should not write in HUMAN format with no changes."""
    output_opts = OutputOptions(output_format=OutputFormat.HUMAN)
    _output_result([], output_opts)
    captured = capsys.readouterr()
    assert "Applied" not in captured.out


def test_limit_output_result_json(capsys: pytest.CaptureFixture[str]) -> None:
    """_output_result should output JSON data."""
    output_opts = OutputOptions(output_format=OutputFormat.JSON)
    changes = [{"setting": "idle_timeout", "value": 300}]
    _output_result(changes, output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["count"] == 1
    assert len(data["changes"]) == 1


def test_limit_output_result_jsonl(capsys: pytest.CaptureFixture[str]) -> None:
    """_output_result should output JSONL event."""
    output_opts = OutputOptions(output_format=OutputFormat.JSONL)
    changes = [{"setting": "idle_timeout", "value": 300}]
    _output_result(changes, output_opts)
    captured = capsys.readouterr()
    data = json.loads(captured.out.strip())
    assert data["event"] == "limit_result"
    assert data["count"] == 1
