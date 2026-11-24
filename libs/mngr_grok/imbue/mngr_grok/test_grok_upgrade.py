import stat
from pathlib import Path
from typing import Final

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.testing import make_current_outcome
from imbue.mngr.api.testing import make_unsupported_until_recreated_outcome
from imbue.mngr.api.testing import make_upgraded_while_stopped_outcome
from imbue.mngr.api.testing import run_upgrade_as_cli
from imbue.mngr.api.upgrade import UPGRADE_STAGING_DIR_NAME
from imbue.mngr.hosts.host import write_shared_shell_libs
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr_grok.grok_config import GROK_HOME_RELATIVE_PATH
from imbue.mngr_grok.grok_config import LAUNCH_ENV_RELATIVE_PATH
from imbue.mngr_grok.grok_config import ROLE_PROMPT_RELATIVE_PATH
from imbue.mngr_grok.grok_config import SESSION_ID_RELATIVE_PATH
from imbue.mngr_grok.plugin import GrokAgent
from imbue.mngr_grok.plugin import GrokAgentConfig
from imbue.mngr_grok.plugin_test import make_grok_agent
from imbue.mngr_grok.resources.grok_hook import SESSION_STARTED_FILENAME

_COMMON_TRANSCRIPT_SCRIPT_NAMES: Final[tuple[str, ...]] = ("common_transcript.sh", "common_transcript_convert.py")
_HOOKS_PATH: Final[Path] = GROK_HOME_RELATIVE_PATH / "hooks"
_HOOKS_CONFIG_PATH: Final[Path] = _HOOKS_PATH / "mngr.json"
_HOOK_HANDLER_PATH: Final[Path] = _HOOKS_PATH / "grok_hook.py"
_CONFIG_TOML_PATH: Final[Path] = GROK_HOME_RELATIVE_PATH / "config.toml"

_CREATED_CONFIG: Final[GrokAgentConfig] = GrokAgentConfig(auto_dismiss_dialogs_at_startup=True)


def _create_grok_agent(
    local_provider: LocalProviderInstance, tmp_path: Path, agent_config: GrokAgentConfig
) -> GrokAgent:
    """A stopped grok agent provisioned as `mngr create` provisions one under ``agent_config``."""
    agent = make_grok_agent(local_provider, tmp_path, agent_config)
    agent.set_command(agent.assemble_command(agent.host, (), None))
    write_shared_shell_libs(agent.host, agent._get_agent_dir() / "commands")
    agent.provision(agent.host, CreateAgentOptions(agent_type=AgentTypeName("grok")), agent.mngr_ctx)
    return agent


def _as_loaded_under_config(agent: GrokAgent, agent_config: GrokAgentConfig) -> GrokAgent:
    """The agent as a `mngr upgrade` run from a directory with another grok config loads it."""
    return agent.model_copy_update(to_update(agent.field_ref().agent_config, agent_config))


def _read_texts(state_dir: Path, relative_paths: tuple[Path, ...]) -> dict[Path, str]:
    return {relative_path: (state_dir / relative_path).read_text() for relative_path in relative_paths}


