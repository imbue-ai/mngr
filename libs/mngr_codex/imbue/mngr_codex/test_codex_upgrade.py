import json
from pathlib import Path
from typing import Any
from typing import Final

import pytest

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
from imbue.mngr_codex.codex_config import build_codex_hooks_config
from imbue.mngr_codex.codex_config import get_codex_config_path
from imbue.mngr_codex.codex_config import get_codex_home
from imbue.mngr_codex.codex_config import get_codex_hooks_path
from imbue.mngr_codex.codex_config import serialize_codex_hooks
from imbue.mngr_codex.plugin import CodexAgent
from imbue.mngr_codex.plugin import CodexAgentConfig
from imbue.mngr_codex.plugin_test import HOOKS_BEFORE_THE_TURN_HOOK
from imbue.mngr_codex.plugin_test import command_stored_before_the_hook_trust_step
from imbue.mngr_codex.plugin_test import make_codex_agent

_HOOKS_PATH: Final[Path] = get_codex_hooks_path(get_codex_home(Path()))
_CONFIG_TOML_PATH: Final[Path] = get_codex_config_path(get_codex_home(Path()))
_COMMON_TRANSCRIPT_SCRIPT_PATHS: Final[tuple[Path, ...]] = (
    Path("commands/common_transcript.sh"),
    Path("commands/common_transcript_convert.py"),
)

_USER_HOOK_GROUP: Final[dict[str, Any]] = {
    "hooks": [{"type": "command", "command": 'bash "$MNGR_AGENT_WORK_DIR/scripts/guard-60417.sh"'}]
}
# The trust codex records in config.toml for a hook picked on its review.
_CODEX_TRUST_STATE: Final[str] = (
    '\n[hooks.state."/state/plugin/codex/home/hooks.json:user_prompt_submit:0:0"]\n'
    'trusted_hash = "sha256:39b3a1ca8412f3e8c58481f7368efc8f9ef09346676c2840aec4832d4c97cfb7"\n'
)


def _create_codex_agent(
    local_provider: LocalProviderInstance, tmp_path: Path, is_common_transcript_emitted: bool = True
) -> CodexAgent:
    """A stopped codex agent provisioned as `mngr create` provisions one."""
    agent = make_codex_agent(
        CodexAgent,
        local_provider,
        tmp_path,
        CodexAgentConfig(model="gpt-6.1-sol", emit_common_transcript=is_common_transcript_emitted),
        is_auto_approve=True,
    )
    agent.set_command(agent.assemble_command(agent.host, (), None))
    write_shared_shell_libs(agent.host, agent._get_agent_dir() / "commands")
    agent.provision(agent.host, CreateAgentOptions(agent_type=AgentTypeName("codex")), agent.mngr_ctx)
    return agent


def _read_texts(state_dir: Path, relative_paths: tuple[Path, ...]) -> dict[Path, str]:
    return {relative_path: (state_dir / relative_path).read_text() for relative_path in relative_paths}


@pytest.mark.rsync
@pytest.mark.parametrize("is_common_transcript_emitted", [True, False])
def test_upgrade_finds_a_just_created_codex_agent_current(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    isolated_codex_home: Path,
    is_common_transcript_emitted: bool,
) -> None:
    agent = _create_codex_agent(local_provider, tmp_path, is_common_transcript_emitted)
    state_dir = agent._get_agent_dir()
    created_hooks_bytes = (state_dir / _HOOKS_PATH).read_bytes()

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_current_outcome(agent)
    assert (state_dir / _HOOKS_PATH).read_bytes() == created_hooks_bytes
    assert [path for path in _COMMON_TRANSCRIPT_SCRIPT_PATHS if (state_dir / path).is_file()] == (
        list(_COMMON_TRANSCRIPT_SCRIPT_PATHS) if is_common_transcript_emitted else []
    )
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_rewrites_only_the_generated_files_of_a_stopped_codex_agent(
    local_provider: LocalProviderInstance, tmp_path: Path, isolated_codex_home: Path
) -> None:
    agent = _create_codex_agent(local_provider, tmp_path)
    state_dir = agent._get_agent_dir()
    aged_paths = (Path("commands/codex_background_tasks.sh"), Path("commands/stream_transcript.sh"))
    removed_paths = (Path("commands/codex_turn_hook.sh"), Path("commands/mngr_background_task_lib.sh"))
    created_files = _read_texts(state_dir, (*aged_paths, *removed_paths))

    # Make it look like an older mngr created it, beside files codex, the user and other plugins wrote.
    for aged_path in aged_paths:
        (state_dir / aged_path).write_text("echo old-generated-file-60417\n")
    for removed_path in removed_paths:
        (state_dir / removed_path).unlink()
    old_hooks = {"hooks": {"SessionStart": [_USER_HOOK_GROUP], **HOOKS_BEFORE_THE_TURN_HOOK["hooks"]}}
    (state_dir / _HOOKS_PATH).write_text(serialize_codex_hooks(old_hooks))
    (state_dir / _CONFIG_TOML_PATH).write_text((state_dir / _CONFIG_TOML_PATH).read_text() + _CODEX_TRUST_STATE)
    (state_dir / "commands" / "codex_usage.sh").write_text("echo other-plugin-60417\n")
    (state_dir / "codex_root_session").write_text("01a1f892-2d59-7d63-8854-dfc83cc8a876")
    kept_paths = (Path("data.json"), _CONFIG_TOML_PATH, Path("commands/codex_usage.sh"), Path("codex_root_session"))
    kept_files = _read_texts(state_dir, kept_paths)

    first_outcome = run_upgrade_as_cli(agent)
    second_outcome = run_upgrade_as_cli(agent)

    assert first_outcome == make_upgraded_while_stopped_outcome(
        agent,
        (
            Path("commands/codex_background_tasks.sh"),
            Path("commands/codex_turn_hook.sh"),
            Path("commands/mngr_background_task_lib.sh"),
            Path("commands/stream_transcript.sh"),
            _HOOKS_PATH,
        ),
    )
    assert second_outcome == make_current_outcome(agent)
    assert _read_texts(state_dir, (*aged_paths, *removed_paths)) == created_files
    assert json.loads((state_dir / _HOOKS_PATH).read_text()) == {
        "hooks": {"SessionStart": [_USER_HOOK_GROUP], **build_codex_hooks_config()["hooks"]}
    }
    assert _read_texts(state_dir, kept_paths) == kept_files
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()
    assert agent.get_lifecycle_state() == AgentLifecycleState.STOPPED


