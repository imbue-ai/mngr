import gc
import shlex
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Final

import pluggy
import pytest
from click.testing import CliRunner
from pydantic import Field

from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.conftest import PinnedLookupHarness
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.testing import InProcessSshHost
from imbue.mngr.api.testing import InProcessSshHostT
from imbue.mngr.api.testing import READY_MARKER_NAME
from imbue.mngr.api.testing import UPGRADABLE_COMMAND_AGENT_TYPE
from imbue.mngr.api.testing import UPGRADED_SCRIPT_NAME
from imbue.mngr.api.testing import UpgradableCommandAgent
from imbue.mngr.api.testing import make_current_outcome
from imbue.mngr.api.testing import make_in_process_ssh_host
from imbue.mngr.api.testing import make_skipped_outcome
from imbue.mngr.api.testing import make_upgradable_command_agent
from imbue.mngr.api.testing import make_upgrade_outcome_host
from imbue.mngr.api.testing import make_upgraded_while_stopped_outcome
from imbue.mngr.api.testing import upgrade_agent
from imbue.mngr.api.testing import write_command_agent_record
from imbue.mngr.api.upgrade import AgentUpgradeFailed
from imbue.mngr.api.upgrade import AgentUpgradeOutcome
from imbue.mngr.api.upgrade import AgentUpgradeSkipped
from imbue.mngr.api.upgrade import AgentUpgraded
from imbue.mngr.api.upgrade import ConnectionLostAtUpgradeStepError
from imbue.mngr.api.upgrade import UPGRADE_STAGING_DIR_NAME
from imbue.mngr.api.upgrade import UpgradeFailedStep
from imbue.mngr.api.upgrade import UpgradePlan
from imbue.mngr.api.upgrade import UpgradeSkip
from imbue.mngr.api.upgrade import UpgradeSkipKind
from imbue.mngr.api.upgrade import upgrade_agents
from imbue.mngr.cli.upgrade import format_upgrade_outcome
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import BinaryNotInstalledError
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import HostError
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.host import SHARED_SHELL_LIB_NAMES
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import UpgradeSkipDetail
from imbue.mngr.utils.deps import SSH
from imbue.mngr.utils.testing import ExecOnlyParamikoServer
from imbue.mngr.utils.testing import create_test_agent_via_cli
from imbue.mngr.utils.testing import read_tmux_pane_pid
from imbue.mngr.utils.testing import tmux_session_cleanup
from imbue.mngr.utils.testing import tmux_session_exists

_OLD_SCRIPT: Final[str] = "echo old-remote-script-59124\n"
_NEW_SCRIPT: Final[str] = "echo new-remote-script-59124\n"

_LOCK_COMMAND_EXITED_MESSAGE: Final[str] = (
    "Remote host lock command exited before acquiring the lock (is flock installed?)"
)

_CONNECTION_CLOSED_MESSAGE: Final[str] = "Connection was closed while running command"

_UPGRADED_SCRIPT_PATH: Final[Path] = Path("commands") / UPGRADED_SCRIPT_NAME
_SHARED_SHELL_LIB_PATHS: Final[tuple[Path, ...]] = tuple(Path("commands") / name for name in SHARED_SHELL_LIB_NAMES)
_ALL_STAGED_PATHS: Final[tuple[Path, ...]] = tuple(sorted([*_SHARED_SHELL_LIB_PATHS, _UPGRADED_SCRIPT_PATH]))

# The host operations an upgrade of a current agent makes: the lock, the input read, the upload's mkdir and rsync,
# and the comparison of the staged files.
_CURRENT_AGENT_OPERATION_COUNT: Final[int] = 5

# The most host operations an upgrade that restarts an idle command agent may make.
_IDLE_RESTART_OPERATION_BUDGET: Final[int] = 41


def _is_swap_command(command: str) -> bool:
    return UPGRADE_STAGING_DIR_NAME in command and "mv -f" in command


class _SwapReplayingSshHost(InProcessSshHost):
    """An in-process SSH host that runs the upgrade's swap command a second time, as a retry after a lost exit status does."""

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        raise_on_timeout: bool = False,
    ) -> CommandResult:
        if _is_swap_command(command):
            super().execute_idempotent_command(command, user, cwd, env, timeout_seconds, raise_on_timeout)
        return super().execute_idempotent_command(command, user, cwd, env, timeout_seconds, raise_on_timeout)


