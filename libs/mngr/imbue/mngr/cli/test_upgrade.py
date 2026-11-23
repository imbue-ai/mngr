import json
import os
from pathlib import Path
from typing import Final

import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.api.testing import UPGRADABLE_COMMAND_AGENT_TYPE
from imbue.mngr.api.testing import UPGRADED_SCRIPT_NAME
from imbue.mngr.api.testing import UPGRADE_CRASHING_COMMAND_AGENT_TYPE
from imbue.mngr.api.testing import UPGRADE_FAILING_COMMAND_AGENT_TYPE
from imbue.mngr.api.testing import UPGRADE_FAILURE_MESSAGE
from imbue.mngr.api.testing import UpgradeCrashError
from imbue.mngr.api.upgrade import SKIP_ABORTED
from imbue.mngr.api.upgrade import SKIP_IS_CALLER
from imbue.mngr.api.upgrade import SKIP_MID_TURN
from imbue.mngr.api.upgrade import UPGRADE_STAGING_DIR_NAME
from imbue.mngr.cli.exit_codes import EXIT_CODE_AGENTS_NOT_UPGRADED
from imbue.mngr.cli.exit_codes import EXIT_CODE_ERROR
from imbue.mngr.cli.exit_codes import EXIT_CODE_SUCCESS
from imbue.mngr.cli.testing import create_agent_with_events_dir
from imbue.mngr.cli.testing import create_test_agent_state
from imbue.mngr.cli.upgrade import upgrade
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.host import SHARED_SHELL_LIB_NAMES
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.utils.deps import RSYNC
from imbue.mngr.utils.plugin_testing import PLACEHOLDER_AGENT_TYPE
from imbue.mngr.utils.testing import create_test_agent_of_type_via_cli
from imbue.mngr.utils.testing import create_test_agent_via_cli
from imbue.mngr.utils.testing import read_tmux_pane_pid
from imbue.mngr.utils.testing import tmux_session_cleanup
from imbue.mngr.utils.testing import tmux_session_exists

_UPGRADED_SCRIPT_PATH: Final[Path] = Path("commands") / UPGRADED_SCRIPT_NAME
# What an upgrade rewrites for a stopped agent that was never provisioned, as JSON output lists it.
_UNPROVISIONED_CHANGED_PATHS: Final[list[str]] = [
    str(path) for path in sorted(Path("commands") / name for name in (*SHARED_SHELL_LIB_NAMES, UPGRADED_SCRIPT_NAME))
]


def _create_running_upgradable_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    agent_name: str,
    command: str,
) -> str:
    return create_test_agent_of_type_via_cli(
        cli_runner,
        temp_work_dir,
        mngr_test_prefix,
        plugin_manager,
        agent_name,
        UPGRADABLE_COMMAND_AGENT_TYPE,
        command,
    )


def _get_state_dir(host: Host, agent_name: str) -> Path:
    (agent,) = [agent for agent in host.get_agents() if agent.name == agent_name]
    return get_agent_state_dir_path(host.host_dir, agent.id)