@pytest.mark.rsync
def test_upgrade_finds_a_hooks_file_another_writer_reformatted_and_added_to_current(
    local_provider: LocalProviderInstance, tmp_path: Path, isolated_codex_home: Path
) -> None:
    agent = _create_codex_agent(local_provider, tmp_path)
    hooks_path = agent._get_agent_dir() / _HOOKS_PATH
    created_hooks = json.loads(hooks_path.read_text())
    reformatted_hooks_text = json.dumps(
        {"hooks": {"SessionStart": [_USER_HOOK_GROUP], **created_hooks["hooks"]}, "description": "kept-60417"}
    )
    hooks_path.write_text(reformatted_hooks_text)

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_current_outcome(agent)
    assert hooks_path.read_text() == reformatted_hooks_text


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_writes_a_missing_hooks_file_as_create_writes_it(
    local_provider: LocalProviderInstance, tmp_path: Path, isolated_codex_home: Path
) -> None:
    agent = _create_codex_agent(local_provider, tmp_path)
    hooks_path = agent._get_agent_dir() / _HOOKS_PATH
    created_hooks_bytes = hooks_path.read_bytes()
    hooks_path.unlink()

    first_outcome = run_upgrade_as_cli(agent)
    second_outcome = run_upgrade_as_cli(agent)

    assert first_outcome == make_upgraded_while_stopped_outcome(agent, (_HOOKS_PATH,))
    assert second_outcome == make_current_outcome(agent)
    assert hooks_path.read_bytes() == created_hooks_bytes


def test_upgrade_reports_a_malformed_hooks_file_as_a_prepare_failure(
    local_provider: LocalProviderInstance, tmp_path: Path, isolated_codex_home: Path
) -> None:
    agent = _create_codex_agent(local_provider, tmp_path)
    state_dir = agent._get_agent_dir()
    (state_dir / _HOOKS_PATH).write_text('{"hooks": []}')

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_prepare_failed_outcome(
        agent, f"{_HOOKS_PATH} has a `hooks` value that does not map each event to a list of hook groups"
    )
    assert (state_dir / _HOOKS_PATH).read_text() == '{"hooks": []}'
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()


def test_upgrade_leaves_a_codex_agent_whose_launch_command_predates_the_hook_trust_step_unsupported(
    local_provider: LocalProviderInstance, tmp_path: Path, isolated_codex_home: Path
) -> None:
    agent = _create_codex_agent(local_provider, tmp_path)
    state_dir = agent._get_agent_dir()
    agent.set_command(CommandString(command_stored_before_the_hook_trust_step(agent)))
    supervisor_path = state_dir / "commands" / "codex_background_tasks.sh"
    supervisor_path.write_text("echo old-supervisor-60417\n")
    (state_dir / _HOOKS_PATH).write_text(serialize_codex_hooks(HOOKS_BEFORE_THE_TURN_HOOK))

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_unsupported_until_recreated_outcome(
        agent,
        "its launch command does not start mngr's background tasks, codex's app-server with hooks on at the "
        "socket this mngr uses, and the step that trusts codex's hooks before its TUI",
    )
    assert supervisor_path.read_text() == "echo old-supervisor-60417\n"
    assert json.loads((state_dir / _HOOKS_PATH).read_text()) == HOOKS_BEFORE_THE_TURN_HOOK