class _SwapDroppingSshHost(InProcessSshHost):
    """An in-process SSH host whose connection drops at the upgrade's swap command, before or after it runs, failing every later command."""

    is_swap_run_before_drop: bool = Field(description="Whether the swap command runs before the connection drops")
    is_connection_lost: bool = Field(default=False, description="Whether the connection has dropped")

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        raise_on_timeout: bool = False,
    ) -> CommandResult:
        if self.is_connection_lost:
            raise HostConnectionError(_CONNECTION_CLOSED_MESSAGE)
        if not _is_swap_command(command):
            return super().execute_idempotent_command(command, user, cwd, env, timeout_seconds, raise_on_timeout)
        if self.is_swap_run_before_drop:
            super().execute_idempotent_command(command, user, cwd, env, timeout_seconds, raise_on_timeout)
        self.is_connection_lost = True
        raise HostConnectionError(_CONNECTION_CLOSED_MESSAGE)


class _LockUnreachableSshHost(InProcessSshHost):
    """An in-process SSH host that cannot be reached to take the host lock."""

    @contextmanager
    def lock_cooperatively(self, timeout_seconds: float | None = 300.0) -> Generator[None, None, None]:
        raise HostConnectionError("Could not acquire host lock due to connection error")
        yield


class _LockCommandExitingSshHost(InProcessSshHost):
    """An in-process SSH host whose host lock command exits before it takes the lock."""

    @contextmanager
    def lock_cooperatively(self, timeout_seconds: float | None = 300.0) -> Generator[None, None, None]:
        raise HostError(_LOCK_COMMAND_EXITED_MESSAGE)
        yield


class _UploadUnreachableSshHost(InProcessSshHost):
    """An in-process SSH host whose upload creates its target and then cannot open its own connection to the host."""

    def copy_local_directory(self, source_path: Path, target_path: Path, extra_args: str | None) -> None:
        self.upload_count += 1
        self.execute_idempotent_command(f"mkdir -p {shlex.quote(str(target_path))}")
        raise HostConnectionError("rsync failed (push to 127.0.0.1): Connection refused")


def _write_stopped_agent_with_old_files(host: Host, work_dir: Path, agent_type: str) -> AgentMatch:
    """Write the record, of ``agent_type``, and the old files of a stopped agent on ``host``, and return the match that names it."""
    agent_id = AgentId.generate()
    agent_name = AgentName(f"remote-{agent_id.get_uuid().hex[:8]}")
    state_dir = write_command_agent_record(host, agent_id, agent_name, work_dir, {"type": agent_type})
    commands_dir = state_dir / "commands"
    commands_dir.mkdir()
    (commands_dir / UPGRADED_SCRIPT_NAME).write_text(_OLD_SCRIPT)
    (state_dir / READY_MARKER_NAME).touch()
    return AgentMatch(
        agent_id=agent_id,
        agent_name=agent_name,
        host_id=host.id,
        host_name=host.get_name(),
        provider_name=host.get_provider_name(),
    )


def _make_stopped_agent_with_old_files(
    host: Host, work_dir: Path, mngr_ctx: MngrContext, upgrade_unsupported_detail: str | None
) -> UpgradableCommandAgent:
    match = _write_stopped_agent_with_old_files(host, work_dir, "command")
    return make_upgradable_command_agent(
        host,
        match.agent_id,
        match.agent_name,
        work_dir,
        mngr_ctx,
        upgrade_unsupported_detail=upgrade_unsupported_detail,
        upgraded_script_content=_NEW_SCRIPT,
        build_error_message=None,
        agent_class=UpgradableCommandAgent,
    )


def _make_looked_up_ssh_host(
    create_ssh_host: Callable[[str], Host],
    host_class: type[InProcessSshHostT],
    harness: PinnedLookupHarness,
    **extra_fields: Any,
) -> InProcessSshHostT:
    """A ``host_class`` SSH host on the in-process server, held by the harness's first provider, where ``mngr upgrade`` looks it up."""
    ssh_host = make_in_process_ssh_host(create_ssh_host, host_class, **extra_fields)
    host = ssh_host.model_copy_update(to_update(ssh_host.field_ref().provider_instance, harness.first_provider))
    assert isinstance(host, host_class)
    harness.first_provider.mock_hosts.append(host)
    return host


