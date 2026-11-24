from pathlib import Path
from typing import Final

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.testing import make_current_outcome
from imbue.mngr.api.testing import make_unsupported_until_recreated_outcome
from imbue.mngr.api.testing import make_upgraded_while_stopped_outcome
from imbue.mngr.api.testing import run_upgrade_as_cli
from imbue.mngr.api.upgrade import UPGRADE_STAGING_DIR_NAME
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import write_shared_shell_libs
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import SystemPromptText
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr_pi_coding.plugin import PiCodingAgent
from imbue.mngr_pi_coding.plugin import PiCodingAgentConfig
from imbue.mngr_pi_coding.testing import make_local_pi_agent

_EXTENSION_PATH: Final[Path] = Path("commands/mngr_pi_lifecycle.ts")

_CREATED_CONFIG: Final[PiCodingAgentConfig] = PiCodingAgentConfig(
    check_installation=False,
    auto_dismiss_dialogs_at_startup=True,
    append_system_prompt=(SystemPromptText("Answer tersely 51734."),),
)

_STALE_EXTENSION: Final[str] = "export default function () {} // an older mngr's extension 51734\n"


def _create_pi_agent(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    agent_config: PiCodingAgentConfig,
    agent_args: tuple[str, ...],
) -> PiCodingAgent:
    """A stopped pi agent provisioned as `mngr create` provisions one under ``agent_config`` and ``agent_args``."""
    agent = make_local_pi_agent(local_provider, tmp_path, agent_config)
    init_git_repo(agent.work_dir, initial_commit=False)
    agent.set_command(agent.assemble_command(agent.host, agent_args, None))
    write_shared_shell_libs(agent.host, agent._get_agent_dir() / "commands")
    agent.provision(
        host=agent.host, options=CreateAgentOptions(agent_type=AgentTypeName("pi-coding")), mngr_ctx=temp_mngr_ctx
    )
    return agent


def _read_files_under(state_dir: Path) -> dict[Path, bytes]:
    """The bytes of every regular file under ``state_dir``, by path relative to it."""
    return {
        path.relative_to(state_dir): path.read_bytes()
        for path in sorted(state_dir.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


@pytest.mark.rsync
def test_upgrade_finds_a_just_created_pi_agent_current(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    agent = _create_pi_agent(local_provider, tmp_path, temp_mngr_ctx, _CREATED_CONFIG, ())
    files_before = _read_files_under(agent._get_agent_dir())

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_current_outcome(agent)
    assert _read_files_under(agent._get_agent_dir()) == files_before


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_rewrites_only_the_lifecycle_extension_of_a_stopped_pi_agent(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    agent = _create_pi_agent(local_provider, tmp_path, temp_mngr_ctx, _CREATED_CONFIG, ())
    state_dir = agent._get_agent_dir()
    extension_path = state_dir / _EXTENSION_PATH
    created_extension = extension_path.read_text()

    # Make it look like an older mngr created it, beside files that other plugins and pi itself wrote.
    extension_path.write_text(_STALE_EXTENSION)
    (state_dir / "commands" / "mngr_log.sh").write_text("echo old-shared-lib-51734\n")
    (state_dir / "pi_emit_usage").write_text("1")
    (state_dir / "env").write_text("MNGR_PI_EMIT_COMMON_TRANSCRIPT=0\n")
    (state_dir / "pi_session_file").write_text("/sessions/old-51734.jsonl")
    (state_dir / "pi_session_started").write_text("1")
    files_before = _read_files_under(state_dir)

    first_outcome = run_upgrade_as_cli(agent)
    second_outcome = run_upgrade_as_cli(agent)

    assert first_outcome == make_upgraded_while_stopped_outcome(agent, (_EXTENSION_PATH,))
    assert second_outcome == make_current_outcome(agent)
    assert extension_path.read_text() == created_extension
    assert not (state_dir / "pi_session_started").exists()
    unchanged_files = {
        path: content
        for path, content in files_before.items()
        if path not in (_EXTENSION_PATH, Path("pi_session_started"))
    }
    assert _read_files_under(state_dir) == {**unchanged_files, _EXTENSION_PATH: created_extension.encode()}
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()
    assert agent.get_lifecycle_state() == AgentLifecycleState.STOPPED


@pytest.mark.parametrize(
    ("is_created_resuming", "created_agent_args", "stored_command", "expected_harness_detail"),
    [
        pytest.param(
            True,
            (),
            CommandString("pi --provider anthropic"),
            "its launch command does not load commands/mngr_pi_lifecycle.ts",
            id="launch_command_without_the_extension",
        ),
        pytest.param(
            False,
            (),
            None,
            "its launch command does not resume its pi session, so a restart would lose its conversation; "
            "first turn resume_session on for its agent type",
            id="created_without_resume",
        ),
        pytest.param(
            True,
            ("--no-session",),
            None,
            "its launch command passes --no-session, so a restart would lose its conversation; "
            "first drop --no-session from its arguments",
            id="created_with_no_session",
        ),
    ],
)
def test_upgrade_leaves_a_pi_agent_whose_restart_would_not_load_the_extension_or_resume_it_unsupported(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    is_created_resuming: bool,
    created_agent_args: tuple[str, ...],
    # The launch command to store in place of the one create stored, if any.
    stored_command: CommandString | None,
    expected_harness_detail: str,
) -> None:
    created_config = _CREATED_CONFIG.model_copy_update(
        to_update(_CREATED_CONFIG.field_ref().resume_session, is_created_resuming)
    )
    agent = _create_pi_agent(local_provider, tmp_path, temp_mngr_ctx, created_config, created_agent_args)
    if stored_command is not None:
        agent.set_command(stored_command)
    extension_path = agent._get_agent_dir() / _EXTENSION_PATH
    extension_path.write_text(_STALE_EXTENSION)

    outcome = run_upgrade_as_cli(agent)

    assert outcome == make_unsupported_until_recreated_outcome(agent, expected_harness_detail)
    assert extension_path.read_text() == _STALE_EXTENSION
