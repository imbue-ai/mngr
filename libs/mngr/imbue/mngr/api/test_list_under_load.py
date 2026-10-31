"""list_agents on a host whose tmux and ps are slow, the way a loaded host's are."""

import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.list import list_agents
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.utils.testing import cleanup_tmux_session
from imbue.mngr.utils.testing import create_test_agent_via_cli
from imbue.mngr.utils.testing import make_mngr_ctx
from imbue.mngr.utils.testing import shadow_program_on_path


@contextmanager
def _live_agents(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    agent_count: int,
    command: str,
) -> Iterator[list[AgentName]]:
    stamp = int(time.time())
    names = [AgentName(f"load-{agent_idx}-{stamp}") for agent_idx in range(agent_count)]
    created_sessions: list[str] = []
    try:
        for name in names:
            created_sessions.append(f"{mngr_test_prefix}{name}")
            create_test_agent_via_cli(cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, str(name), command)
        yield names
    finally:
        for session in created_sessions:
            cleanup_tmux_session(session)


def _list_states(mngr_ctx: MngrContext) -> dict[AgentName, AgentLifecycleState]:
    return {agent.name: agent.state for agent in list_agents(mngr_ctx=mngr_ctx, is_streaming=False).agents}


@pytest.mark.tmux
def test_slow_tmux_is_read_once_per_listing_so_live_agents_stay_waiting(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _live_agents(cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, 8, "sleep 847302") as names:
        assert {_list_states(temp_mngr_ctx)[name] for name in names} == {AgentLifecycleState.WAITING}

        # Each tmux call takes 1.5s, so per-agent tmux probes would spend the host's 20s read
        # budget part way through these 8 agents and read the rest STOPPED.
        tmux_call_log = tmp_path / "tmux_calls.log"
        with monkeypatch.context() as patch:
            shadow_program_on_path(
                patch, tmp_path / "shims", "tmux", f'echo "$*" >> {tmux_call_log}\nsleep 1.5\nexec "$REAL" "$@"'
            )
            states = _list_states(temp_mngr_ctx)

        assert {states[name] for name in names} == {AgentLifecycleState.WAITING}
        assert len(tmux_call_log.read_text().splitlines()) == 1


@contextmanager
def _states_listed_while_slow(
    slow_program: str,
    command: str,
    live_agent_count: int,
    stopped_agent_count: int,
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_config: MngrConfig,
    temp_profile_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    shim_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[list[AgentName], dict[AgentName, AgentLifecycleState]]]:
    """List live agents, the last ``stopped_agent_count`` of them stopped first, while ``slow_program`` outlasts a 1s read budget.

    Yields the agents' names and their listed states while the live agents still run.
    """
    agent_count = live_agent_count + stopped_agent_count
    tight_budget_config = temp_config.model_copy_update(
        to_update(temp_config.field_ref().host_detail_read_timeout_seconds, 1.0),
    )
    with ConcurrencyGroup(name="tight-read-budget") as concurrency_group:
        mngr_ctx = make_mngr_ctx(
            tight_budget_config, plugin_manager, temp_profile_dir, concurrency_group=concurrency_group
        )
        with _live_agents(cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_count, command) as names:
            assert {_list_states(mngr_ctx)[name] for name in names} == {AgentLifecycleState.WAITING}
            for name in names[live_agent_count:]:
                cleanup_tmux_session(f"{mngr_test_prefix}{name}")

            with monkeypatch.context() as patch:
                shadow_program_on_path(patch, shim_dir, slow_program, 'sleep 3\nexec "$REAL" "$@"')
                states = _list_states(mngr_ctx)
            yield names, states


@pytest.mark.tmux
def test_live_agents_read_unknown_while_tmux_outlasts_the_read_budget(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_config: MngrConfig,
    temp_profile_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _states_listed_while_slow(
        slow_program="tmux",
        command="sleep 847303",
        live_agent_count=2,
        stopped_agent_count=0,
        cli_runner=cli_runner,
        temp_work_dir=temp_work_dir,
        temp_config=temp_config,
        temp_profile_dir=temp_profile_dir,
        mngr_test_prefix=mngr_test_prefix,
        plugin_manager=plugin_manager,
        shim_dir=tmp_path / "shims",
        monkeypatch=monkeypatch,
    ) as (names, states):
        assert [states[name] for name in names] == [AgentLifecycleState.UNKNOWN, AgentLifecycleState.UNKNOWN]


@pytest.mark.tmux
def test_tmux_settles_what_it_shows_while_ps_outlasts_the_read_budget(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_config: MngrConfig,
    temp_profile_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live agent reads WAITING where tmux shows its process in the pane's foreground, else UNKNOWN; a gone session STOPPED.

    Where the panes have no controlling terminal (e.g. a gVisor sandbox), tmux cannot see the
    foreground and reports the pane's shell, so only the process table could show the agent.
    """
    with _states_listed_while_slow(
        slow_program="ps",
        command="sleep 847305",
        live_agent_count=2,
        stopped_agent_count=1,
        cli_runner=cli_runner,
        temp_work_dir=temp_work_dir,
        temp_config=temp_config,
        temp_profile_dir=temp_profile_dir,
        mngr_test_prefix=mngr_test_prefix,
        plugin_manager=plugin_manager,
        shim_dir=tmp_path / "shims",
        monkeypatch=monkeypatch,
    ) as (names, states):
        expected_live_states = [
            AgentLifecycleState.WAITING
            if _pane_current_command(f"{mngr_test_prefix}{name}", temp_config.tmux.primary_window_name) == "sleep"
            else AgentLifecycleState.UNKNOWN
            for name in names[:2]
        ]
        assert [states[name] for name in names] == [*expected_live_states, AgentLifecycleState.STOPPED]


def _pane_current_command(session_name: str, window_name: str) -> str:
    return subprocess.run(
        ["tmux", "display-message", "-p", "-t", f"={session_name}:{window_name}", "#{pane_current_command}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()


@pytest.mark.tmux
def test_live_agents_read_unknown_when_tmux_fails_with_a_server_running(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only "no server running" means there are no sessions; any other tmux failure says nothing about them."""
    with _live_agents(cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, 2, "sleep 847304") as names:
        with monkeypatch.context() as patch:
            shadow_program_on_path(
                patch, tmp_path / "shims", "tmux", 'echo "lost server: connection reset by peer" >&2\nexit 1'
            )
            states = _list_states(temp_mngr_ctx)

    assert {states[name] for name in names} == {AgentLifecycleState.UNKNOWN}
