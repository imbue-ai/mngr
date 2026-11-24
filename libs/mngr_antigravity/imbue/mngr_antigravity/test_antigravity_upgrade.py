import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.testing import make_current_outcome
from imbue.mngr.api.testing import make_prepare_failed_outcome
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
from imbue.mngr_antigravity.antigravity_config import build_antigravity_hooks_config
from imbue.mngr_antigravity.antigravity_config import build_antigravity_statusline_settings
from imbue.mngr_antigravity.antigravity_config import get_antigravity_global_rules_path
from imbue.mngr_antigravity.antigravity_config import get_antigravity_hooks_config_path
from imbue.mngr_antigravity.antigravity_config import get_antigravity_oauth_token_path
from imbue.mngr_antigravity.antigravity_config import get_antigravity_onboarding_cache_path
from imbue.mngr_antigravity.antigravity_config import get_antigravity_settings_path
from imbue.mngr_antigravity.plugin import AntigravityAgent
from imbue.mngr_antigravity.plugin import AntigravityAgentConfig
from imbue.mngr_antigravity.plugin_test import make_antigravity_agent

_AGY_HOME: Final[Path] = Path("plugin", "antigravity", "home")
_HOOKS_PATH: Final[Path] = get_antigravity_hooks_config_path(_AGY_HOME)
_SETTINGS_PATH: Final[Path] = get_antigravity_settings_path(_AGY_HOME)
_ONBOARDING_PATH: Final[Path] = get_antigravity_onboarding_cache_path(_AGY_HOME)
_RULES_PATH: Final[Path] = get_antigravity_global_rules_path(_AGY_HOME)
_OAUTH_TOKEN_PATH: Final[Path] = get_antigravity_oauth_token_path(_AGY_HOME)
_COMMON_TRANSCRIPT_SCRIPT_NAMES: Final[tuple[str, ...]] = ("common_transcript.sh", "common_transcript_convert.py")

_CREATED_CONFIG: Final[AntigravityAgentConfig] = AntigravityAgentConfig(auto_dismiss_dialogs_at_startup=True)

_USER_HOOK_GROUP: Final[dict[str, Any]] = {
    "Stop": [{"type": "command", "command": "echo user-stop-hook-73915"}],
}
_STALE_MNGR_HOOK_GROUP: Final[dict[str, Any]] = {
    "PreInvocation": [{"type": "command", "command": 'bash "$MNGR_AGENT_STATE_DIR/commands/retired_hook_73915.sh"'}],
}
_USER_STATUSLINE: Final[dict[str, Any]] = {"type": "command", "command": "echo user-status-73915"}


def _create_antigravity_agent(
    local_provider: LocalProviderInstance, tmp_path: Path, agent_config: AntigravityAgentConfig
) -> AntigravityAgent:
    """A stopped antigravity agent provisioned as `mngr create` provisions one under ``agent_config``."""
    agent = make_antigravity_agent(local_provider, tmp_path, agent_config)
    agent.set_command(agent.assemble_command(agent.host, (), None))
    write_shared_shell_libs(agent.host, agent._get_agent_dir() / "commands")
    agent.provision(agent.host, CreateAgentOptions(agent_type=AgentTypeName("antigravity")), agent.mngr_ctx)
    return agent


def _with_common_transcript(is_common_transcript_emitted: bool) -> AntigravityAgentConfig:
    return _CREATED_CONFIG.model_copy_update(
        to_update(_CREATED_CONFIG.field_ref().emit_common_transcript, is_common_transcript_emitted)
    )


def _as_loaded_under_config(agent: AntigravityAgent, agent_config: AntigravityAgentConfig) -> AntigravityAgent:
    """The agent as a `mngr upgrade` run from a directory with another antigravity config loads it."""
    return agent.model_copy_update(to_update(agent.field_ref().agent_config, agent_config))


def _read_texts(state_dir: Path, relative_paths: tuple[Path, ...]) -> dict[Path, str]:
    return {relative_path: (state_dir / relative_path).read_text() for relative_path in relative_paths}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


