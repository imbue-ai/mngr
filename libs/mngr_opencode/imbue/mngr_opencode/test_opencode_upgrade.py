import shlex
import shutil
import stat
from pathlib import Path
from typing import Final

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.testing import make_current_outcome
from imbue.mngr.api.testing import make_unsupported_until_recreated_outcome
from imbue.mngr.api.testing import make_upgraded_while_stopped_outcome
from imbue.mngr.api.testing import run_upgrade_as_cli
from imbue.mngr.api.upgrade import AgentUpgraded
from imbue.mngr.api.upgrade import UPGRADE_STAGING_DIR_NAME
from imbue.mngr.api.upgrade import UpgradePlan
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import SystemPromptText
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import read_tmux_pane_pid
from imbue.mngr.utils.testing import tmux_session_cleanup
from imbue.mngr.utils.testing import write_executable_script
from imbue.mngr_opencode.opencode_config import READY_SENTINEL_FILENAME
from imbue.mngr_opencode.opencode_config import ROOT_SESSION_FILENAME
from imbue.mngr_opencode.opencode_config import get_opencode_agents_md_path
from imbue.mngr_opencode.opencode_config import get_opencode_config_dir
from imbue.mngr_opencode.opencode_config import get_opencode_config_file_path
from imbue.mngr_opencode.plugin import OpenCodeAgent
from imbue.mngr_opencode.plugin import OpenCodeAgentConfig
from imbue.mngr_opencode.testing import make_opencode_agent

_LAUNCH_SCRIPT_PATH: Final[Path] = Path("commands/opencode_launch.sh")
_PLUGIN_PATH: Final[Path] = Path("plugin/opencode/config/plugin/mngr_opencode_plugin.ts")

_CREATED_CONFIG: Final[OpenCodeAgentConfig] = OpenCodeAgentConfig(
    append_system_prompt=(SystemPromptText("Review code 51834."),),
    config_overrides={"model": "opencode/created-model-51834"},
)
_RUNNING_CONFIG: Final[OpenCodeAgentConfig] = OpenCodeAgentConfig(
    emit_common_transcript=False,
    auto_allow_permissions=True,
    append_system_prompt=(SystemPromptText("Write docs 51834."),),
    config_overrides={"model": "opencode/running-model-51834"},
)

# ``exec -a`` sets the name macOS ps reports; Linux ps reports the name of the file run.
_STANDIN_OPENCODE_SCRIPT_TEMPLATE: Final[str] = """#!/usr/bin/env bash
if [ "$1" = serve ]; then
    sleep 1
    echo "listening on http://127.0.0.1:47215"
fi
exec -a opencode {sleep_named_opencode} 947215
"""


def _create_opencode_agent(
    local_provider: LocalProviderInstance, tmp_path: Path, agent_config: OpenCodeAgentConfig
) -> OpenCodeAgent:
    """A stopped opencode agent provisioned as `mngr create` provisions one under ``agent_config``."""
    agent = make_opencode_agent(local_provider, tmp_path, agent_config)
    agent._write_data(
        {
            "id": str(agent.id),
            "name": str(agent.name),
            "type": str(agent.agent_type),
            "work_dir": str(agent.work_dir),
            "create_time": agent.create_time.isoformat(),
            "command": str(agent.assemble_command(agent.host, (), None)),
        }
    )
    agent.host.provision_agent(agent, CreateAgentOptions(agent_type=AgentTypeName("opencode")), agent.mngr_ctx)
    return agent


def _write_standin_opencode(tmp_path: Path) -> CommandString:
    """An opencode command whose server reports a port a second after it starts, and whose processes ps names opencode."""
    sleep_path = shutil.which("sleep")
    assert sleep_path is not None
    standin_dir = tmp_path / "standin"
    standin_dir.mkdir()
    sleep_named_opencode = standin_dir / "opencode"
    sleep_named_opencode.symlink_to(sleep_path)
    standin_path = standin_dir / "opencode_standin"
    write_executable_script(
        standin_path,
        _STANDIN_OPENCODE_SCRIPT_TEMPLATE.format(sleep_named_opencode=shlex.quote(str(sleep_named_opencode))),
    )
    return CommandString(str(standin_path))


def _read_texts(state_dir: Path, relative_paths: tuple[Path, ...]) -> dict[Path, str]:
    return {relative_path: (state_dir / relative_path).read_text() for relative_path in relative_paths}


def _as_loaded_under_config(agent: OpenCodeAgent, agent_config: OpenCodeAgentConfig) -> OpenCodeAgent:
    """The agent as a `mngr upgrade` run from a directory with another opencode config loads it."""
    return agent.model_copy_update(to_update(agent.field_ref().agent_config, agent_config))