def _create_agents_with_one_outcome_each(
    cli_runner: CliRunner, plugin_manager: pluggy.PluginManager, host: Host, work_dir: Path
) -> tuple[AgentInterface, AgentInterface, AgentInterface, AgentInterface]:
    """Create four stopped agents that an upgrade finds current, out of date, unsupported and failing to upgrade, in that order."""
    current_agent = create_test_agent_state(host, work_dir, "outcome-current-48213", UPGRADABLE_COMMAND_AGENT_TYPE)
    first_upgrade = cli_runner.invoke(upgrade, [str(current_agent.name)], obj=plugin_manager, catch_exceptions=False)
    assert first_upgrade.exit_code == EXIT_CODE_SUCCESS, first_upgrade.output
    stale_agent = create_test_agent_state(host, work_dir, "outcome-stale-48213", UPGRADABLE_COMMAND_AGENT_TYPE)
    unsupported_agent = create_test_agent_state(host, work_dir, "outcome-unsupported-48213")
    failing_agent = create_test_agent_state(
        host, work_dir, "outcome-failing-48213", UPGRADE_FAILING_COMMAND_AGENT_TYPE
    )
    return current_agent, stale_agent, unsupported_agent, failing_agent


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_dry_run_lists_the_files_it_would_rewrite_and_changes_nothing(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    upgradable_agent_types: None,
    local_host: Host,
) -> None:
    agent_name = "upgrade-cli-dry-48213"
    session_name = _create_running_upgradable_agent(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, "sleep 958214"
    )
    with tmux_session_cleanup(session_name):
        state_dir = _get_state_dir(local_host, agent_name)
        pane_pid_before = read_tmux_pane_pid(session_name)

        result = cli_runner.invoke(upgrade, [agent_name, "--dry-run"], obj=plugin_manager, catch_exceptions=False)

        assert result.exit_code == EXIT_CODE_SUCCESS
        assert result.stdout == f"would upgrade {agent_name} (restart)\n  {_UPGRADED_SCRIPT_PATH}\n"
        assert not (state_dir / _UPGRADED_SCRIPT_PATH).exists()
        assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()
        assert read_tmux_pane_pid(session_name) == pane_pid_before


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_restarts_an_idle_agent_and_then_finds_it_current(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    upgradable_agent_types: None,
    local_host: Host,
) -> None:
    agent_name = "upgrade-cli-idle-48213"
    session_name = _create_running_upgradable_agent(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, "sleep 958215"
    )
    with tmux_session_cleanup(session_name):
        state_dir = _get_state_dir(local_host, agent_name)
        pane_pid_before = read_tmux_pane_pid(session_name)

        result = cli_runner.invoke(upgrade, [agent_name], obj=plugin_manager, catch_exceptions=False)

        assert result.exit_code == EXIT_CODE_SUCCESS
        assert result.stdout == f"upgraded {agent_name} (restarted)\n"
        assert (state_dir / _UPGRADED_SCRIPT_PATH).is_file()
        assert tmux_session_exists(session_name)
        pane_pid_after_upgrade = read_tmux_pane_pid(session_name)
        assert pane_pid_after_upgrade != pane_pid_before

        second_result = cli_runner.invoke(
            upgrade, [agent_name, "--format", "json"], obj=plugin_manager, catch_exceptions=False
        )

        assert second_result.exit_code == EXIT_CODE_SUCCESS
        second_data = json.loads(second_result.stdout)
        assert [entry["agent_name"] for entry in second_data["current_agents"]] == [agent_name]
        assert second_data["exit_code"] == EXIT_CODE_SUCCESS
        assert read_tmux_pane_pid(session_name) == pane_pid_after_upgrade