@pytest.mark.rsync
@pytest.mark.parametrize("is_common_transcript_emitted", [True, False])
def test_upgrade_finds_a_just_created_grok_agent_current(
    local_provider: LocalProviderInstance, tmp_path: Path, is_common_transcript_emitted: bool
) -> None:
    agent = _create_grok_agent(
        local_provider,
        tmp_path,
        _CREATED_CONFIG.model_copy_update(
            to_update(_CREATED_CONFIG.field_ref().emit_common_transcript, is_common_transcript_emitted)
        ),
    )
    commands_dir = agent._get_agent_dir() / "commands"

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_current_outcome(agent)
    assert [name for name in _COMMON_TRANSCRIPT_SCRIPT_NAMES if (commands_dir / name).exists()] == (
        list(_COMMON_TRANSCRIPT_SCRIPT_NAMES) if is_common_transcript_emitted else []
    )
    assert not (agent._get_agent_dir() / UPGRADE_STAGING_DIR_NAME).exists()


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_rewrites_only_the_generated_files_of_a_stopped_grok_agent(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _create_grok_agent(local_provider, tmp_path, _CREATED_CONFIG)
    state_dir = agent._get_agent_dir()
    aged_paths = (
        Path("commands/grok_launch.sh"),
        Path("commands/grok_transcript_streamer.py"),
        _HOOKS_CONFIG_PATH,
        _HOOK_HANDLER_PATH,
    )
    created_files = _read_texts(state_dir, (*aged_paths, Path("commands/mngr_background_task_lib.sh")))

    # Make it look like an older mngr created it, beside files grok, the user and other plugins wrote.
    for aged_path in aged_paths:
        (state_dir / aged_path).write_text("echo old-generated-file-58214\n")
    (state_dir / "commands" / "mngr_background_task_lib.sh").unlink()
    (state_dir / _CONFIG_TOML_PATH).write_text(
        (state_dir / _CONFIG_TOML_PATH).read_text() + '\n[models]\ndefault = "grok-4.6"\n'
    )
    (state_dir / LAUNCH_ENV_RELATIVE_PATH).write_text(
        (state_dir / LAUNCH_ENV_RELATIVE_PATH).read_text() + "launch_older_setting_58214=1\n"
    )
    (state_dir / ROLE_PROMPT_RELATIVE_PATH).write_text("Reviewer role 58214\n")
    (state_dir / _HOOKS_PATH / "user.json").write_text('{"hooks": {}}\n')
    (state_dir / "commands" / "ttyd").mkdir()
    (state_dir / "commands" / "ttyd" / "agent.sh").write_text("echo other-plugin-58214\n")
    (state_dir / SESSION_STARTED_FILENAME).touch()
    kept_paths = (
        Path("data.json"),
        _CONFIG_TOML_PATH,
        LAUNCH_ENV_RELATIVE_PATH,
        ROLE_PROMPT_RELATIVE_PATH,
        SESSION_ID_RELATIVE_PATH,
        _HOOKS_PATH / "user.json",
        Path("commands/ttyd/agent.sh"),
    )
    kept_files = _read_texts(state_dir, kept_paths)

    first_outcome = run_upgrade_as_cli(agent)
    second_outcome = run_upgrade_as_cli(agent)

    assert first_outcome == make_upgraded_while_stopped_outcome(
        agent,
        (
            Path("commands/grok_launch.sh"),
            Path("commands/grok_transcript_streamer.py"),
            Path("commands/mngr_background_task_lib.sh"),
            _HOOK_HANDLER_PATH,
            _HOOKS_CONFIG_PATH,
        ),
    )
    assert second_outcome == make_current_outcome(agent)
    assert _read_texts(state_dir, tuple(created_files)) == created_files
    assert stat.S_IMODE((state_dir / _HOOK_HANDLER_PATH).stat().st_mode) == 0o755
    assert _read_texts(state_dir, kept_paths) == kept_files
    assert not (state_dir / SESSION_STARTED_FILENAME).exists()
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()
    assert agent.get_lifecycle_state() == AgentLifecycleState.STOPPED


@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.parametrize(
    ("is_created_with_common_transcript", "aged_script_names"),
    [
        pytest.param(True, _COMMON_TRANSCRIPT_SCRIPT_NAMES, id="created_with_the_common_transcript"),
        pytest.param(False, ("grok_launch.sh",), id="created_without_the_common_transcript"),
    ],
)
def test_upgrade_rewrites_the_common_transcript_scripts_only_where_the_agent_has_them(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    is_created_with_common_transcript: bool,
    aged_script_names: tuple[str, ...],
) -> None:
    created_config = _CREATED_CONFIG.model_copy_update(
        to_update(_CREATED_CONFIG.field_ref().emit_common_transcript, is_created_with_common_transcript)
    )
    running_config = _CREATED_CONFIG.model_copy_update(
        to_update(_CREATED_CONFIG.field_ref().emit_common_transcript, not is_created_with_common_transcript)
    )
    agent = _create_grok_agent(local_provider, tmp_path, created_config)
    commands_dir = agent._get_agent_dir() / "commands"
    created_scripts = {name: (commands_dir / name).read_text() for name in aged_script_names}
    for name in aged_script_names:
        (commands_dir / name).write_text("echo old-script-39581\n")
    agent_under_running_config = _as_loaded_under_config(agent, running_config)

    first_outcome = run_upgrade_as_cli(agent_under_running_config)
    second_outcome = run_upgrade_as_cli(agent_under_running_config)

    assert first_outcome == make_upgraded_while_stopped_outcome(
        agent, tuple(Path("commands") / name for name in aged_script_names)
    )
    assert second_outcome == make_current_outcome(agent)
    assert {name: (commands_dir / name).read_text() for name in aged_script_names} == created_scripts
    assert [name for name in _COMMON_TRANSCRIPT_SCRIPT_NAMES if (commands_dir / name).exists()] == (
        list(_COMMON_TRANSCRIPT_SCRIPT_NAMES) if is_created_with_common_transcript else []
    )


def test_upgrade_leaves_a_grok_agent_whose_launch_command_skips_the_launch_script_unsupported_until_recreated(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _create_grok_agent(local_provider, tmp_path, _CREATED_CONFIG)
    launch_script_path = agent._get_agent_dir() / "commands" / "grok_launch.sh"
    agent.set_command(CommandString("grok --verbose"))
    launch_script_path.write_text("echo old-launch-script-58214\n")

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_unsupported_until_recreated_outcome(
        agent, "its launch command does not run commands/grok_launch.sh"
    )
    assert launch_script_path.read_text() == "echo old-launch-script-58214\n"