@pytest.mark.rsync
@pytest.mark.parametrize("is_common_transcript_emitted", [True, False])
def test_upgrade_finds_a_just_created_antigravity_agent_current(
    local_provider: LocalProviderInstance, tmp_path: Path, is_common_transcript_emitted: bool
) -> None:
    agent = _create_antigravity_agent(local_provider, tmp_path, _with_common_transcript(is_common_transcript_emitted))
    state_dir = agent._get_agent_dir()
    created_config_files = {path: (state_dir / path).read_bytes() for path in (_SETTINGS_PATH, _HOOKS_PATH)}

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_current_outcome(agent)
    assert {path: (state_dir / path).read_bytes() for path in created_config_files} == created_config_files
    assert [name for name in _COMMON_TRANSCRIPT_SCRIPT_NAMES if (state_dir / "commands" / name).exists()] == (
        list(_COMMON_TRANSCRIPT_SCRIPT_NAMES) if is_common_transcript_emitted else []
    )
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_rewrites_only_mngrs_files_and_entries_of_a_stopped_antigravity_agent(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _create_antigravity_agent(local_provider, tmp_path, _CREATED_CONFIG)
    state_dir = agent._get_agent_dir()
    aged_paths = (
        Path("commands/decode_agy_transcript.py"),
        Path("commands/statusline.sh"),
        Path("commands/turn_end_hook.sh"),
    )
    created_files = _read_texts(state_dir, (*aged_paths, Path("commands/mngr_background_task_lib.sh")))
    oauth_token_target = os.readlink(state_dir / _OAUTH_TOKEN_PATH)
    live_settings = {**_read_json(state_dir / _SETTINGS_PATH), "model": "Gemini 3.8 Flash (High)"}

    # Make it look like an older mngr created it, beside what agy, the user and the agent's options wrote.
    for aged_path in aged_paths:
        (state_dir / aged_path).write_text("echo old-generated-file-73915\n")
    (state_dir / "commands" / "mngr_background_task_lib.sh").unlink()
    (state_dir / _HOOKS_PATH).write_text(
        json.dumps({"user-audit": _USER_HOOK_GROUP, "mngr": _STALE_MNGR_HOOK_GROUP}, indent=2)
    )
    (state_dir / _SETTINGS_PATH).write_text(json.dumps({**live_settings, "statusLine": _USER_STATUSLINE}, indent=2))
    (state_dir / _ONBOARDING_PATH).write_text('{"onboardingComplete": true, "enterpriseOnboardingComplete": false}')
    (state_dir / _RULES_PATH).write_text("Reviewer role 73915\n")
    (state_dir / "user_statusline_command").write_text("echo user-status-73915")
    (state_dir / "root_conversation").write_text("0b5e33a1-7391-4a5e-9c1d-73915c0ffee0")
    (state_dir / "active").touch()
    (state_dir / "antigravity_ready").touch()
    kept_paths = (
        Path("data.json"),
        _ONBOARDING_PATH,
        _RULES_PATH,
        Path("user_statusline_command"),
        Path("root_conversation"),
    )
    kept_files = _read_texts(state_dir, kept_paths)

    first_outcome = run_upgrade_as_cli(agent)
    second_outcome = run_upgrade_as_cli(agent)

    assert first_outcome == make_upgraded_while_stopped_outcome(
        agent,
        (
            Path("commands/decode_agy_transcript.py"),
            Path("commands/mngr_background_task_lib.sh"),
            Path("commands/statusline.sh"),
            Path("commands/turn_end_hook.sh"),
            _SETTINGS_PATH,
            _HOOKS_PATH,
        ),
    )
    assert second_outcome == make_current_outcome(agent)
    assert _read_texts(state_dir, tuple(created_files)) == created_files
    assert _read_json(state_dir / _HOOKS_PATH) == {"user-audit": _USER_HOOK_GROUP, **build_antigravity_hooks_config()}
    assert _read_json(state_dir / _SETTINGS_PATH) == {**live_settings, **build_antigravity_statusline_settings()}
    assert stat.S_IMODE((state_dir / "commands" / "statusline.sh").stat().st_mode) == 0o755
    assert _read_texts(state_dir, kept_paths) == kept_files
    assert os.readlink(state_dir / _OAUTH_TOKEN_PATH) == oauth_token_target
    assert not (state_dir / "active").exists()
    assert not (state_dir / "antigravity_ready").exists()
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()
    assert agent.get_lifecycle_state() == AgentLifecycleState.STOPPED


@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.parametrize(
    ("is_created_with_common_transcript", "aged_script_names"),
    [
        pytest.param(True, _COMMON_TRANSCRIPT_SCRIPT_NAMES, id="created_with_the_common_transcript"),
        pytest.param(False, ("stream_transcript.sh",), id="created_without_the_common_transcript"),
    ],
)
def test_upgrade_rewrites_the_common_transcript_scripts_only_where_the_antigravity_agent_has_them(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    is_created_with_common_transcript: bool,
    aged_script_names: tuple[str, ...],
) -> None:
    agent = _create_antigravity_agent(
        local_provider, tmp_path, _with_common_transcript(is_created_with_common_transcript)
    )
    commands_dir = agent._get_agent_dir() / "commands"
    created_scripts = {name: (commands_dir / name).read_text() for name in aged_script_names}
    for name in aged_script_names:
        (commands_dir / name).write_text("echo old-script-73915\n")
    agent_under_running_config = _as_loaded_under_config(
        agent, _with_common_transcript(not is_created_with_common_transcript)
    )

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


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_writes_a_missing_hooks_file_as_create_writes_it(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _create_antigravity_agent(local_provider, tmp_path, _CREATED_CONFIG)
    hooks_path = agent._get_agent_dir() / _HOOKS_PATH
    created_hooks_bytes = hooks_path.read_bytes()
    hooks_path.unlink()

    first_outcome = run_upgrade_as_cli(agent)
    second_outcome = run_upgrade_as_cli(agent)

    assert first_outcome == make_upgraded_while_stopped_outcome(agent, (_HOOKS_PATH,))
    assert second_outcome == make_current_outcome(agent)
    assert hooks_path.read_bytes() == created_hooks_bytes


@pytest.mark.rsync
def test_upgrade_finds_agy_config_files_another_writer_reformatted_and_added_to_current(
    local_provider: LocalProviderInstance, tmp_path: Path
) -> None:
    agent = _create_antigravity_agent(local_provider, tmp_path, _CREATED_CONFIG)
    state_dir = agent._get_agent_dir()
    reformatted_texts = {
        _SETTINGS_PATH: json.dumps(
            {**_read_json(state_dir / _SETTINGS_PATH), "model": "Gemini 3.8 Flash (High)"}, indent=2, sort_keys=True
        ),
        _HOOKS_PATH: json.dumps({"user-audit": _USER_HOOK_GROUP, **_read_json(state_dir / _HOOKS_PATH)}),
    }
    for path, text in reformatted_texts.items():
        (state_dir / path).write_text(text)

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_current_outcome(agent)
    assert _read_texts(state_dir, tuple(reformatted_texts)) == reformatted_texts


def _without_background_tasks(command: str) -> str:
    """``command`` with its supervisor start removed."""
    _, _, rest = command.partition(") & ")
    return rest


def _with_home_moved(command: str) -> str:
    """``command`` running agy under the home of a state dir at another path."""
    return command.replace("/plugin/antigravity/home", "/moved-73915/plugin/antigravity/home")


def _without_home(command: str) -> str:
    """``command`` running agy under the caller's home."""
    head, _, tail = command.partition("env HOME=")
    return f"{head}env {tail.partition(' ')[2]}"


@pytest.mark.parametrize(
    "stored_command",
    [
        pytest.param(_without_background_tasks, id="without_the_background_tasks"),
        pytest.param(_with_home_moved, id="with_another_home"),
        pytest.param(_without_home, id="without_a_home"),
    ],
)
def test_upgrade_leaves_an_antigravity_agent_whose_launch_command_cannot_load_the_upgraded_files_unsupported(
    local_provider: LocalProviderInstance, tmp_path: Path, stored_command: Callable[[str], str]
) -> None:
    agent = _create_antigravity_agent(local_provider, tmp_path, _CREATED_CONFIG)
    statusline_path = agent._get_agent_dir() / "commands" / "statusline.sh"
    agent.set_command(CommandString(stored_command(str(agent.assemble_command(agent.host, (), None)))))
    statusline_path.write_text("echo old-statusline-73915\n")

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_unsupported_until_recreated_outcome(
        agent, "its launch command does not start mngr's background tasks and run agy under the agent's own home"
    )
    assert statusline_path.read_text() == "echo old-statusline-73915\n"


@pytest.mark.parametrize(
    ("malformed_path", "malformed_text", "expected_problem"),
    [
        pytest.param(
            _HOOKS_PATH,
            "{not json",
            "is not valid JSON: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
            id="hooks_not_json",
        ),
        pytest.param(_HOOKS_PATH, "[]", "does not hold a JSON object", id="hooks_list"),
        pytest.param(
            _SETTINGS_PATH,
            "{not json",
            "is not valid JSON: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
            id="settings_not_json",
        ),
        pytest.param(_SETTINGS_PATH, '"statusLine"', "does not hold a JSON object", id="settings_string"),
    ],
)
def test_upgrade_reports_a_malformed_antigravity_config_file_as_a_prepare_failure(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    malformed_path: Path,
    malformed_text: str,
    expected_problem: str,
) -> None:
    agent = _create_antigravity_agent(local_provider, tmp_path, _CREATED_CONFIG)
    state_dir = agent._get_agent_dir()
    (state_dir / malformed_path).write_text(malformed_text)

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_prepare_failed_outcome(agent, f"{malformed_path} {expected_problem}")
    assert (state_dir / malformed_path).read_text() == malformed_text
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()