@pytest.mark.tmux
def test_upgrade_finds_a_running_command_agent_current_and_leaves_it_running(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    agent_name = "upgrade-cli-command-48213"
    session_name = create_test_agent_via_cli(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, "sleep 958219"
    )
    with tmux_session_cleanup(session_name):
        pane_pid_before = read_tmux_pane_pid(session_name)

        result = cli_runner.invoke(upgrade, [agent_name, "--force"], obj=plugin_manager, catch_exceptions=False)

        assert result.exit_code == EXIT_CODE_SUCCESS
        assert result.stdout == f"current {agent_name}\n"
        assert read_tmux_pane_pid(session_name) == pane_pid_before


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_leaves_a_busy_agent_out_of_date_with_its_own_exit_code_unless_forced(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    upgradable_agent_types: None,
    local_host: Host,
) -> None:
    agent_name = "upgrade-cli-busy-48213"
    session_name = _create_running_upgradable_agent(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, "sleep 958216"
    )
    with tmux_session_cleanup(session_name):
        state_dir = _get_state_dir(local_host, agent_name)
        (state_dir / "active").touch()
        pane_pid_before = read_tmux_pane_pid(session_name)

        result = cli_runner.invoke(upgrade, [agent_name], obj=plugin_manager, catch_exceptions=False)

        assert result.exit_code == EXIT_CODE_AGENTS_NOT_UPGRADED
        assert result.stdout == f"skipped {agent_name}: {SKIP_MID_TURN.detail}\n"
        assert not (state_dir / _UPGRADED_SCRIPT_PATH).exists()
        assert read_tmux_pane_pid(session_name) == pane_pid_before

        forced_result = cli_runner.invoke(upgrade, [agent_name, "--force"], obj=plugin_manager, catch_exceptions=False)

        assert forced_result.exit_code == EXIT_CODE_SUCCESS
        assert forced_result.stdout == f"upgraded {agent_name} (restarted; interrupted RUNNING)\n"
        assert (state_dir / _UPGRADED_SCRIPT_PATH).is_file()
        assert read_tmux_pane_pid(session_name) != pane_pid_before


def _read_files_under(directory: Path) -> dict[Path, tuple[bytes, bool]]:
    """Each file under ``directory``, relative to it, with its bytes and whether it is executable."""
    return {
        path.relative_to(directory): (path.read_bytes(), os.access(path, os.X_OK))
        for path in directory.rglob("*")
        if path.is_file()
    }


@pytest.mark.rsync
@pytest.mark.parametrize("force_args", [[], ["--force"]], ids=["not_forced", "forced"])
def test_upgrade_leaves_the_agent_running_the_command_out_of_date_with_its_own_exit_code(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
    upgradable_agent_types: None,
    local_host: Host,
    force_args: list[str],
) -> None:
    agent = create_test_agent_state(
        local_host, temp_work_dir, "upgrade-cli-caller-48213", UPGRADABLE_COMMAND_AGENT_TYPE
    )
    state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)
    files_before = _read_files_under(state_dir)

    result = cli_runner.invoke(
        upgrade,
        [str(agent.name), *force_args, "--format", "json"],
        obj=plugin_manager,
        env={"MNGR_AGENT_ID": str(agent.id)},
        catch_exceptions=False,
    )

    assert result.exit_code == EXIT_CODE_AGENTS_NOT_UPGRADED
    assert json.loads(result.stdout) == {
        "upgraded_agents": [],
        "current_agents": [],
        "skipped_agents": [
            {
                "agent_id": str(agent.id),
                "agent_name": str(agent.name),
                "kind": "NOT_IDLE",
                "detail": SKIP_IS_CALLER.detail,
            }
        ],
        "failed_agents": [],
        "dry_run": False,
        "upgraded_count": 0,
        "current_count": 0,
        "skipped_count": 1,
        "failed_count": 0,
        "exit_code": EXIT_CODE_AGENTS_NOT_UPGRADED,
    }
    assert _read_files_under(state_dir) == files_before


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_finds_the_agent_running_the_command_current_when_its_files_are(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
    upgradable_agent_types: None,
    local_host: Host,
) -> None:
    agent = create_test_agent_state(
        local_host, temp_work_dir, "upgrade-cli-current-caller-48213", UPGRADABLE_COMMAND_AGENT_TYPE
    )
    first_upgrade = cli_runner.invoke(upgrade, [str(agent.name)], obj=plugin_manager, catch_exceptions=False)
    assert first_upgrade.exit_code == EXIT_CODE_SUCCESS, first_upgrade.output

    result = cli_runner.invoke(
        upgrade,
        [str(agent.name)],
        obj=plugin_manager,
        env={"MNGR_AGENT_ID": str(agent.id)},
        catch_exceptions=False,
    )

    assert result.exit_code == EXIT_CODE_SUCCESS
    assert result.stdout == f"current {agent.name}\n"


def test_upgrade_leaves_the_agent_running_the_command_unsupported_when_its_type_has_no_upgrade_support(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
) -> None:
    agent = create_test_agent_state(local_host, temp_work_dir, "upgrade-cli-unsupported-caller-48213")

    result = cli_runner.invoke(
        upgrade,
        [str(agent.name), "--format", "json"],
        obj=plugin_manager,
        env={"MNGR_AGENT_ID": str(agent.id)},
        catch_exceptions=False,
    )

    assert result.exit_code == EXIT_CODE_AGENTS_NOT_UPGRADED
    assert json.loads(result.stdout)["skipped_agents"] == [
        {
            "agent_id": str(agent.id),
            "agent_name": str(agent.name),
            "kind": "UNSUPPORTED",
            "detail": f"agent type '{PLACEHOLDER_AGENT_TYPE}' has no upgrade support",
        }
    ]


def test_upgrade_of_agents_with_no_files_to_stage_needs_no_rsync(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
    path_without_rsync: None,
) -> None:
    command_agent = create_test_agent_state(local_host, temp_work_dir, "no-rsync-command-48213", "command")
    unsupported_agent = create_test_agent_state(local_host, temp_work_dir, "no-rsync-unsupported-48213")
    assert not RSYNC.is_available()

    result = cli_runner.invoke(
        upgrade, [str(command_agent.name), str(unsupported_agent.name)], obj=plugin_manager, catch_exceptions=False
    )

    assert result.exit_code == EXIT_CODE_AGENTS_NOT_UPGRADED, result.output
    assert result.stdout.splitlines() == [
        f"current {command_agent.name}",
        f"skipped {unsupported_agent.name}: agent type '{PLACEHOLDER_AGENT_TYPE}' has no upgrade support",
        "0 upgraded, 1 current, 1 not upgraded, 0 failed",
    ]