def _count_operations(host: InProcessSshHost, server: ExecOnlyParamikoServer, exec_count_before: int) -> int:
    return len(server.exec_commands) - exec_count_before + host.count_operations_besides_ssh_commands()


def _upgrade(agent: UpgradableCommandAgent, host: InProcessSshHost, mngr_ctx: MngrContext) -> AgentUpgradeOutcome:
    return upgrade_agent(agent, host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=mngr_ctx)


def _run_upgrade_agents(matches: Sequence[AgentMatch], mngr_ctx: MngrContext) -> list[AgentUpgradeOutcome]:
    """The outcomes of ``mngr upgrade`` for the agents of ``matches``, not forced and not a dry run."""
    return list(
        upgrade_agents(
            matches,
            is_forced=False,
            is_dry_run=False,
            error_behavior=ErrorBehavior.CONTINUE,
            caller=None,
            mngr_ctx=mngr_ctx,
        )
    )


def _make_offline_outcome(match: AgentMatch, host: InProcessSshHost, error_message: str) -> AgentUpgradeSkipped:
    return AgentUpgradeSkipped(
        agent_id=match.agent_id,
        agent_name=match.agent_name,
        host=make_upgrade_outcome_host(host),
        kind=UpgradeSkipKind.NOT_IDLE,
        detail=UpgradeSkipDetail(f"its host could not be reached: {error_message}"),
        is_dry_run=False,
    )


@pytest.mark.tmux
def test_upgrade_over_ssh_rewrites_a_stopped_agents_files_and_then_finds_it_current_in_five_host_operations(
    paramiko_ssh_host_factory: tuple[Callable[[str], Host], ExecOnlyParamikoServer],
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    create_ssh_host, server = paramiko_ssh_host_factory
    host = make_in_process_ssh_host(create_ssh_host, InProcessSshHost)
    agent = _make_stopped_agent_with_old_files(host, tmp_path, temp_mngr_ctx, upgrade_unsupported_detail=None)
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)
    assert not host.is_local

    outcome = _upgrade(agent, host, temp_mngr_ctx)

    assert outcome == make_upgraded_while_stopped_outcome(agent, _ALL_STAGED_PATHS)
    assert format_upgrade_outcome(outcome) == (
        f"upgraded {agent.name}@{host.get_name()}.{host.get_provider_name()} (stopped)"
    )
    assert (state_dir / _UPGRADED_SCRIPT_PATH).read_text() == _NEW_SCRIPT
    for name in SHARED_SHELL_LIB_NAMES:
        assert (host.host_dir / "commands" / name).read_text() == (state_dir / "commands" / name).read_text()
    assert not (state_dir / READY_MARKER_NAME).exists()
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()

    exec_count_before = len(server.exec_commands)
    connection_count_before = server.accepted_connection_count
    current_host = make_in_process_ssh_host(create_ssh_host, InProcessSshHost)
    current_agent = agent.model_copy_update(to_update(agent.field_ref().host, current_host))

    current_outcome = _upgrade(current_agent, current_host, temp_mngr_ctx)

    assert current_outcome == make_current_outcome(current_agent)
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()
    assert _count_operations(current_host, server, exec_count_before) == _CURRENT_AGENT_OPERATION_COUNT
    assert server.accepted_connection_count - connection_count_before == 1