@pytest.mark.rsync
@pytest.mark.parametrize(
    "agent_config",
    [pytest.param(OpenCodeAgentConfig(), id="default"), pytest.param(_CREATED_CONFIG, id="role_and_overrides")],
)
def test_upgrade_finds_a_just_created_opencode_agent_current(
    local_provider: LocalProviderInstance, tmp_path: Path, agent_config: OpenCodeAgentConfig
) -> None:
    agent = _create_opencode_agent(local_provider, tmp_path, agent_config)

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_current_outcome(agent)


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_rewrites_only_the_launch_script_and_plugin_of_a_stopped_opencode_agent(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _create_opencode_agent(local_provider, tmp_path, _CREATED_CONFIG)
    state_dir = agent._get_agent_dir()
    launch_script_path = state_dir / _LAUNCH_SCRIPT_PATH
    plugin_path = state_dir / _PLUGIN_PATH
    created_launch_script = launch_script_path.read_text()
    created_plugin = plugin_path.read_text()
    config_dir = get_opencode_config_dir(state_dir)
    opencode_json_path = get_opencode_config_file_path(config_dir)
    agents_md_path = get_opencode_agents_md_path(config_dir)

    # Make it look like an older mngr created it, beside files the user and other plugins wrote.
    launch_script_path.write_text("echo old-launch-script-51834\n")
    launch_script_path.chmod(0o644)
    plugin_path.write_text("// old lifecycle plugin 51834\n")
    plugin_path.chmod(0o600)
    opencode_json_path.write_text(opencode_json_path.read_text().replace("{", '{"theme": "user-theme-51834",', 1))
    agents_md_path.write_text(agents_md_path.read_text() + "\nUser rule 51834.\n")
    (plugin_path.parent / "user_plugin_51834.ts").write_text("// user plugin 51834\n")
    (state_dir / READY_SENTINEL_FILENAME).touch()
    kept_paths = (
        Path("data.json"),
        opencode_json_path.relative_to(state_dir),
        agents_md_path.relative_to(state_dir),
        _PLUGIN_PATH.parent / "user_plugin_51834.ts",
    )
    kept_files = _read_texts(state_dir, kept_paths)
    agent_under_running_config = _as_loaded_under_config(agent, _RUNNING_CONFIG)

    first_outcome = run_upgrade_as_cli(agent_under_running_config)
    second_outcome = run_upgrade_as_cli(agent_under_running_config)

    assert first_outcome == make_upgraded_while_stopped_outcome(agent, (_LAUNCH_SCRIPT_PATH, _PLUGIN_PATH))
    assert second_outcome == make_current_outcome(agent)
    assert launch_script_path.read_text() == created_launch_script
    assert stat.S_IMODE(launch_script_path.stat().st_mode) == 0o755
    assert plugin_path.read_text() == created_plugin
    assert stat.S_IMODE(plugin_path.stat().st_mode) == 0o644
    assert _read_texts(state_dir, kept_paths) == kept_files
    assert not (state_dir / READY_SENTINEL_FILENAME).exists()
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()
    assert agent.get_lifecycle_state() == AgentLifecycleState.STOPPED


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_restarts_an_idle_opencode_agent_through_its_launch_script_and_waits_for_its_fresh_sentinel(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _create_opencode_agent(
        local_provider, tmp_path, OpenCodeAgentConfig(command=_write_standin_opencode(tmp_path))
    )
    state_dir = agent._get_agent_dir()
    sentinel_path = state_dir / READY_SENTINEL_FILENAME
    plugin_path = state_dir / _PLUGIN_PATH
    created_plugin = plugin_path.read_text()
    (state_dir / ROOT_SESSION_FILENAME).write_text("ses_51834")

    with tmux_session_cleanup(agent.session_name):
        agent.host.start_agents([agent.id])
        wait_for(
            lambda: sentinel_path.exists() and agent.get_lifecycle_state() == AgentLifecycleState.WAITING,
            timeout=15.0,
            error_message="expected the started opencode agent to signal readiness and read WAITING",
        )
        pane_pid_before = read_tmux_pane_pid(agent.session_name)
        plugin_path.write_text("// old lifecycle plugin 51834\n")

        outcome = run_upgrade_as_cli(agent)

        assert isinstance(outcome, AgentUpgraded)
        assert (outcome.plan, outcome.prior_state, outcome.changed_paths) == (
            UpgradePlan.RESTART_IDLE,
            AgentLifecycleState.WAITING,
            (_PLUGIN_PATH,),
        )
        assert plugin_path.read_text() == created_plugin
        assert read_tmux_pane_pid(agent.session_name) != pane_pid_before
        assert sentinel_path.exists()
        assert agent.get_lifecycle_state() == AgentLifecycleState.WAITING


def test_upgrade_leaves_an_opencode_agent_whose_launch_command_skips_the_launch_script_unsupported_until_recreated(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _create_opencode_agent(local_provider, tmp_path, OpenCodeAgentConfig())
    launch_script_path = agent._get_agent_dir() / _LAUNCH_SCRIPT_PATH
    agent.set_command(CommandString("opencode --agent plan"))
    launch_script_path.write_text("echo old-launch-script-51834\n")

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_unsupported_until_recreated_outcome(
        agent, "its launch command does not run commands/opencode_launch.sh"
    )
    assert launch_script_path.read_text() == "echo old-launch-script-51834\n"