def test_upgrade_of_an_agent_with_files_to_stage_ends_with_rsyncs_install_hint_when_rsync_is_missing(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
    upgradable_agent_types: None,
    local_host: Host,
    path_without_rsync: None,
) -> None:
    agent = create_test_agent_state(local_host, temp_work_dir, "no-rsync-stale-48213", UPGRADABLE_COMMAND_AGENT_TYPE)
    state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)
    files_before = _read_files_under(state_dir)

    result = cli_runner.invoke(upgrade, [str(agent.name)], obj=plugin_manager, catch_exceptions=True)

    assert result.exit_code == EXIT_CODE_ERROR
    assert result.stdout == ""
    assert result.stderr == (
        f"Error: rsync is required for {RSYNC.purpose} but was not found on PATH  [{RSYNC.install_hint}]\n"
    )
    assert _read_files_under(state_dir) == files_before


def _create_agents_around_a_failing_one(host: Host, work_dir: Path) -> tuple[AgentInterface, AgentInterface]:
    """Create three stopped agents, the second of which fails to upgrade, and return the first and the third."""
    first_agent = create_test_agent_state(host, work_dir, "upgrade-cli-first-48213", UPGRADABLE_COMMAND_AGENT_TYPE)
    create_test_agent_state(host, work_dir, "upgrade-cli-failing-48213", UPGRADE_FAILING_COMMAND_AGENT_TYPE)
    later_agent = create_test_agent_state(host, work_dir, "upgrade-cli-later-48213", UPGRADABLE_COMMAND_AGENT_TYPE)
    return first_agent, later_agent