@pytest.mark.tmux
def test_upgrade_over_ssh_restarts_an_idle_agent_within_its_budget(
    paramiko_ssh_host_factory: tuple[Callable[[str], Host], ExecOnlyParamikoServer],
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    create_ssh_host, server = paramiko_ssh_host_factory
    agent_name = "upgrade-remote-idle-59124"
    session_name = create_test_agent_via_cli(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 959124"
    )
    with tmux_session_cleanup(session_name):
        host = make_in_process_ssh_host(create_ssh_host, InProcessSshHost)
        (created_agent,) = [listed for listed in host.get_agents() if listed.name == agent_name]
        agent = make_upgradable_command_agent(
            host,
            created_agent.id,
            created_agent.name,
            created_agent.work_dir,
            created_agent.mngr_ctx,
            upgrade_unsupported_detail=None,
            upgraded_script_content=_NEW_SCRIPT,
            build_error_message=None,
            agent_class=UpgradableCommandAgent,
        )
        pane_pid_before = read_tmux_pane_pid(session_name)
        exec_count_before = len(server.exec_commands)
        operation_count_before = host.count_operations_besides_ssh_commands()

        outcome = upgrade_agent(
            agent, host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=agent.mngr_ctx
        )

        assert outcome == AgentUpgraded(
            agent_id=agent.id,
            agent_name=agent.name,
            host=make_upgrade_outcome_host(host),
            plan=UpgradePlan.RESTART_IDLE,
            prior_state=AgentLifecycleState.WAITING,
            changed_paths=(_UPGRADED_SCRIPT_PATH,),
            is_dry_run=False,
        )
        state_dir = get_agent_state_dir_path(host.host_dir, agent.id)
        assert (state_dir / _UPGRADED_SCRIPT_PATH).read_text() == _NEW_SCRIPT
        assert tmux_session_exists(session_name)
        assert read_tmux_pane_pid(session_name) != pane_pid_before
        operation_count = _count_operations(host, server, exec_count_before) - operation_count_before
        assert operation_count <= _IDLE_RESTART_OPERATION_BUDGET


@pytest.mark.tmux
def test_upgrade_over_ssh_finishes_when_its_swap_command_runs_twice(
    paramiko_ssh_host_factory: tuple[Callable[[str], Host], ExecOnlyParamikoServer],
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    create_ssh_host, server = paramiko_ssh_host_factory
    host = make_in_process_ssh_host(create_ssh_host, _SwapReplayingSshHost)
    agent = _make_stopped_agent_with_old_files(host, tmp_path, temp_mngr_ctx, upgrade_unsupported_detail=None)
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    outcome = _upgrade(agent, host, temp_mngr_ctx)

    assert isinstance(outcome, AgentUpgraded)
    assert sum(_is_swap_command(command) for command in server.exec_commands) == 2
    assert (state_dir / _UPGRADED_SCRIPT_PATH).read_text() == _NEW_SCRIPT
    assert (state_dir / "commands" / "mngr_log.sh").is_file()
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()


def test_upgrade_over_ssh_whose_host_cannot_be_reached_for_the_lock_skips_the_agent_as_offline(
    paramiko_ssh_host_factory: tuple[Callable[[str], Host], ExecOnlyParamikoServer],
    pinned_lookup_harness: PinnedLookupHarness,
    tmp_path: Path,
    upgradable_agent_types: None,
) -> None:
    create_ssh_host, _server = paramiko_ssh_host_factory
    host = _make_looked_up_ssh_host(create_ssh_host, _LockUnreachableSshHost, pinned_lookup_harness)
    host.connect()
    # Collects the hosts the lookup dropped while the shared SSH client is open, as a collection during the run would.
    gc.collect()
    match = _write_stopped_agent_with_old_files(host, tmp_path, UPGRADABLE_COMMAND_AGENT_TYPE)
    state_dir = get_agent_state_dir_path(host.host_dir, match.agent_id)

    outcomes = _run_upgrade_agents([match], pinned_lookup_harness.mngr_ctx)

    assert outcomes == [_make_offline_outcome(match, host, "Could not acquire host lock due to connection error")]
    assert (state_dir / _UPGRADED_SCRIPT_PATH).read_text() == _OLD_SCRIPT
    assert (state_dir / READY_MARKER_NAME).exists()


def test_upgrade_over_ssh_whose_host_lock_command_exits_before_taking_the_lock_fails_at_the_lock_step_untouched(
    paramiko_ssh_host_factory: tuple[Callable[[str], Host], ExecOnlyParamikoServer],
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    create_ssh_host, _server = paramiko_ssh_host_factory
    host = make_in_process_ssh_host(create_ssh_host, _LockCommandExitingSshHost)
    agent = _make_stopped_agent_with_old_files(host, tmp_path, temp_mngr_ctx, upgrade_unsupported_detail=None)
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    outcome = _upgrade(agent, host, temp_mngr_ctx)

    assert outcome == AgentUpgradeFailed(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(host),
        step=UpgradeFailedStep.LOCK,
        error_message=_LOCK_COMMAND_EXITED_MESSAGE,
    )
    assert format_upgrade_outcome(outcome).endswith("; untouched")
    assert (state_dir / _UPGRADED_SCRIPT_PATH).read_text() == _OLD_SCRIPT
    assert (state_dir / READY_MARKER_NAME).exists()


def test_upgrade_over_ssh_whose_upload_cannot_reach_the_host_skips_the_agent_as_offline_and_removes_its_staging(
    paramiko_ssh_host_factory: tuple[Callable[[str], Host], ExecOnlyParamikoServer],
    pinned_lookup_harness: PinnedLookupHarness,
    tmp_path: Path,
    upgradable_agent_types: None,
) -> None:
    create_ssh_host, _server = paramiko_ssh_host_factory
    host = _make_looked_up_ssh_host(create_ssh_host, _UploadUnreachableSshHost, pinned_lookup_harness)
    host.connect()
    # Collects the hosts the lookup dropped while the shared SSH client is open, as a collection during the run would.
    gc.collect()
    match = _write_stopped_agent_with_old_files(host, tmp_path, UPGRADABLE_COMMAND_AGENT_TYPE)
    state_dir = get_agent_state_dir_path(host.host_dir, match.agent_id)

    outcomes = _run_upgrade_agents([match], pinned_lookup_harness.mngr_ctx)

    assert outcomes == [_make_offline_outcome(match, host, "rsync failed (push to 127.0.0.1): Connection refused")]
    assert host.upload_count == 1
    assert (state_dir / _UPGRADED_SCRIPT_PATH).read_text() == _OLD_SCRIPT
    assert (state_dir / READY_MARKER_NAME).exists()
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()


def test_upgrade_over_ssh_of_an_agent_with_files_to_stage_ends_with_ssh_install_hint_when_ssh_is_missing(
    paramiko_ssh_host_factory: tuple[Callable[[str], Host], ExecOnlyParamikoServer],
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    path_without_ssh: None,
) -> None:
    create_ssh_host, _server = paramiko_ssh_host_factory
    host = make_in_process_ssh_host(create_ssh_host, InProcessSshHost)
    agent = _make_stopped_agent_with_old_files(host, tmp_path, temp_mngr_ctx, upgrade_unsupported_detail=None)
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    with pytest.raises(BinaryNotInstalledError) as exc_info:
        _upgrade(agent, host, temp_mngr_ctx)

    assert str(exc_info.value) == f"ssh is required for {SSH.purpose} but was not found on PATH"
    assert exc_info.value.user_help_text == SSH.install_hint
    assert host.upload_count == 0
    assert (state_dir / _UPGRADED_SCRIPT_PATH).read_text() == _OLD_SCRIPT


def test_upgrade_over_ssh_names_an_unsupported_agent_by_its_address_in_the_recreate_command(
    paramiko_ssh_host_factory: tuple[Callable[[str], Host], ExecOnlyParamikoServer],
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
) -> None:
    create_ssh_host, _server = paramiko_ssh_host_factory
    host = make_in_process_ssh_host(create_ssh_host, InProcessSshHost)
    agent = _make_stopped_agent_with_old_files(
        host, tmp_path, temp_mngr_ctx, upgrade_unsupported_detail="its launch command loads no file of mngr's"
    )
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    outcome = _upgrade(agent, host, temp_mngr_ctx)

    assert outcome == make_skipped_outcome(
        agent,
        UpgradeSkip(
            kind=UpgradeSkipKind.UNSUPPORTED,
            detail=UpgradeSkipDetail(
                "its launch command loads no file of mngr's; re-create it with "
                f"`mngr create {agent.name}@{host.get_name()}.{host.get_provider_name()} --reuse --update`"
            ),
        ),
    )
    assert (state_dir / _UPGRADED_SCRIPT_PATH).read_text() == _OLD_SCRIPT


@pytest.mark.tmux
@pytest.mark.parametrize(
    "is_swap_run_before_drop", [False, True], ids=["drop_before_the_swap_runs", "drop_after_the_swap_ran"]
)
def test_upgrade_over_ssh_whose_connection_drops_at_the_swap_fails_there_and_a_later_run_gives_the_stopped_agent_its_files(
    paramiko_ssh_host_factory: tuple[Callable[[str], Host], ExecOnlyParamikoServer],
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    is_swap_run_before_drop: bool,
) -> None:
    create_ssh_host, _server = paramiko_ssh_host_factory
    dropping_host = make_in_process_ssh_host(
        create_ssh_host, _SwapDroppingSshHost, is_swap_run_before_drop=is_swap_run_before_drop
    )
    agent = _make_stopped_agent_with_old_files(dropping_host, tmp_path, temp_mngr_ctx, upgrade_unsupported_detail=None)
    state_dir = get_agent_state_dir_path(dropping_host.host_dir, agent.id)

    with pytest.raises(ConnectionLostAtUpgradeStepError) as exc_info:
        _upgrade(agent, dropping_host, temp_mngr_ctx)

    outcome = exc_info.value.failure
    assert outcome == AgentUpgradeFailed(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(dropping_host),
        step=UpgradeFailedStep.SWAP,
        error_message=_CONNECTION_CLOSED_MESSAGE,
    )
    assert format_upgrade_outcome(outcome).endswith("; left stopped; some files may already be replaced")
    assert (state_dir / _UPGRADED_SCRIPT_PATH).read_text() == (_NEW_SCRIPT if is_swap_run_before_drop else _OLD_SCRIPT)
    assert (state_dir / UPGRADE_STAGING_DIR_NAME).is_dir()

    reached_host = make_in_process_ssh_host(create_ssh_host, InProcessSshHost)
    reached_agent = agent.model_copy_update(to_update(agent.field_ref().host, reached_host))
    later_outcome = _upgrade(reached_agent, reached_host, temp_mngr_ctx)

    assert later_outcome == (
        make_current_outcome(reached_agent)
        if is_swap_run_before_drop
        else make_upgraded_while_stopped_outcome(reached_agent, _ALL_STAGED_PATHS)
    )
    assert (state_dir / _UPGRADED_SCRIPT_PATH).read_text() == _NEW_SCRIPT
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()


@pytest.mark.tmux
def test_upgrade_agents_over_ssh_whose_connection_drops_at_an_agents_swap_fails_it_there_and_skips_the_hosts_next_agent_without_locking_again(
    paramiko_ssh_host_factory: tuple[Callable[[str], Host], ExecOnlyParamikoServer],
    pinned_lookup_harness: PinnedLookupHarness,
    tmp_path: Path,
    upgradable_agent_types: None,
) -> None:
    create_ssh_host, _server = paramiko_ssh_host_factory
    host = _make_looked_up_ssh_host(
        create_ssh_host, _SwapDroppingSshHost, pinned_lookup_harness, is_swap_run_before_drop=False
    )
    host.connect()
    # Collects the hosts the lookup dropped while the shared SSH client is open, as a collection during the run would.
    gc.collect()
    first_match, second_match = (
        _write_stopped_agent_with_old_files(host, tmp_path, UPGRADABLE_COMMAND_AGENT_TYPE) for _ in range(2)
    )

    outcomes = _run_upgrade_agents([first_match, second_match], pinned_lookup_harness.mngr_ctx)

    assert outcomes == [
        AgentUpgradeFailed(
            agent_id=first_match.agent_id,
            agent_name=first_match.agent_name,
            host=make_upgrade_outcome_host(host),
            step=UpgradeFailedStep.SWAP,
            error_message=_CONNECTION_CLOSED_MESSAGE,
        ),
        _make_offline_outcome(second_match, host, _CONNECTION_CLOSED_MESSAGE),
    ]
    assert host.lock_count == 1
    second_state_dir = get_agent_state_dir_path(host.host_dir, second_match.agent_id)
    assert (second_state_dir / _UPGRADED_SCRIPT_PATH).read_text() == _OLD_SCRIPT