@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.parametrize("on_error_args", [[], ["--on-error", "continue"]], ids=["default", "continue"])
def test_upgrade_that_fails_for_one_agent_goes_on_to_the_agents_named_after_it_and_exits_with_an_error(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
    upgradable_agent_types: None,
    local_host: Host,
    on_error_args: list[str],
) -> None:
    first_agent, later_agent = _create_agents_around_a_failing_one(local_host, temp_work_dir)

    result = cli_runner.invoke(
        upgrade,
        ["upgrade-cli-first-48213", "upgrade-cli-failing-48213", "upgrade-cli-later-48213", *on_error_args],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == EXIT_CODE_ERROR
    assert result.stdout.splitlines() == [
        "upgraded upgrade-cli-first-48213 (stopped)",
        f"failed upgrade-cli-failing-48213: prepare ({UPGRADE_FAILURE_MESSAGE}); untouched",
        "upgraded upgrade-cli-later-48213 (stopped)",
        "2 upgraded, 0 current, 0 not upgraded, 1 failed",
    ]
    assert (get_agent_state_dir_path(local_host.host_dir, first_agent.id) / _UPGRADED_SCRIPT_PATH).is_file()
    assert (get_agent_state_dir_path(local_host.host_dir, later_agent.id) / _UPGRADED_SCRIPT_PATH).is_file()


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_on_error_abort_stops_at_the_first_failure_in_a_dry_run_and_a_real_run_alike(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
    upgradable_agent_types: None,
    local_host: Host,
) -> None:
    first_agent, later_agent = _create_agents_around_a_failing_one(local_host, temp_work_dir)
    arguments = [
        "upgrade-cli-first-48213",
        "upgrade-cli-failing-48213",
        "upgrade-cli-later-48213",
        "--on-error",
        "abort",
    ]

    dry_run_result = cli_runner.invoke(upgrade, [*arguments, "--dry-run"], obj=plugin_manager, catch_exceptions=False)
    result = cli_runner.invoke(upgrade, arguments, obj=plugin_manager, catch_exceptions=False)

    assert dry_run_result.exit_code == EXIT_CODE_ERROR
    assert [line for line in dry_run_result.stdout.splitlines() if not line.startswith("  ")] == [
        "would upgrade upgrade-cli-first-48213 (stopped)",
        f"failed upgrade-cli-failing-48213: prepare ({UPGRADE_FAILURE_MESSAGE}); untouched",
        f"would skip upgrade-cli-later-48213: {SKIP_ABORTED.detail}",
        "1 would be upgraded, 0 current, 1 would not be upgraded, 1 failed",
    ]
    assert result.exit_code == EXIT_CODE_ERROR
    assert result.stdout.splitlines() == [
        "upgraded upgrade-cli-first-48213 (stopped)",
        f"failed upgrade-cli-failing-48213: prepare ({UPGRADE_FAILURE_MESSAGE}); untouched",
        f"skipped upgrade-cli-later-48213: {SKIP_ABORTED.detail}",
        "1 upgraded, 0 current, 1 not upgraded, 1 failed",
    ]
    assert (get_agent_state_dir_path(local_host.host_dir, first_agent.id) / _UPGRADED_SCRIPT_PATH).is_file()
    assert not (get_agent_state_dir_path(local_host.host_dir, later_agent.id) / _UPGRADED_SCRIPT_PATH).exists()


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_json_output_carries_every_outcome_and_the_exit_code(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
    upgradable_agent_types: None,
    local_host: Host,
) -> None:
    current_agent, stale_agent, unsupported_agent, failing_agent = _create_agents_with_one_outcome_each(
        cli_runner, plugin_manager, local_host, temp_work_dir
    )

    result = cli_runner.invoke(
        upgrade,
        [
            str(current_agent.name),
            str(stale_agent.name),
            str(unsupported_agent.name),
            str(failing_agent.name),
            "--format",
            "json",
        ],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == EXIT_CODE_ERROR
    assert json.loads(result.stdout) == {
        "upgraded_agents": [
            {
                "agent_id": str(stale_agent.id),
                "agent_name": str(stale_agent.name),
                "plan": "LEAVE_STOPPED",
                "prior_state": "STOPPED",
                "changed_paths": _UNPROVISIONED_CHANGED_PATHS,
            }
        ],
        "current_agents": [{"agent_id": str(current_agent.id), "agent_name": str(current_agent.name)}],
        "skipped_agents": [
            {
                "agent_id": str(unsupported_agent.id),
                "agent_name": str(unsupported_agent.name),
                "kind": "UNSUPPORTED",
                "detail": f"agent type '{PLACEHOLDER_AGENT_TYPE}' has no upgrade support",
            }
        ],
        "failed_agents": [
            {
                "agent_id": str(failing_agent.id),
                "agent_name": str(failing_agent.name),
                "step": "PREPARE",
                "error_message": UPGRADE_FAILURE_MESSAGE,
            }
        ],
        "dry_run": False,
        "upgraded_count": 1,
        "current_count": 1,
        "skipped_count": 1,
        "failed_count": 1,
        "exit_code": EXIT_CODE_ERROR,
    }


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_json_output_reports_the_agents_upgraded_before_an_error_escapes_the_run(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
    upgradable_agent_types: None,
    local_host: Host,
) -> None:
    stale_agent = create_test_agent_state(
        local_host, temp_work_dir, "escape-stale-48213", UPGRADABLE_COMMAND_AGENT_TYPE
    )
    create_test_agent_state(local_host, temp_work_dir, "escape-crashing-48213", UPGRADE_CRASHING_COMMAND_AGENT_TYPE)

    result = cli_runner.invoke(
        upgrade,
        [str(stale_agent.name), "escape-crashing-48213", "--format", "json"],
        obj=plugin_manager,
        catch_exceptions=True,
    )

    assert isinstance(result.exception, UpgradeCrashError)
    assert json.loads(result.stdout) == {
        "upgraded_agents": [
            {
                "agent_id": str(stale_agent.id),
                "agent_name": str(stale_agent.name),
                "plan": "LEAVE_STOPPED",
                "prior_state": "STOPPED",
                "changed_paths": _UNPROVISIONED_CHANGED_PATHS,
            }
        ],
        "current_agents": [],
        "skipped_agents": [],
        "failed_agents": [],
        "dry_run": False,
        "upgraded_count": 1,
        "current_count": 0,
        "skipped_count": 0,
        "failed_count": 0,
        "exit_code": EXIT_CODE_ERROR,
    }
    assert (get_agent_state_dir_path(local_host.host_dir, stale_agent.id) / _UPGRADED_SCRIPT_PATH).is_file()


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_jsonl_dry_run_emits_an_event_per_outcome_in_the_order_named_past_a_failure_and_then_the_result(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
    upgradable_agent_types: None,
    local_host: Host,
) -> None:
    current_agent, stale_agent, unsupported_agent, failing_agent = _create_agents_with_one_outcome_each(
        cli_runner, plugin_manager, local_host, temp_work_dir
    )

    result = cli_runner.invoke(
        upgrade,
        [
            str(stale_agent.name),
            str(failing_agent.name),
            str(unsupported_agent.name),
            str(current_agent.name),
            "--dry-run",
            "--format",
            "jsonl",
        ],
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == EXIT_CODE_ERROR
    assert [json.loads(line) for line in result.stdout.splitlines()] == [
        {
            "event": "agent_upgraded",
            "agent_id": str(stale_agent.id),
            "agent_name": str(stale_agent.name),
            "plan": "LEAVE_STOPPED",
            "prior_state": "STOPPED",
            "changed_paths": _UNPROVISIONED_CHANGED_PATHS,
            "dry_run": True,
        },
        {
            "event": "agent_failed",
            "agent_id": str(failing_agent.id),
            "agent_name": str(failing_agent.name),
            "step": "PREPARE",
            "error_message": UPGRADE_FAILURE_MESSAGE,
            "dry_run": True,
        },
        {
            "event": "agent_skipped",
            "agent_id": str(unsupported_agent.id),
            "agent_name": str(unsupported_agent.name),
            "kind": "UNSUPPORTED",
            "detail": f"agent type '{PLACEHOLDER_AGENT_TYPE}' has no upgrade support",
            "dry_run": True,
        },
        {
            "event": "agent_current",
            "agent_id": str(current_agent.id),
            "agent_name": str(current_agent.name),
            "dry_run": True,
        },
        {
            "event": "upgrade_result",
            "dry_run": True,
            "upgraded_count": 1,
            "current_count": 1,
            "skipped_count": 1,
            "failed_count": 1,
            "exit_code": EXIT_CODE_ERROR,
        },
    ]


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_reports_agents_in_the_order_named_and_an_agent_named_twice_once(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
    upgradable_agent_types: None,
    local_host: Host,
) -> None:
    agent_a, _agent_b, _agent_c = (
        create_test_agent_state(local_host, temp_work_dir, agent_name, UPGRADABLE_COMMAND_AGENT_TYPE)
        for agent_name in ("order-a-48213", "order-b-48213", "order-c-48213")
    )

    result = cli_runner.invoke(
        upgrade,
        ["order-c-48213", "order-a-48213", "--agent", "order-b-48213", str(agent_a.id), "--dry-run"],
        obj=plugin_manager,
        catch_exceptions=False,
    )
    stdin_result = cli_runner.invoke(
        upgrade,
        ["-", "--dry-run"],
        input="order-b-48213\norder-c-48213\norder-a-48213\norder-c-48213\n",
        obj=plugin_manager,
        catch_exceptions=False,
    )

    assert result.exit_code == EXIT_CODE_SUCCESS
    assert [line for line in result.stdout.splitlines() if not line.startswith("  ")] == [
        "would upgrade order-c-48213 (stopped)",
        "would upgrade order-a-48213 (stopped)",
        "would upgrade order-b-48213 (stopped)",
        "3 would be upgraded, 0 current, 0 would not be upgraded, 0 failed",
    ]
    assert stdin_result.exit_code == EXIT_CODE_SUCCESS
    assert [line for line in stdin_result.stdout.splitlines() if not line.startswith("  ")] == [
        "would upgrade order-b-48213 (stopped)",
        "would upgrade order-c-48213 (stopped)",
        "would upgrade order-a-48213 (stopped)",
        "3 would be upgraded, 0 current, 0 would not be upgraded, 0 failed",
    ]


def test_upgrade_leaves_an_agent_of_a_type_this_config_does_not_define_unsupported_and_says_so(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
) -> None:
    agent_id, _ = create_agent_with_events_dir(
        local_host.host_dir, "undefined-type-agent-48213", agent_type="undefined-type-48213"
    )

    result = cli_runner.invoke(upgrade, ["undefined-type-agent-48213"], obj=plugin_manager, catch_exceptions=False)
    json_result = cli_runner.invoke(
        upgrade, ["undefined-type-agent-48213", "--format", "json"], obj=plugin_manager, catch_exceptions=False
    )

    assert result.exit_code == EXIT_CODE_AGENTS_NOT_UPGRADED
    assert result.stdout == (
        "skipped undefined-type-agent-48213: agent type 'undefined-type-48213' is not defined in this config\n"
    )
    assert json.loads(json_result.stdout)["skipped_agents"] == [
        {
            "agent_id": str(agent_id),
            "agent_name": "undefined-type-agent-48213",
            "kind": "UNSUPPORTED",
            "detail": "agent type 'undefined-type-48213' is not defined in this config",
        }
    ]
