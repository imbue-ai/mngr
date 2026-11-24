import errno
import fcntl
import json
import os
import shlex
import shutil
import stat
import threading
from collections.abc import Generator
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import pluggy
import pytest
from click.testing import CliRunner
from pydantic import Field

from imbue.imbue_common.model_update import to_update
from imbue.mngr.agents.upgrade import AGENT_DATA_RELATIVE_PATH
from imbue.mngr.agents.upgrade import build_shared_shell_lib_files
from imbue.mngr.api.conftest import PinnedLookupHarness
from imbue.mngr.api.conftest import PinnedLookupHarnessFactory
from imbue.mngr.api.discovery_events import AgentDiscoveryEvent
from imbue.mngr.api.discovery_events import get_discovery_events_path
from imbue.mngr.api.discovery_events import parse_discovery_event_line
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.find import START_HOST_LOCK_TIMEOUT_SECONDS
from imbue.mngr.api.testing import DIALOG_MARKER_NAME
from imbue.mngr.api.testing import LockRecordingHost
from imbue.mngr.api.testing import NEVER_READY_TIMEOUT_SECONDS
from imbue.mngr.api.testing import NeverReadyCommandAgent
from imbue.mngr.api.testing import READY_MARKER_NAME
from imbue.mngr.api.testing import ScriptedStateCommandAgent
from imbue.mngr.api.testing import UPGRADABLE_COMMAND_AGENT_TYPE
from imbue.mngr.api.testing import UPGRADED_SCRIPT_NAME
from imbue.mngr.api.testing import UpgradableCommandAgent
from imbue.mngr.api.testing import make_current_outcome
from imbue.mngr.api.testing import make_recorded_upgradable_agent
from imbue.mngr.api.testing import make_skipped_outcome
from imbue.mngr.api.testing import make_upgradable_command_agent
from imbue.mngr.api.testing import make_upgrade_outcome_host
from imbue.mngr.api.testing import upgrade_agent
from imbue.mngr.api.testing import write_command_agent_record
from imbue.mngr.api.upgrade import AgentCurrent
from imbue.mngr.api.upgrade import AgentUpgradeFailed
from imbue.mngr.api.upgrade import AgentUpgradeSkipped
from imbue.mngr.api.upgrade import AgentUpgraded
from imbue.mngr.api.upgrade import ReachedHost
from imbue.mngr.api.upgrade import SKIP_ABORTED
from imbue.mngr.api.upgrade import SKIP_DESTROYED
from imbue.mngr.api.upgrade import SKIP_EXITED
from imbue.mngr.api.upgrade import SKIP_MID_TURN
from imbue.mngr.api.upgrade import SKIP_ON_DIALOG
from imbue.mngr.api.upgrade import SKIP_STATE_UNREADABLE
from imbue.mngr.api.upgrade import UPGRADE_MESSAGE_LOCK_TIMEOUT_SECONDS
from imbue.mngr.api.upgrade import UPGRADE_STAGING_DIR_NAME
from imbue.mngr.api.upgrade import UpgradeFailedStep
from imbue.mngr.api.upgrade import UpgradePlan
from imbue.mngr.api.upgrade import UpgradeSkip
from imbue.mngr.api.upgrade import UpgradeSkipKind
from imbue.mngr.api.upgrade import build_compare_staged_files_command
from imbue.mngr.api.upgrade import build_read_upgrade_inputs_command
from imbue.mngr.api.upgrade import build_swap_command
from imbue.mngr.api.upgrade import iter_upgrade_outcomes
from imbue.mngr.api.upgrade import parse_upgrade_inputs_output
from imbue.mngr.api.upgrade import upgrade_agents
from imbue.mngr.api.upgrade import upgrade_matched_agent
from imbue.mngr.cli.testing import create_test_agent_state
from imbue.mngr.cli.upgrade import format_upgrade_outcome
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import HostConnectionError
from imbue.mngr.errors import HostNotFoundError
from imbue.mngr.errors import LockLostError
from imbue.mngr.errors import MessageLockTimeoutError
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.common import get_agents_root_dir
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.host import SHARED_SHELL_LIB_NAMES
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.cleanup_failures import CleanupFailedGroup
from imbue.mngr.interfaces.data_types import CleanupFailure
from imbue.mngr.interfaces.data_types import CleanupFailureCategory
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.data_types import HarnessFileSpec
from imbue.mngr.interfaces.data_types import HostLifecycleSnapshot
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ErrorBehavior
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import HostState
from imbue.mngr.primitives import LOCAL_PROVIDER_NAME
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import create_test_agent_via_cli
from imbue.mngr.utils.testing import file_lock_held_by_another_process
from imbue.mngr.utils.testing import make_local_host_of_class
from imbue.mngr.utils.testing import read_tmux_pane_pid
from imbue.mngr.utils.testing import tmux_session_cleanup
from imbue.mngr.utils.testing import tmux_session_exists

_OLD_SCRIPT: Final[str] = "echo old-script-48213\n"
_NEW_SCRIPT: Final[str] = "echo new-script-48213\n"

_UPGRADED_SCRIPT_PATH: Final[Path] = Path("commands") / UPGRADED_SCRIPT_NAME
_MERGED_SETTINGS_PATH: Final[Path] = Path("harness-settings-948229.json")

_MISSING_RECORD_DETAIL: Final[str] = "its record is missing"
_LOCK_LOST_AT_STATE_READ_MESSAGE: Final[str] = (
    "Host lock was lost while the connection was dropped: acquisition counter is 9, expected 7 "
    "(another actor acquired the lock)"
)
_CONNECTION_LOST_MESSAGE: Final[str] = "Connection was closed while running command"
_MESSAGE_LOCK_NAME: Final[str] = "message.lock"
_SHORT_MESSAGE_LOCK_WAIT_SECONDS: Final[float] = 0.5
_SHARED_SHELL_LIB_PATHS: Final[tuple[Path, ...]] = tuple(Path("commands") / name for name in SHARED_SHELL_LIB_NAMES)
# What an upgrade rewrites for an agent whose state dir holds none of the shared shell libs.
_ALL_STAGED_PATHS: Final[tuple[Path, ...]] = tuple(sorted([*_SHARED_SHELL_LIB_PATHS, _UPGRADED_SCRIPT_PATH]))


class _StartRecordingHost(Host):
    """Local Host that records each agent's live upgraded script as start finds it, then starts the agent."""

    scripts_at_start: list[str | None] = Field(
        default_factory=list, description="The live upgraded script at each agent start, or None when absent"
    )

    def start_agents(self, agent_ids: Sequence[AgentId]) -> None:
        for agent_id in agent_ids:
            script_path = get_agent_state_dir_path(self.host_dir, agent_id) / "commands" / UPGRADED_SCRIPT_NAME
            self.scripts_at_start.append(script_path.read_text() if script_path.exists() else None)
        super().start_agents(agent_ids)


class _StopFailingHost(Host):
    """Local Host whose agent stops fail with the group a real stop raises when it leaves a session behind."""

    def stop_agents(self, agent_ids: Sequence[AgentId], timeout_seconds: float = 5.0) -> None:
        raise CleanupFailedGroup.from_failures(
            [CleanupFailure(category=CleanupFailureCategory.LOCAL_STATE_REMAINS, message="tmux kill-session failed")]
        )


class _SettingsMergingCommandAgent(ScriptedStateCommandAgent):
    """A command agent whose upgrade sets mngr's key in a settings file another program also writes."""

    def get_upgrade_input_paths(self) -> tuple[Path, ...]:
        return (_MERGED_SETTINGS_PATH,)

    def build_upgraded_files(self, input_content_by_path: Mapping[Path, str | None]) -> dict[Path, HarnessFileSpec]:
        live_settings = json.loads(input_content_by_path[_MERGED_SETTINGS_PATH] or "{}")
        return {
            _MERGED_SETTINGS_PATH: HarnessFileSpec(
                content=json.dumps({**live_settings, "mngr_key": "new"}), mode="0644"
            )
        }


class _RecordRequiringCommandAgent(ScriptedStateCommandAgent):
    """A command agent that cannot be upgraded while its record reads as missing."""

    def get_upgrade_input_paths(self) -> tuple[Path, ...]:
        return (AGENT_DATA_RELATIVE_PATH,)

    def get_upgrade_unsupported_detail(self, input_content_by_path: Mapping[Path, str | None]) -> str | None:
        return _MISSING_RECORD_DETAIL if input_content_by_path[AGENT_DATA_RELATIVE_PATH] is None else None


class _LockLosingStateCommandAgent(ScriptedStateCommandAgent):
    """A command agent whose state reads return its scripted states once each and then raise, as a read that reconnects after another mngr took the host lock does."""

    def get_lifecycle_state(self) -> AgentLifecycleState:
        if not self.lifecycle_states:
            raise LockLostError(_LOCK_LOST_AT_STATE_READ_MESSAGE)
        return self.lifecycle_states.pop(0)


class _SettingsWritingHost(LockRecordingHost):
    """A LockRecordingHost on which another program writes an agent's settings file once, during its upgrade."""

    written_settings: dict[str, object] = Field(description="What the other program writes")
    is_written_at_staging_upload: bool = Field(
        description="Write at the first upload of the upgrade's staged files, rather than at the agent's stop"
    )
    is_written: bool = Field(default=False, description="Whether the write happened")

    def _write_settings_once(self, state_dir: Path) -> None:
        if not self.is_written:
            (state_dir / _MERGED_SETTINGS_PATH).write_text(json.dumps(self.written_settings))
            self.is_written = True

    def stop_agents(self, agent_ids: Sequence[AgentId], timeout_seconds: float = 5.0) -> None:
        if not self.is_written_at_staging_upload:
            for agent_id in agent_ids:
                self._write_settings_once(get_agent_state_dir_path(self.host_dir, agent_id))
        super().stop_agents(agent_ids, timeout_seconds)

    def copy_local_directory(self, source_path: Path, target_path: Path, extra_args: str | None) -> None:
        if self.is_written_at_staging_upload and target_path.name == UPGRADE_STAGING_DIR_NAME:
            self._write_settings_once(target_path.parent)
        super().copy_local_directory(source_path, target_path, extra_args)


class _DestroyingAtStopHost(LockRecordingHost):
    """A LockRecordingHost on which another program destroys an agent as its upgrade stops it, after its files were staged."""

    def stop_agents(self, agent_ids: Sequence[AgentId], timeout_seconds: float = 5.0) -> None:
        for agent_id in agent_ids:
            shutil.rmtree(get_agent_state_dir_path(self.host_dir, agent_id))
        super().stop_agents(agent_ids, timeout_seconds)


class _DestroyingAtStagingHost(LockRecordingHost):
    """A LockRecordingHost on which another program destroys an agent just before its upgrade uploads its staged files."""

    def copy_local_directory(self, source_path: Path, target_path: Path, extra_args: str | None) -> None:
        if target_path.name == UPGRADE_STAGING_DIR_NAME:
            shutil.rmtree(target_path.parent)
        super().copy_local_directory(source_path, target_path, extra_args)


class _DestroyingAfterStagingHost(LockRecordingHost):
    """A LockRecordingHost on which another program destroys an agent right after its upgrade uploaded its staged files."""

    def copy_local_directory(self, source_path: Path, target_path: Path, extra_args: str | None) -> None:
        super().copy_local_directory(source_path, target_path, extra_args)
        if target_path.name == UPGRADE_STAGING_DIR_NAME:
            shutil.rmtree(target_path.parent)


class _DestroyingAtStartHost(LockRecordingHost):
    """A LockRecordingHost on which another program destroys an agent as its upgrade starts it again, and which then runs the real start."""

    def start_agents(self, agent_ids: Sequence[AgentId]) -> None:
        for agent_id in agent_ids:
            shutil.rmtree(get_agent_state_dir_path(self.host_dir, agent_id))
        Host.start_agents(self, agent_ids)


class _DestroyingAtStartCheckHost(LockRecordingHost):
    """A LockRecordingHost on which another program destroys an agent as its upgrade checks that it can start, after its files were staged."""

    def check_agent_can_start(self, agent: AgentInterface) -> None:
        shutil.rmtree(get_agent_state_dir_path(self.host_dir, agent.id))
        super().check_agent_can_start(agent)


class _DestroyingAtHostCommandHost(LockRecordingHost):
    """A LockRecordingHost on which another program destroys every agent just before the first host command holding ``destroy_before_command_containing`` runs."""

    destroy_before_command_containing: str = Field(description="Text of the host command the destroy comes before")
    is_destroyed: bool = Field(default=False, description="Whether the destroy happened")

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        raise_on_timeout: bool = False,
    ) -> CommandResult:
        if not self.is_destroyed and self.destroy_before_command_containing in command:
            for agent_state_dir in get_agents_root_dir(self.host_dir).iterdir():
                shutil.rmtree(agent_state_dir)
            self.is_destroyed = True
        return super().execute_idempotent_command(command, user, cwd, env, timeout_seconds, raise_on_timeout)


class _LockLosingHost(Host):
    """Local Host whose cooperative lock reports, as it is released, that another actor took it during the block."""

    @contextmanager
    def lock_cooperatively(self, timeout_seconds: float | None = 300.0) -> Generator[None, None, None]:
        yield
        raise LockLostError("Host lock was lost during the critical section: acquisition counter is 8, expected 7")


class _ConnectionLosingHost(LockRecordingHost):
    """A LockRecordingHost whose connection is lost at each attempt to take its lock."""

    @contextmanager
    def lock_cooperatively(self, timeout_seconds: float | None = 300.0) -> Generator[None, None, None]:
        self.lock_timeouts.append(timeout_seconds)
        raise HostConnectionError(_CONNECTION_LOST_MESSAGE)
        yield


class _UnreadableLifecycleHost(Host):
    """Local Host whose tmux panes cannot be read, so the state of every agent on it is unknown."""

    def read_lifecycle_snapshot(self) -> HostLifecycleSnapshot | None:
        return None


class _TurnMarkerCommandAgent(UpgradableCommandAgent):
    """An upgradable command agent that is mid-turn while its state dir holds ``active`` and idle otherwise, and records each wait for its message lock."""

    message_lock_timeouts: list[float] = Field(
        default_factory=list, description="The timeout_seconds of each hold_message_lock call"
    )
    message_lock_wait_seconds: float | None = Field(
        default=None, description="How long it waits for its message lock in place of the timeout it is given, if set"
    )

    def get_lifecycle_state(self) -> AgentLifecycleState:
        if self._check_file_exists(self._get_agent_dir() / "active"):
            return AgentLifecycleState.RUNNING
        return AgentLifecycleState.WAITING

    @contextmanager
    def hold_message_lock(self, timeout_seconds: float) -> Generator[None, None, None]:
        self.message_lock_timeouts.append(timeout_seconds)
        wait_seconds = timeout_seconds if self.message_lock_wait_seconds is None else self.message_lock_wait_seconds
        with super().hold_message_lock(wait_seconds):
            yield


class _MessageLockObservingHost(Host):
    """Local Host that records, as it stops or starts each agent, whether that agent's message lock is free."""

    is_message_lock_free_at_stop: list[bool] = Field(
        default_factory=list, description="Whether the message lock was free at each agent stop"
    )
    is_message_lock_free_at_start: list[bool] = Field(
        default_factory=list, description="Whether the message lock was free at each agent start"
    )

    def stop_agents(self, agent_ids: Sequence[AgentId], timeout_seconds: float = 5.0) -> None:
        self.is_message_lock_free_at_stop.extend(
            _is_message_lock_free(self.host_dir, agent_id) for agent_id in agent_ids
        )
        super().stop_agents(agent_ids, timeout_seconds)

    def start_agents(self, agent_ids: Sequence[AgentId]) -> None:
        self.is_message_lock_free_at_start.extend(
            _is_message_lock_free(self.host_dir, agent_id) for agent_id in agent_ids
        )
        super().start_agents(agent_ids)


def _is_message_lock_free(host_dir: Path, agent_id: AgentId) -> bool:
    with open(get_agent_state_dir_path(host_dir, agent_id) / _MESSAGE_LOCK_NAME, "a") as probe_file:
        try:
            fcntl.flock(probe_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(probe_file.fileno(), fcntl.LOCK_UN)
    return True


def _finish_send_once_upgrade_waits(
    send_lock_descriptor: int,
    agent: _TurnMarkerCommandAgent,
    is_turn_started_by_send: bool,
    is_block_exited: threading.Event,
) -> None:
    poll_until(
        lambda: len(agent.message_lock_timeouts) > 0 or is_block_exited.is_set(),
        timeout=UPGRADE_MESSAGE_LOCK_TIMEOUT_SECONDS,
        poll_interval=0.01,
    )
    if is_turn_started_by_send:
        (get_agent_state_dir_path(agent.host.host_dir, agent.id) / "active").touch()
    fcntl.flock(send_lock_descriptor, fcntl.LOCK_UN)


@contextmanager
def _message_lock_held_by_a_send(
    agent: _TurnMarkerCommandAgent, is_turn_started_by_send: bool
) -> Generator[None, None, None]:
    """Hold the agent's message lock from a file handle of its own, as a send in flight does, until the upgrade waits for it; then start a turn if ``is_turn_started_by_send`` and release the lock."""
    lock_path = get_agent_state_dir_path(agent.host.host_dir, agent.id) / _MESSAGE_LOCK_NAME
    is_block_exited = threading.Event()
    with open(lock_path, "w") as send_lock_file:
        fcntl.flock(send_lock_file.fileno(), fcntl.LOCK_EX)
        sender = threading.Thread(
            target=_finish_send_once_upgrade_waits,
            args=(send_lock_file.fileno(), agent, is_turn_started_by_send, is_block_exited),
            daemon=True,
        )
        sender.start()
        try:
            yield
        finally:
            is_block_exited.set()
            sender.join(timeout=10.0)


def _read_state_dir_files(state_dir: Path) -> dict[Path, bytes]:
    return {path.relative_to(state_dir): path.read_bytes() for path in state_dir.rglob("*") if path.is_file()}


def _make_stopped_agent_with_old_files(
    local_host: Host, tmp_path: Path, temp_mngr_ctx: MngrContext, build_error_message: str | None
) -> UpgradableCommandAgent:
    agent_id = AgentId.generate()
    agent_name = AgentName(f"stopped-{agent_id.get_uuid().hex[:8]}")
    state_dir = write_command_agent_record(local_host, agent_id, agent_name, tmp_path, {})
    commands_dir = state_dir / "commands"
    commands_dir.mkdir()
    (commands_dir / UPGRADED_SCRIPT_NAME).write_text(_OLD_SCRIPT)
    (commands_dir / "added_by_another_plugin.sh").write_text("echo kept-48213\n")
    (state_dir / READY_MARKER_NAME).touch()
    return make_upgradable_command_agent(
        local_host,
        agent_id,
        agent_name,
        tmp_path,
        temp_mngr_ctx,
        upgrade_unsupported_detail=None,
        upgraded_script_content=_NEW_SCRIPT,
        build_error_message=build_error_message,
        agent_class=UpgradableCommandAgent,
    )


def _with_lifecycle_states(
    agent: ScriptedStateCommandAgent, lifecycle_states: Iterable[AgentLifecycleState]
) -> ScriptedStateCommandAgent:
    return agent.model_copy_update(to_update(agent.field_ref().lifecycle_states, list(lifecycle_states)))


def _find_cli_agent(
    host: Host, agent_name: str, build_error_message: str | None, agent_class: type[UpgradableCommandAgent]
) -> UpgradableCommandAgent:
    (created_agent,) = [agent for agent in host.get_agents() if agent.name == agent_name]
    return make_upgradable_command_agent(
        host,
        created_agent.id,
        created_agent.name,
        created_agent.work_dir,
        created_agent.mngr_ctx,
        upgrade_unsupported_detail=None,
        upgraded_script_content=_NEW_SCRIPT,
        build_error_message=build_error_message,
        agent_class=agent_class,
    )


def _is_line_written_to_the_reader_of(fifo_path: Path) -> bool:
    """Write a line to the FIFO if a process is blocked reading it, and say whether one was."""
    try:
        fifo_descriptor = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)
    except OSError as e:
        if e.errno != errno.ENXIO:
            raise
        return False
    with os.fdopen(fifo_descriptor, "w") as fifo:
        fifo.write("\n")
    return True


def _read_discovery_event_lines(mngr_ctx: MngrContext) -> list[str]:
    return get_discovery_events_path(mngr_ctx.config).read_text().splitlines()


def _is_discovery_event_for_agent_emitted_since(agent: UpgradableCommandAgent, event_line_count_before: int) -> bool:
    return any(
        isinstance(event, AgentDiscoveryEvent) and event.agent.agent_id == agent.id
        for event in (
            parse_discovery_event_line(line)
            for line in _read_discovery_event_lines(agent.mngr_ctx)[event_line_count_before:]
        )
    )


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_rewrites_a_stopped_agents_files_leaves_it_stopped_and_then_reports_it_current(
    local_host: Host, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    agent = _make_stopped_agent_with_old_files(local_host, tmp_path, temp_mngr_ctx, build_error_message=None)
    state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)

    outcome = upgrade_agent(
        agent, local_host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx
    )

    assert outcome == AgentUpgraded(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(agent.get_host()),
        plan=UpgradePlan.LEAVE_STOPPED,
        prior_state=AgentLifecycleState.STOPPED,
        changed_paths=_ALL_STAGED_PATHS,
        is_dry_run=False,
    )
    assert (state_dir / "commands" / UPGRADED_SCRIPT_NAME).read_text() == _NEW_SCRIPT
    assert (state_dir / "commands" / "mngr_background_task_lib.sh").is_file()
    assert (local_host.host_dir / "commands" / "mngr_background_task_lib.sh").is_file()
    assert (state_dir / "commands" / "added_by_another_plugin.sh").read_text() == "echo kept-48213\n"
    assert not (state_dir / READY_MARKER_NAME).exists()
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()
    assert agent.get_lifecycle_state() == AgentLifecycleState.STOPPED

    assert upgrade_agent(
        agent, local_host, is_forced=False, is_dry_run=True, is_caller=False, mngr_ctx=temp_mngr_ctx
    ) == (
        AgentCurrent(
            agent_id=agent.id, agent_name=agent.name, host=make_upgrade_outcome_host(agent.get_host()), is_dry_run=True
        )
    )
    assert upgrade_agent(
        agent, local_host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx
    ) == (
        AgentCurrent(
            agent_id=agent.id,
            agent_name=agent.name,
            host=make_upgrade_outcome_host(agent.get_host()),
            is_dry_run=False,
        )
    )


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_replaces_a_file_an_earlier_run_left_in_the_staging_dir_rather_than_moving_it_in(
    local_host: Host, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    agent = _make_stopped_agent_with_old_files(local_host, tmp_path, temp_mngr_ctx, build_error_message=None)
    state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)
    left_behind_path = Path("commands") / "left-by-an-earlier-run-48213.sh"
    (state_dir / UPGRADE_STAGING_DIR_NAME / left_behind_path).parent.mkdir(parents=True)
    (state_dir / UPGRADE_STAGING_DIR_NAME / left_behind_path).write_text("echo left-behind-48213\n")

    outcome = upgrade_agent(
        agent, local_host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx
    )

    assert outcome == AgentUpgraded(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(agent.get_host()),
        plan=UpgradePlan.LEAVE_STOPPED,
        prior_state=AgentLifecycleState.STOPPED,
        changed_paths=_ALL_STAGED_PATHS,
        is_dry_run=False,
    )
    assert not (state_dir / left_behind_path).exists()
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_applies_a_change_of_only_the_executable_bit_and_ignores_other_permission_bits(
    local_host: Host, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    agent = _make_stopped_agent_with_old_files(local_host, tmp_path, temp_mngr_ctx, build_error_message=None)
    state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)
    for relative_path, upgraded_file in build_shared_shell_lib_files().items():
        (state_dir / relative_path).write_text(upgraded_file.content)
        (state_dir / relative_path).chmod(0o700)
    script_path = state_dir / _UPGRADED_SCRIPT_PATH
    script_path.write_text(_NEW_SCRIPT)
    script_path.chmod(0o644)

    outcome = upgrade_agent(
        agent, local_host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx
    )

    assert outcome == AgentUpgraded(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(agent.get_host()),
        plan=UpgradePlan.LEAVE_STOPPED,
        prior_state=AgentLifecycleState.STOPPED,
        changed_paths=(_UPGRADED_SCRIPT_PATH,),
        is_dry_run=False,
    )
    assert stat.S_IMODE(script_path.stat().st_mode) == 0o755
    assert script_path.read_text() == _NEW_SCRIPT
    assert {stat.S_IMODE((state_dir / path).stat().st_mode) for path in _SHARED_SHELL_LIB_PATHS} == {0o700}


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_dry_run_writes_nothing(local_host: Host, tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    agent = _make_stopped_agent_with_old_files(local_host, tmp_path, temp_mngr_ctx, build_error_message=None)
    state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)

    outcome = upgrade_agent(
        agent, local_host, is_forced=False, is_dry_run=True, is_caller=False, mngr_ctx=temp_mngr_ctx
    )

    assert outcome == AgentUpgraded(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(agent.get_host()),
        plan=UpgradePlan.LEAVE_STOPPED,
        prior_state=AgentLifecycleState.STOPPED,
        changed_paths=_ALL_STAGED_PATHS,
        is_dry_run=True,
    )
    assert (state_dir / "commands" / UPGRADED_SCRIPT_NAME).read_text() == _OLD_SCRIPT
    assert not (state_dir / "commands" / "mngr_background_task_lib.sh").exists()
    assert (state_dir / READY_MARKER_NAME).exists()
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()


def test_upgrade_that_cannot_write_the_new_files_leaves_a_stopped_agent_untouched(
    local_host: Host, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    agent = _make_stopped_agent_with_old_files(
        local_host, tmp_path, temp_mngr_ctx, build_error_message="cannot build the hooks"
    )
    state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)

    outcome = upgrade_agent(
        agent, local_host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx
    )

    assert outcome == AgentUpgradeFailed(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(agent.get_host()),
        step=UpgradeFailedStep.PREPARE,
        error_message="cannot build the hooks",
    )
    assert (state_dir / "commands" / UPGRADED_SCRIPT_NAME).read_text() == _OLD_SCRIPT
    assert not (state_dir / "commands" / "mngr_background_task_lib.sh").exists()
    assert (state_dir / READY_MARKER_NAME).exists()
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_swaps_in_a_file_whose_new_directory_and_name_contain_spaces_and_a_unicode_line_separator(
    local_host: Host, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    agent = _make_stopped_agent_with_old_files(local_host, tmp_path, temp_mngr_ctx, build_error_message=None)
    spaced_path = Path("commands") / "new dir 48213" / "has space\u2028and separator.sh"
    spaced_agent = agent.model_copy_update(to_update(agent.field_ref().upgraded_script_path, spaced_path))
    state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)

    outcome = upgrade_agent(
        spaced_agent, local_host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx
    )

    assert outcome == AgentUpgraded(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(agent.get_host()),
        plan=UpgradePlan.LEAVE_STOPPED,
        prior_state=AgentLifecycleState.STOPPED,
        changed_paths=tuple(sorted([*_SHARED_SHELL_LIB_PATHS, spaced_path])),
        is_dry_run=False,
    )
    assert (state_dir / spaced_path).read_text() == _NEW_SCRIPT


def test_upgrade_that_cannot_take_the_host_lock_leaves_the_agent_untouched(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = make_local_host_of_class(local_provider, LockRecordingHost, is_lock_refused=True)
    agent = _make_stopped_agent_with_old_files(host, tmp_path, temp_mngr_ctx, build_error_message=None)
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    outcome = upgrade_agent(agent, host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx)

    assert outcome == AgentUpgradeFailed(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(agent.get_host()),
        step=UpgradeFailedStep.LOCK,
        error_message="Timed out waiting to acquire the host lock",
    )
    assert format_upgrade_outcome(outcome).endswith("; untouched")
    assert (state_dir / "commands" / UPGRADED_SCRIPT_NAME).read_text() == _OLD_SCRIPT
    assert (state_dir / READY_MARKER_NAME).exists()
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_that_cannot_swap_the_new_files_in_reports_the_commands_error_and_leaves_the_agent_stopped(
    local_host: Host, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    agent_id = AgentId.generate()
    agent_name = AgentName("swap-fails-48213")
    state_dir = write_command_agent_record(local_host, agent_id, agent_name, tmp_path, {})
    # A file where the upgraded files' directory belongs, so the swap cannot create that directory.
    (state_dir / "commands").write_text("not a directory 48213\n")
    agent = make_upgradable_command_agent(
        local_host,
        agent_id,
        agent_name,
        tmp_path,
        temp_mngr_ctx,
        upgrade_unsupported_detail=None,
        upgraded_script_content=_NEW_SCRIPT,
        build_error_message=None,
        agent_class=UpgradableCommandAgent,
    )

    outcome = upgrade_agent(
        agent, local_host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx
    )

    assert isinstance(outcome, AgentUpgradeFailed)
    assert outcome.step == UpgradeFailedStep.SWAP
    assert "File exists" in outcome.error_message
    assert "mv -f" not in outcome.error_message
    assert format_upgrade_outcome(outcome).endswith("; left stopped; some files may already be replaced")
    assert (state_dir / "commands").read_text() == "not a directory 48213\n"
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()
    assert agent.get_lifecycle_state() == AgentLifecycleState.STOPPED


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_that_cannot_swap_the_new_files_into_a_running_agent_leaves_it_stopped(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
) -> None:
    agent_name = "upgrade-swap-fails-running-48213"
    session_name = create_test_agent_via_cli(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 948221"
    )
    with tmux_session_cleanup(session_name):
        agent = _find_cli_agent(local_host, agent_name, build_error_message=None, agent_class=UpgradableCommandAgent)
        state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)
        discovery_event_count_before = len(_read_discovery_event_lines(agent.mngr_ctx))
        # A file where the upgraded files' directory belongs, so the swap cannot create that directory.
        (state_dir / "commands").rename(state_dir / "commands-moved-aside-48213")
        (state_dir / "commands").write_text("not a directory 948221\n")

        outcome = upgrade_agent(
            agent, local_host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=agent.mngr_ctx
        )

        assert isinstance(outcome, AgentUpgradeFailed)
        assert outcome.step == UpgradeFailedStep.SWAP
        assert not tmux_session_exists(session_name)
        assert agent.get_lifecycle_state() == AgentLifecycleState.STOPPED
        assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()
        assert _is_discovery_event_for_agent_emitted_since(agent, discovery_event_count_before)


@pytest.mark.tmux
def test_upgrade_skips_an_agent_destroyed_after_it_was_listed_without_recreating_its_state_dir(
    local_host: Host, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    agent = _make_stopped_agent_with_old_files(local_host, tmp_path, temp_mngr_ctx, build_error_message=None)
    state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)
    local_host.destroy_agent(agent)

    outcome = upgrade_agent(
        agent, local_host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx
    )

    assert outcome == make_skipped_outcome(agent, SKIP_DESTROYED)
    assert format_upgrade_outcome(outcome) == f"skipped {agent.name}: {SKIP_DESTROYED.detail}"
    assert not state_dir.exists()


@pytest.mark.rsync
def test_upgrade_skips_an_agent_destroyed_after_its_files_were_staged_and_leaves_nothing_behind(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = make_local_host_of_class(local_provider, _DestroyingAtStopHost)
    agent = _with_lifecycle_states(
        make_recorded_upgradable_agent(host, tmp_path, temp_mngr_ctx, {}, _NEW_SCRIPT, ScriptedStateCommandAgent),
        (AgentLifecycleState.WAITING,),
    )
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    outcome = upgrade_agent(agent, host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx)

    assert outcome == make_skipped_outcome(agent, SKIP_DESTROYED)
    assert host.is_lock_held_at_stop == [True]
    assert host.is_lock_held_at_start == []
    assert not state_dir.exists()


@pytest.mark.rsync
@pytest.mark.parametrize("is_dry_run", [False, True], ids=["real_run", "dry_run"])
def test_upgrade_skips_an_agent_destroyed_before_its_files_were_staged_and_removes_the_state_dir_the_staging_recreated(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext, is_dry_run: bool
) -> None:
    host = make_local_host_of_class(local_provider, _DestroyingAtStagingHost)
    agent = _with_lifecycle_states(
        make_recorded_upgradable_agent(host, tmp_path, temp_mngr_ctx, {}, _NEW_SCRIPT, ScriptedStateCommandAgent),
        (AgentLifecycleState.STOPPED,),
    )
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    outcome = upgrade_agent(
        agent, host, is_forced=False, is_dry_run=is_dry_run, is_caller=False, mngr_ctx=temp_mngr_ctx
    )

    assert outcome == AgentUpgradeSkipped(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(agent.get_host()),
        kind=SKIP_DESTROYED.kind,
        detail=SKIP_DESTROYED.detail,
        is_dry_run=is_dry_run,
    )
    assert not state_dir.exists()


@pytest.mark.rsync
@pytest.mark.parametrize("is_dry_run", [False, True], ids=["real_run", "dry_run"])
def test_upgrade_skips_an_agent_destroyed_right_after_its_files_were_staged_rather_than_failing_to_compare_them(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext, is_dry_run: bool
) -> None:
    host = make_local_host_of_class(local_provider, _DestroyingAfterStagingHost)
    agent = _with_lifecycle_states(
        make_recorded_upgradable_agent(host, tmp_path, temp_mngr_ctx, {}, _NEW_SCRIPT, ScriptedStateCommandAgent),
        (AgentLifecycleState.WAITING,),
    )
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    outcome = upgrade_agent(
        agent, host, is_forced=False, is_dry_run=is_dry_run, is_caller=False, mngr_ctx=temp_mngr_ctx
    )

    assert outcome == AgentUpgradeSkipped(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(agent.get_host()),
        kind=SKIP_DESTROYED.kind,
        detail=SKIP_DESTROYED.detail,
        is_dry_run=is_dry_run,
    )
    assert host.is_lock_held_at_stop == []
    assert not state_dir.exists()


@pytest.mark.parametrize("is_dry_run", [False, True], ids=["real_run", "dry_run"])
def test_upgrade_skips_an_agent_destroyed_as_its_inputs_are_read_rather_than_reporting_it_unsupported(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext, is_dry_run: bool
) -> None:
    host = make_local_host_of_class(
        local_provider, _DestroyingAtHostCommandHost, destroy_before_command_containing="MNGR_UPGRADE_INPUT_PRESENT"
    )
    agent = _with_lifecycle_states(
        make_recorded_upgradable_agent(host, tmp_path, temp_mngr_ctx, {}, _NEW_SCRIPT, _RecordRequiringCommandAgent),
        (AgentLifecycleState.WAITING,),
    )
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    outcome = upgrade_agent(
        agent, host, is_forced=False, is_dry_run=is_dry_run, is_caller=False, mngr_ctx=temp_mngr_ctx
    )

    assert outcome == AgentUpgradeSkipped(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(agent.get_host()),
        kind=SKIP_DESTROYED.kind,
        detail=SKIP_DESTROYED.detail,
        is_dry_run=is_dry_run,
    )
    assert host.is_lock_held_at_stop == []
    assert not state_dir.exists()


@pytest.mark.rsync
def test_upgrade_skips_an_agent_destroyed_as_it_is_started_again_rather_than_failing_the_start(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = make_local_host_of_class(local_provider, _DestroyingAtStartHost)
    agent = _with_lifecycle_states(
        make_recorded_upgradable_agent(host, tmp_path, temp_mngr_ctx, {}, _NEW_SCRIPT, ScriptedStateCommandAgent),
        (AgentLifecycleState.WAITING,),
    )
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    outcome = upgrade_agent(agent, host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx)

    assert outcome == make_skipped_outcome(agent, SKIP_DESTROYED)
    assert host.is_lock_held_at_stop == [True]
    assert not state_dir.exists()


@pytest.mark.rsync
@pytest.mark.parametrize("is_dry_run", [False, True], ids=["real_run", "dry_run"])
def test_upgrade_skips_an_agent_destroyed_before_its_start_check_rather_than_failing_the_check(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext, is_dry_run: bool
) -> None:
    host = make_local_host_of_class(local_provider, _DestroyingAtStartCheckHost)
    agent = _with_lifecycle_states(
        make_recorded_upgradable_agent(host, tmp_path, temp_mngr_ctx, {}, _NEW_SCRIPT, ScriptedStateCommandAgent),
        (AgentLifecycleState.WAITING,),
    )
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    outcome = upgrade_agent(
        agent, host, is_forced=False, is_dry_run=is_dry_run, is_caller=False, mngr_ctx=temp_mngr_ctx
    )

    assert outcome == AgentUpgradeSkipped(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(agent.get_host()),
        kind=SKIP_DESTROYED.kind,
        detail=SKIP_DESTROYED.detail,
        is_dry_run=is_dry_run,
    )
    assert host.is_lock_held_at_stop == []
    assert host.is_lock_held_at_start == []
    assert not state_dir.exists()


@pytest.mark.rsync
def test_upgrade_skips_an_agent_destroyed_just_before_the_swap_without_recreating_its_state_dir(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = make_local_host_of_class(
        local_provider, _DestroyingAtHostCommandHost, destroy_before_command_containing="mv -f"
    )
    agent = _with_lifecycle_states(
        make_recorded_upgradable_agent(host, tmp_path, temp_mngr_ctx, {}, _NEW_SCRIPT, ScriptedStateCommandAgent),
        (AgentLifecycleState.WAITING,),
    )
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    outcome = upgrade_agent(agent, host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx)

    assert outcome == make_skipped_outcome(agent, SKIP_DESTROYED)
    assert host.is_lock_held_at_stop == [True]
    assert host.is_lock_held_at_start == []
    assert not state_dir.exists()


@pytest.mark.rsync
def test_upgrade_skips_an_agent_whose_state_cannot_be_read(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = make_local_host_of_class(local_provider, _UnreadableLifecycleHost)
    agent = _make_stopped_agent_with_old_files(host, tmp_path, temp_mngr_ctx, build_error_message=None)
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    outcome = upgrade_agent(agent, host, is_forced=True, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx)

    assert outcome == make_skipped_outcome(agent, SKIP_STATE_UNREADABLE)
    assert (state_dir / "commands" / UPGRADED_SCRIPT_NAME).read_text() == _OLD_SCRIPT
    assert (state_dir / READY_MARKER_NAME).exists()
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_restarts_an_idle_agent_on_its_new_files(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    local_provider: LocalProviderInstance,
) -> None:
    agent_name = "upgrade-idle-48213"
    session_name = create_test_agent_via_cli(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 948213"
    )
    with tmux_session_cleanup(session_name):
        host = make_local_host_of_class(local_provider, _StartRecordingHost)
        agent = _find_cli_agent(host, agent_name, build_error_message=None, agent_class=UpgradableCommandAgent)
        pane_pid_before = read_tmux_pane_pid(session_name)
        discovery_event_count_before = len(_read_discovery_event_lines(agent.mngr_ctx))

        outcome = upgrade_agent(
            agent, host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=agent.mngr_ctx
        )

        assert outcome == AgentUpgraded(
            agent_id=agent.id,
            agent_name=agent.name,
            host=make_upgrade_outcome_host(agent.get_host()),
            plan=UpgradePlan.RESTART_IDLE,
            prior_state=AgentLifecycleState.WAITING,
            changed_paths=(_UPGRADED_SCRIPT_PATH,),
            is_dry_run=False,
        )
        assert host.scripts_at_start == [_NEW_SCRIPT]
        state_dir = get_agent_state_dir_path(host.host_dir, agent.id)
        assert (state_dir / "commands" / UPGRADED_SCRIPT_NAME).read_text() == _NEW_SCRIPT
        assert tmux_session_exists(session_name)
        assert read_tmux_pane_pid(session_name) != pane_pid_before
        assert _is_discovery_event_for_agent_emitted_since(agent, discovery_event_count_before)


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_stops_and_starts_an_agent_under_the_host_lock(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    local_provider: LocalProviderInstance,
) -> None:
    agent_name = "upgrade-locked-48213"
    session_name = create_test_agent_via_cli(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 948216"
    )
    with tmux_session_cleanup(session_name):
        host = make_local_host_of_class(local_provider, LockRecordingHost)
        agent = _find_cli_agent(host, agent_name, build_error_message=None, agent_class=UpgradableCommandAgent)

        outcome = upgrade_agent(
            agent, host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=agent.mngr_ctx
        )

        assert outcome == AgentUpgraded(
            agent_id=agent.id,
            agent_name=agent.name,
            host=make_upgrade_outcome_host(agent.get_host()),
            plan=UpgradePlan.RESTART_IDLE,
            prior_state=AgentLifecycleState.WAITING,
            changed_paths=(_UPGRADED_SCRIPT_PATH,),
            is_dry_run=False,
        )
        assert host.lock_timeouts == [START_HOST_LOCK_TIMEOUT_SECONDS]
        assert host.is_lock_held_at_stop == [True]
        assert host.is_lock_held_at_start == [True]
        assert host.is_currently_locked is False


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_removes_a_stale_ready_marker_and_reports_an_agent_that_never_signals_ready_again_as_failed(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
) -> None:
    agent_name = "upgrade-never-ready-48213"
    session_name = create_test_agent_via_cli(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 948217"
    )
    with tmux_session_cleanup(session_name):
        agent = _find_cli_agent(local_host, agent_name, build_error_message=None, agent_class=NeverReadyCommandAgent)
        state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)
        # The marker the previous run left, which would satisfy the wait if the upgrade kept it.
        (state_dir / READY_MARKER_NAME).touch()

        outcome = upgrade_agent(
            agent, local_host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=agent.mngr_ctx
        )

        assert outcome == AgentUpgradeFailed(
            agent_id=agent.id,
            agent_name=agent.name,
            host=make_upgrade_outcome_host(agent.get_host()),
            step=UpgradeFailedStep.READY,
            error_message=(
                f"Failed to start agent {agent.name}: "
                f"{READY_MARKER_NAME} did not appear within {NEVER_READY_TIMEOUT_SECONDS}s"
            ),
        )
        assert format_upgrade_outcome(outcome).endswith(f"; restart it with `mngr start {agent.name} --restart`")
        assert not (state_dir / READY_MARKER_NAME).exists()
        assert (state_dir / "commands" / UPGRADED_SCRIPT_NAME).read_text() == _NEW_SCRIPT
        assert tmux_session_exists(session_name)


@pytest.mark.tmux
def test_upgrade_that_cannot_write_the_new_files_never_stops_a_running_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
) -> None:
    agent_name = "upgrade-build-fails-48213"
    session_name = create_test_agent_via_cli(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 948214"
    )
    with tmux_session_cleanup(session_name):
        agent = _find_cli_agent(
            local_host, agent_name, build_error_message="cannot build the hooks", agent_class=UpgradableCommandAgent
        )
        pane_pid_before = read_tmux_pane_pid(session_name)

        outcome = upgrade_agent(
            agent, local_host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=agent.mngr_ctx
        )

        assert outcome == AgentUpgradeFailed(
            agent_id=agent.id,
            agent_name=agent.name,
            host=make_upgrade_outcome_host(agent.get_host()),
            step=UpgradeFailedStep.PREPARE,
            error_message="cannot build the hooks",
        )
        state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)
        assert not (state_dir / "commands" / UPGRADED_SCRIPT_NAME).exists()
        assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()
        assert read_tmux_pane_pid(session_name) == pane_pid_before


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_of_an_agent_whose_work_dir_is_gone_fails_before_stopping_it(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
) -> None:
    agent_name = "upgrade-start-check-fails-48213"
    session_name = create_test_agent_via_cli(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 948220"
    )
    with tmux_session_cleanup(session_name):
        agent = _find_cli_agent(local_host, agent_name, build_error_message=None, agent_class=UpgradableCommandAgent)
        pane_pid_before = read_tmux_pane_pid(session_name)
        # A start refuses to launch an agent whose work dir is gone.
        temp_work_dir.rename(temp_work_dir.with_name("moved-away-48213"))
        expected_outcome = AgentUpgradeFailed(
            agent_id=agent.id,
            agent_name=agent.name,
            host=make_upgrade_outcome_host(agent.get_host()),
            step=UpgradeFailedStep.START_CHECK,
            error_message=(
                f"Failed to start agent {agent_name}: "
                f"Work directory {temp_work_dir} does not exist and no branch is recorded"
            ),
        )

        dry_run_outcome = upgrade_agent(
            agent, local_host, is_forced=False, is_dry_run=True, is_caller=False, mngr_ctx=agent.mngr_ctx
        )
        outcome = upgrade_agent(
            agent, local_host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=agent.mngr_ctx
        )

        assert dry_run_outcome == expected_outcome
        assert outcome == expected_outcome
        assert format_upgrade_outcome(outcome).endswith("; untouched")
        state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)
        assert not (state_dir / "commands" / UPGRADED_SCRIPT_NAME).exists()
        assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()
        assert read_tmux_pane_pid(session_name) == pane_pid_before


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_whose_restart_fails_after_the_session_is_created_leaves_the_agent_on_the_new_files(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
) -> None:
    agent_name = "upgrade-start-fails-48213"
    session_name = create_test_agent_via_cli(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 948222"
    )
    with tmux_session_cleanup(session_name):
        agent = _find_cli_agent(local_host, agent_name, build_error_message=None, agent_class=UpgradableCommandAgent)
        state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)
        discovery_event_count_before = len(_read_discovery_event_lines(agent.mngr_ctx))
        # A directory where the start writes the launch script, which it does after creating the session.
        (state_dir / "launch_agent.sh").unlink()
        (state_dir / "launch_agent.sh").mkdir()

        outcome = upgrade_agent(
            agent, local_host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=agent.mngr_ctx
        )

        assert isinstance(outcome, AgentUpgradeFailed)
        assert outcome.step == UpgradeFailedStep.START
        assert "Is a directory" in outcome.error_message
        assert format_upgrade_outcome(outcome).endswith(
            f"; not running on the new files; start it with `mngr start {agent_name} --restart`"
        )
        assert (state_dir / "commands" / UPGRADED_SCRIPT_NAME).read_text() == _NEW_SCRIPT
        assert tmux_session_exists(session_name)
        assert agent.get_lifecycle_state() == AgentLifecycleState.DONE
        assert _is_discovery_event_for_agent_emitted_since(agent, discovery_event_count_before)


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_that_cannot_stop_an_agent_leaves_its_files_and_under_abort_skips_the_rest(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    local_provider: LocalProviderInstance,
) -> None:
    agent_name = "upgrade-stop-fails-48213"
    session_name = create_test_agent_via_cli(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 948218"
    )
    with tmux_session_cleanup(session_name):
        host = make_local_host_of_class(local_provider, _StopFailingHost)
        agent = _find_cli_agent(host, agent_name, build_error_message=None, agent_class=UpgradableCommandAgent)
        discovery_event_count_before = len(_read_discovery_event_lines(agent.mngr_ctx))
        matches = [
            AgentMatch(
                agent_id=match_id,
                agent_name=match_name,
                host_id=host.id,
                host_name=host.host_name,
                provider_name=LOCAL_PROVIDER_NAME,
            )
            for match_id, match_name in ((agent.id, agent.name), (AgentId.generate(), AgentName("later-48213")))
        ]

        outcomes = iter_upgrade_outcomes(
            matches,
            lambda match: upgrade_matched_agent(
                match,
                ReachedHost(host=host, agent_by_id={agent.id: agent}),
                is_forced=False,
                is_dry_run=False,
                is_caller=False,
                mngr_ctx=agent.mngr_ctx,
            ),
            ErrorBehavior.ABORT,
            is_dry_run=False,
        )

        assert [format_upgrade_outcome(outcome) for outcome in outcomes] == [
            f"failed {agent_name}: stop ([LOCAL_STATE_REMAINS] tmux kill-session failed); "
            "files unchanged; the agent may be stopped",
            f"skipped later-48213: {SKIP_ABORTED.detail}",
        ]
        state_dir = get_agent_state_dir_path(host.host_dir, agent.id)
        assert not (state_dir / "commands" / UPGRADED_SCRIPT_NAME).exists()
        assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()
        assert _is_discovery_event_for_agent_emitted_since(agent, discovery_event_count_before)


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_skips_an_agent_on_a_dialog_unless_forced(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
) -> None:
    agent_name = "upgrade-dialog-48213"
    session_name = create_test_agent_via_cli(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 948219"
    )
    with tmux_session_cleanup(session_name):
        agent = _find_cli_agent(local_host, agent_name, build_error_message=None, agent_class=UpgradableCommandAgent)
        state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)
        (state_dir / "active").touch()
        (state_dir / DIALOG_MARKER_NAME).touch()
        pane_pid_before = read_tmux_pane_pid(session_name)

        skipped_outcome = upgrade_agent(
            agent, local_host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=agent.mngr_ctx
        )

        assert skipped_outcome == make_skipped_outcome(agent, SKIP_ON_DIALOG)
        assert not (state_dir / "commands" / UPGRADED_SCRIPT_NAME).exists()
        assert read_tmux_pane_pid(session_name) == pane_pid_before

        forced_outcome = upgrade_agent(
            agent, local_host, is_forced=True, is_dry_run=False, is_caller=False, mngr_ctx=agent.mngr_ctx
        )

        assert forced_outcome == AgentUpgraded(
            agent_id=agent.id,
            agent_name=agent.name,
            host=make_upgrade_outcome_host(agent.get_host()),
            plan=UpgradePlan.RESTART_INTERRUPTING,
            prior_state=AgentLifecycleState.WAITING,
            changed_paths=(_UPGRADED_SCRIPT_PATH,),
            is_dry_run=False,
        )
        assert (state_dir / "commands" / UPGRADED_SCRIPT_NAME).read_text() == _NEW_SCRIPT
        assert read_tmux_pane_pid(session_name) != pane_pid_before


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_skips_an_agent_whose_program_exited_and_leaves_it_untouched_unless_forced(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    local_provider: LocalProviderInstance,
) -> None:
    agent_name = "upgrade-exited-48213"
    session_name = create_test_agent_via_cli(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 0.948224"
    )
    with tmux_session_cleanup(session_name):
        host = make_local_host_of_class(local_provider, _StartRecordingHost)
        agent = _find_cli_agent(host, agent_name, build_error_message=None, agent_class=UpgradableCommandAgent)
        wait_for(
            lambda: agent.get_lifecycle_state() == AgentLifecycleState.DONE,
            timeout=15.0,
            error_message=f"Expected {agent_name}'s command to exit",
        )
        state_dir = get_agent_state_dir_path(host.host_dir, agent.id)
        script_path = state_dir / _UPGRADED_SCRIPT_PATH
        script_path.write_text(_OLD_SCRIPT)
        (state_dir / READY_MARKER_NAME).touch()
        pane_pid_before = read_tmux_pane_pid(session_name)
        discovery_event_count_before = len(_read_discovery_event_lines(agent.mngr_ctx))

        skipped_outcome = upgrade_agent(
            agent, host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=agent.mngr_ctx
        )

        assert skipped_outcome == make_skipped_outcome(agent, SKIP_EXITED)
        assert format_upgrade_outcome(skipped_outcome) == (
            f"skipped {agent_name}: its program has exited (--force restarts it)"
        )
        assert script_path.read_text() == _OLD_SCRIPT
        assert (state_dir / READY_MARKER_NAME).exists()
        assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()
        assert read_tmux_pane_pid(session_name) == pane_pid_before
        assert agent.get_lifecycle_state() == AgentLifecycleState.DONE
        assert host.scripts_at_start == []
        assert not _is_discovery_event_for_agent_emitted_since(agent, discovery_event_count_before)

        forced_outcome = upgrade_agent(
            agent, host, is_forced=True, is_dry_run=False, is_caller=False, mngr_ctx=agent.mngr_ctx
        )

        assert forced_outcome == AgentUpgraded(
            agent_id=agent.id,
            agent_name=agent.name,
            host=make_upgrade_outcome_host(agent.get_host()),
            plan=UpgradePlan.RESTART_INTERRUPTING,
            prior_state=AgentLifecycleState.DONE,
            changed_paths=(_UPGRADED_SCRIPT_PATH,),
            is_dry_run=False,
        )
        assert format_upgrade_outcome(forced_outcome) == f"upgraded {agent_name} (restarted; its program had exited)"
        assert host.scripts_at_start == [_NEW_SCRIPT]
        assert script_path.read_text() == _NEW_SCRIPT
        assert not (state_dir / READY_MARKER_NAME).exists()
        assert read_tmux_pane_pid(session_name) != pane_pid_before


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_skips_a_just_started_agent_whose_shell_has_not_run_its_launch_command_and_lets_it_start(
    local_host: Host, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    agent = make_recorded_upgradable_agent(
        local_host, tmp_path, temp_mngr_ctx, {"command": "sleep 948230"}, _NEW_SCRIPT, UpgradableCommandAgent
    )
    state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)
    # The pane's shell sources the agent's env file before it reads the launch command, and waits here for a writer.
    launch_gate_path = tmp_path / "launch-gate-948230"
    os.mkfifo(launch_gate_path)
    local_host.get_agent_env_path(agent).write_text(f"read -r _ < {shlex.quote(str(launch_gate_path))}\n")
    with tmux_session_cleanup(agent.session_name):
        local_host.start_agents([agent.id])
        wait_for(
            lambda: agent.get_lifecycle_state() == AgentLifecycleState.DONE,
            error_message=f"Expected {agent.name}'s pane to hold only its shell",
        )
        pane_pid_before = read_tmux_pane_pid(agent.session_name)

        outcome = upgrade_agent(
            agent, local_host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx
        )

        assert outcome == make_skipped_outcome(agent, SKIP_EXITED)
        assert not (state_dir / _UPGRADED_SCRIPT_PATH).exists()
        assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()
        assert read_tmux_pane_pid(agent.session_name) == pane_pid_before

        wait_for(
            lambda: _is_line_written_to_the_reader_of(launch_gate_path),
            error_message=f"Expected {agent.name}'s shell to be waiting at the launch gate",
        )
        wait_for(
            lambda: agent.get_lifecycle_state() == AgentLifecycleState.WAITING,
            error_message=f"Expected {agent.name} to go on to run its launch command",
        )
        assert read_tmux_pane_pid(agent.session_name) == pane_pid_before


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_finds_an_agent_whose_program_exited_current_when_its_files_match_and_leaves_its_session(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
) -> None:
    agent_name = "upgrade-exited-current-48213"
    session_name = create_test_agent_via_cli(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 0.948227"
    )
    with tmux_session_cleanup(session_name):
        agent = _find_cli_agent(local_host, agent_name, build_error_message=None, agent_class=UpgradableCommandAgent)
        wait_for(
            lambda: agent.get_lifecycle_state() == AgentLifecycleState.DONE,
            timeout=15.0,
            error_message=f"Expected {agent_name}'s command to exit",
        )
        script_path = get_agent_state_dir_path(local_host.host_dir, agent.id) / _UPGRADED_SCRIPT_PATH
        script_path.write_text(_NEW_SCRIPT)
        script_path.chmod(0o755)
        pane_pid_before = read_tmux_pane_pid(session_name)

        outcome = upgrade_agent(
            agent, local_host, is_forced=True, is_dry_run=False, is_caller=False, mngr_ctx=agent.mngr_ctx
        )

        assert outcome == AgentCurrent(
            agent_id=agent.id,
            agent_name=agent.name,
            host=make_upgrade_outcome_host(agent.get_host()),
            is_dry_run=False,
        )
        assert read_tmux_pane_pid(session_name) == pane_pid_before
        assert agent.get_lifecycle_state() == AgentLifecycleState.DONE


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_skips_a_busy_agent_unless_forced(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    local_host: Host,
) -> None:
    agent_name = "upgrade-busy-48213"
    session_name = create_test_agent_via_cli(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 948215"
    )
    with tmux_session_cleanup(session_name):
        agent = _find_cli_agent(local_host, agent_name, build_error_message=None, agent_class=UpgradableCommandAgent)
        state_dir = get_agent_state_dir_path(local_host.host_dir, agent.id)
        (state_dir / "active").touch()
        pane_pid_before = read_tmux_pane_pid(session_name)

        skipped_outcome = upgrade_agent(
            agent, local_host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=agent.mngr_ctx
        )

        assert skipped_outcome == make_skipped_outcome(agent, SKIP_MID_TURN)
        assert not (state_dir / "commands" / UPGRADED_SCRIPT_NAME).exists()
        assert read_tmux_pane_pid(session_name) == pane_pid_before

        forced_outcome = upgrade_agent(
            agent, local_host, is_forced=True, is_dry_run=False, is_caller=False, mngr_ctx=agent.mngr_ctx
        )

        assert forced_outcome == AgentUpgraded(
            agent_id=agent.id,
            agent_name=agent.name,
            host=make_upgrade_outcome_host(agent.get_host()),
            plan=UpgradePlan.RESTART_INTERRUPTING,
            prior_state=AgentLifecycleState.RUNNING,
            changed_paths=(_UPGRADED_SCRIPT_PATH,),
            is_dry_run=False,
        )
        assert (state_dir / "commands" / UPGRADED_SCRIPT_NAME).read_text() == _NEW_SCRIPT
        assert read_tmux_pane_pid(session_name) != pane_pid_before

        # Still busy, but with nothing left to change, so it reads as current rather than busy.
        assert agent.get_lifecycle_state() == AgentLifecycleState.RUNNING
        assert upgrade_agent(
            agent, local_host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=agent.mngr_ctx
        ) == (
            AgentCurrent(
                agent_id=agent.id,
                agent_name=agent.name,
                host=make_upgrade_outcome_host(agent.get_host()),
                is_dry_run=False,
            )
        )


@pytest.mark.rsync
@pytest.mark.parametrize(
    ("state_just_before_stop", "expected_skip"),
    [
        pytest.param(AgentLifecycleState.RUNNING, SKIP_MID_TURN, id="took_work"),
        pytest.param(AgentLifecycleState.DONE, SKIP_EXITED, id="exited"),
        pytest.param(AgentLifecycleState.UNKNOWN, SKIP_STATE_UNREADABLE, id="state_unreadable"),
    ],
)
def test_upgrade_skips_an_idle_agent_that_left_idle_just_before_its_stop_by_its_new_state(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    state_just_before_stop: AgentLifecycleState,
    expected_skip: UpgradeSkip,
) -> None:
    host = make_local_host_of_class(local_provider, LockRecordingHost)
    agent = _with_lifecycle_states(
        make_recorded_upgradable_agent(host, tmp_path, temp_mngr_ctx, {}, _NEW_SCRIPT, ScriptedStateCommandAgent),
        (AgentLifecycleState.WAITING, state_just_before_stop),
    )
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    outcome = upgrade_agent(agent, host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx)

    assert outcome == make_skipped_outcome(agent, expected_skip)
    assert host.is_lock_held_at_stop == []
    assert host.is_lock_held_at_start == []
    assert not (state_dir / _UPGRADED_SCRIPT_PATH).exists()
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()


@pytest.mark.rsync
@pytest.mark.parametrize(
    ("is_forced", "state_just_before_stop", "expected_plan"),
    [
        pytest.param(True, AgentLifecycleState.RUNNING, UpgradePlan.RESTART_INTERRUPTING, id="took_work_under_force"),
        pytest.param(False, AgentLifecycleState.STOPPED, UpgradePlan.LEAVE_STOPPED, id="stopped"),
    ],
)
def test_upgrade_applies_the_plan_for_the_state_an_idle_agent_is_in_just_before_its_stop(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    is_forced: bool,
    state_just_before_stop: AgentLifecycleState,
    expected_plan: UpgradePlan,
) -> None:
    host = make_local_host_of_class(local_provider, LockRecordingHost)
    agent = _with_lifecycle_states(
        make_recorded_upgradable_agent(host, tmp_path, temp_mngr_ctx, {}, _NEW_SCRIPT, ScriptedStateCommandAgent),
        (AgentLifecycleState.WAITING, state_just_before_stop),
    )
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    outcome = upgrade_agent(
        agent, host, is_forced=is_forced, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx
    )

    assert outcome == AgentUpgraded(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(agent.get_host()),
        plan=expected_plan,
        prior_state=state_just_before_stop,
        changed_paths=_ALL_STAGED_PATHS,
        is_dry_run=False,
    )
    is_restarted = expected_plan != UpgradePlan.LEAVE_STOPPED
    assert host.is_lock_held_at_stop == ([True] if is_restarted else [])
    assert host.is_lock_held_at_start == ([True] if is_restarted else [])
    assert (state_dir / _UPGRADED_SCRIPT_PATH).read_text() == _NEW_SCRIPT


@pytest.mark.rsync
def test_upgrade_waits_for_a_send_in_flight_and_skips_an_idle_agent_whose_turn_the_send_started(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = make_local_host_of_class(local_provider, LockRecordingHost)
    agent = make_recorded_upgradable_agent(host, tmp_path, temp_mngr_ctx, {}, _NEW_SCRIPT, _TurnMarkerCommandAgent)
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    with _message_lock_held_by_a_send(agent, is_turn_started_by_send=True):
        outcome = upgrade_agent(
            agent, host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx
        )

    assert outcome == make_skipped_outcome(agent, SKIP_MID_TURN)
    assert agent.message_lock_timeouts == [UPGRADE_MESSAGE_LOCK_TIMEOUT_SECONDS]
    assert host.is_lock_held_at_stop == []
    assert host.is_lock_held_at_start == []
    assert not (state_dir / _UPGRADED_SCRIPT_PATH).exists()
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()


@pytest.mark.rsync
def test_upgrade_waits_for_a_send_in_flight_and_restarts_an_agent_still_idle_after_it(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = make_local_host_of_class(local_provider, LockRecordingHost)
    agent = make_recorded_upgradable_agent(host, tmp_path, temp_mngr_ctx, {}, _NEW_SCRIPT, _TurnMarkerCommandAgent)
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    with _message_lock_held_by_a_send(agent, is_turn_started_by_send=False):
        outcome = upgrade_agent(
            agent, host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx
        )

    assert outcome == AgentUpgraded(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(agent.get_host()),
        plan=UpgradePlan.RESTART_IDLE,
        prior_state=AgentLifecycleState.WAITING,
        changed_paths=_ALL_STAGED_PATHS,
        is_dry_run=False,
    )
    assert agent.message_lock_timeouts == [UPGRADE_MESSAGE_LOCK_TIMEOUT_SECONDS]
    assert host.is_lock_held_at_stop == [True]
    assert host.is_lock_held_at_start == [True]
    assert (state_dir / _UPGRADED_SCRIPT_PATH).read_text() == _NEW_SCRIPT


@pytest.mark.rsync
def test_upgrade_that_waits_out_its_bound_for_a_send_in_flight_fails_at_the_lock_step_and_leaves_the_agent_untouched(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = make_local_host_of_class(local_provider, LockRecordingHost)
    recorded_agent = make_recorded_upgradable_agent(
        host, tmp_path, temp_mngr_ctx, {}, _NEW_SCRIPT, _TurnMarkerCommandAgent
    )
    agent = recorded_agent.model_copy_update(
        to_update(recorded_agent.field_ref().message_lock_wait_seconds, _SHORT_MESSAGE_LOCK_WAIT_SECONDS)
    )
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    with file_lock_held_by_another_process(state_dir / _MESSAGE_LOCK_NAME, temp_mngr_ctx.concurrency_group):
        files_before = _read_state_dir_files(state_dir)
        outcome = upgrade_agent(
            agent, host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx
        )
        files_after = _read_state_dir_files(state_dir)

    assert outcome == AgentUpgradeFailed(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(agent.get_host()),
        step=UpgradeFailedStep.LOCK,
        error_message=str(MessageLockTimeoutError(str(agent.name), _SHORT_MESSAGE_LOCK_WAIT_SECONDS)),
    )
    assert format_upgrade_outcome(outcome).endswith("; untouched")
    assert agent.message_lock_timeouts == [UPGRADE_MESSAGE_LOCK_TIMEOUT_SECONDS]
    assert host.is_lock_held_at_stop == []
    assert host.is_lock_held_at_start == []
    assert files_after == files_before


@pytest.mark.rsync
@pytest.mark.parametrize(
    "states_read_before_the_loss",
    [
        pytest.param((), id="first_state_read"),
        pytest.param((AgentLifecycleState.WAITING,), id="state_read_again_before_the_stop"),
    ],
)
def test_upgrade_whose_host_lock_is_lost_to_another_mngr_before_it_changed_the_agent_fails_at_the_lock_step_untouched(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    states_read_before_the_loss: tuple[AgentLifecycleState, ...],
) -> None:
    host = make_local_host_of_class(local_provider, LockRecordingHost)
    agent = _with_lifecycle_states(
        make_recorded_upgradable_agent(host, tmp_path, temp_mngr_ctx, {}, _NEW_SCRIPT, _LockLosingStateCommandAgent),
        states_read_before_the_loss,
    )
    state_dir = get_agent_state_dir_path(host.host_dir, agent.id)

    outcome = upgrade_agent(agent, host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx)

    assert outcome == AgentUpgradeFailed(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(host),
        step=UpgradeFailedStep.LOCK,
        error_message=_LOCK_LOST_AT_STATE_READ_MESSAGE,
    )
    assert format_upgrade_outcome(outcome).endswith("; untouched")
    assert host.is_lock_held_at_stop == []
    assert host.is_lock_held_at_start == []
    assert not (state_dir / _UPGRADED_SCRIPT_PATH).exists()
    assert not (state_dir / UPGRADE_STAGING_DIR_NAME).exists()


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_restarts_an_idle_agent_holding_its_message_lock_through_the_stop_but_not_the_start(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    local_provider: LocalProviderInstance,
) -> None:
    agent_name = "upgrade-message-lock-48213"
    session_name = create_test_agent_via_cli(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, command="sleep 948231"
    )
    with tmux_session_cleanup(session_name):
        host = make_local_host_of_class(local_provider, _MessageLockObservingHost)
        agent = _find_cli_agent(host, agent_name, build_error_message=None, agent_class=UpgradableCommandAgent)
        pane_pid_before = read_tmux_pane_pid(session_name)

        outcome = upgrade_agent(
            agent, host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=agent.mngr_ctx
        )

        assert outcome == AgentUpgraded(
            agent_id=agent.id,
            agent_name=agent.name,
            host=make_upgrade_outcome_host(agent.get_host()),
            plan=UpgradePlan.RESTART_IDLE,
            prior_state=AgentLifecycleState.WAITING,
            changed_paths=(_UPGRADED_SCRIPT_PATH,),
            is_dry_run=False,
        )
        assert host.is_message_lock_free_at_stop == [False]
        assert host.is_message_lock_free_at_start == [True]
        assert tmux_session_exists(session_name)
        assert read_tmux_pane_pid(session_name) != pane_pid_before


@pytest.mark.rsync
@pytest.mark.parametrize(
    ("is_written_at_staging_upload", "lifecycle_state", "expected_plan", "written_settings", "expected_changed_paths"),
    [
        pytest.param(
            False,
            AgentLifecycleState.WAITING,
            UpgradePlan.RESTART_IDLE,
            {"mngr_key": "old", "fastMode": True},
            (_MERGED_SETTINGS_PATH,),
            id="another_key_written_at_the_stop",
        ),
        pytest.param(
            True,
            AgentLifecycleState.STOPPED,
            UpgradePlan.LEAVE_STOPPED,
            {"mngr_key": "old", "fastMode": True},
            (_MERGED_SETTINGS_PATH,),
            id="another_key_written_while_staging",
        ),
        pytest.param(
            False,
            AgentLifecycleState.WAITING,
            UpgradePlan.RESTART_IDLE,
            {"mngr_key": "new"},
            (),
            id="mngrs_own_new_value_written_at_the_stop",
        ),
    ],
)
def test_upgrade_keeps_a_write_another_program_makes_to_an_input_after_upgrade_first_read_it(
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    temp_mngr_ctx: MngrContext,
    is_written_at_staging_upload: bool,
    lifecycle_state: AgentLifecycleState,
    expected_plan: UpgradePlan,
    written_settings: dict[str, object],
    expected_changed_paths: tuple[Path, ...],
) -> None:
    host = make_local_host_of_class(
        local_provider,
        _SettingsWritingHost,
        written_settings=written_settings,
        is_written_at_staging_upload=is_written_at_staging_upload,
    )
    agent = _with_lifecycle_states(
        make_recorded_upgradable_agent(host, tmp_path, temp_mngr_ctx, {}, _NEW_SCRIPT, _SettingsMergingCommandAgent),
        (lifecycle_state,),
    )
    settings_path = get_agent_state_dir_path(host.host_dir, agent.id) / _MERGED_SETTINGS_PATH
    settings_path.write_text(json.dumps({"mngr_key": "old"}))

    outcome = upgrade_agent(agent, host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx)

    assert outcome == AgentUpgraded(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(agent.get_host()),
        plan=expected_plan,
        prior_state=lifecycle_state,
        changed_paths=expected_changed_paths,
        is_dry_run=False,
    )
    assert host.is_written
    assert json.loads(settings_path.read_text()) == {**written_settings, "mngr_key": "new"}
    assert host.is_lock_held_at_start == ([True] if expected_plan == UpgradePlan.RESTART_IDLE else [])


@pytest.mark.tmux
@pytest.mark.rsync
def test_upgrade_whose_host_lock_is_lost_by_the_end_fails_a_real_upgrade_at_unlock_and_keeps_any_other_outcome(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = make_local_host_of_class(local_provider, _LockLosingHost)
    agent = _make_stopped_agent_with_old_files(host, tmp_path, temp_mngr_ctx, build_error_message=None)

    dry_run_outcome = upgrade_agent(
        agent, host, is_forced=False, is_dry_run=True, is_caller=False, mngr_ctx=temp_mngr_ctx
    )
    outcome = upgrade_agent(agent, host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx)
    current_outcome = upgrade_agent(
        agent, host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx
    )

    assert isinstance(dry_run_outcome, AgentUpgraded)
    assert outcome == AgentUpgradeFailed(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(host),
        step=UpgradeFailedStep.UNLOCK,
        error_message="Host lock was lost during the critical section: acquisition counter is 8, expected 7",
    )
    assert format_upgrade_outcome(outcome).endswith(
        "; its upgrade ran, but another mngr may have changed it while the host lock was not held"
    )
    assert (get_agent_state_dir_path(host.host_dir, agent.id) / _UPGRADED_SCRIPT_PATH).read_text() == _NEW_SCRIPT
    assert current_outcome == make_current_outcome(agent)


def test_upgrade_reads_each_input_exactly_in_one_host_command(local_host: Host, tmp_path: Path) -> None:
    state_dir = tmp_path / "state-59124"
    (state_dir / "nested").mkdir(parents=True)
    contents = {
        AGENT_DATA_RELATIVE_PATH: "{}",
        Path("empty-59124"): "",
        Path("no-final-newline-59124"): "last line without a newline",
        Path("nested") / "unicode-59124.json": '{"name": "caf\u00e9 \u2713"}\n\n',
        Path("long-59124.sh"): "echo " + "x" * 300 + "\n",
    }
    for relative_path, content in contents.items():
        (state_dir / relative_path).write_text(content)
    (state_dir / "dangling-link-59124").symlink_to(state_dir / "nothing-there-59124")
    input_paths = (*contents, Path("missing-59124"), Path("dangling-link-59124"))

    result = local_host.execute_idempotent_command(build_read_upgrade_inputs_command(state_dir, input_paths))

    assert result.success, result.stderr
    assert parse_upgrade_inputs_output(result.stdout, input_paths) == {
        **contents,
        Path("missing-59124"): None,
        Path("dangling-link-59124"): None,
    }
    (state_dir / AGENT_DATA_RELATIVE_PATH).unlink()
    for missing_record_state_dir in (state_dir, tmp_path / "gone-59124"):
        record_missing_result = local_host.execute_idempotent_command(
            build_read_upgrade_inputs_command(missing_record_state_dir, input_paths)
        )
        assert parse_upgrade_inputs_output(record_missing_result.stdout, input_paths) is None


def _write_staged_files(staging_dir: Path) -> None:
    for relative_path, upgraded_file in build_shared_shell_lib_files().items():
        (staging_dir / relative_path).parent.mkdir(parents=True, exist_ok=True)
        (staging_dir / relative_path).write_text(upgraded_file.content)
    (staging_dir / "new dir").mkdir()
    (staging_dir / "new dir" / "script.sh").write_text(_NEW_SCRIPT)


def test_upgrade_swap_command_finishes_the_same_when_run_again_and_changes_nothing_once_the_record_is_gone(
    local_host: Host, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state-59124"
    staging_dir = state_dir / UPGRADE_STAGING_DIR_NAME
    host_commands_dir = tmp_path / "host-commands-59124"
    _write_staged_files(staging_dir)
    (state_dir / AGENT_DATA_RELATIVE_PATH).write_text("{}")
    (state_dir / READY_MARKER_NAME).touch()
    changed_paths = (*_SHARED_SHELL_LIB_PATHS, Path("new dir") / "script.sh")
    swap_command = build_swap_command(
        staging_dir, state_dir, changed_paths, (Path(READY_MARKER_NAME),), host_commands_dir
    )
    assert swap_command is not None

    first_result = local_host.execute_idempotent_command(swap_command)
    replayed_result = local_host.execute_idempotent_command(swap_command)

    assert (first_result.success, first_result.stdout) == (True, ""), first_result.stderr
    assert (replayed_result.success, replayed_result.stdout) == (True, ""), replayed_result.stderr
    assert (state_dir / "new dir" / "script.sh").read_text() == _NEW_SCRIPT
    assert not (state_dir / READY_MARKER_NAME).exists()
    for name in SHARED_SHELL_LIB_NAMES:
        assert (host_commands_dir / name).read_text() == (state_dir / "commands" / name).read_text()
        assert stat.S_IMODE((host_commands_dir / name).stat().st_mode) == 0o755
    assert sorted(path.name for path in host_commands_dir.iterdir()) == sorted(SHARED_SHELL_LIB_NAMES)

    destroyed_state_dir = tmp_path / "destroyed-59124"
    _write_staged_files(destroyed_state_dir / UPGRADE_STAGING_DIR_NAME)
    destroyed_swap_command = build_swap_command(
        destroyed_state_dir / UPGRADE_STAGING_DIR_NAME, destroyed_state_dir, changed_paths, (), None
    )
    assert destroyed_swap_command is not None
    destroyed_result = local_host.execute_idempotent_command(destroyed_swap_command)
    assert (destroyed_result.success, destroyed_result.stdout.strip()) == (True, "agent-record-missing")
    assert sorted(path.name for path in destroyed_state_dir.iterdir()) == [UPGRADE_STAGING_DIR_NAME]


def test_upgrade_compare_command_removes_staged_files_that_match_and_prints_nothing_when_run_again(
    local_host: Host, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state-59124"
    staging_dir = state_dir / UPGRADE_STAGING_DIR_NAME
    for commands_dir in (staging_dir / "commands", state_dir / "commands"):
        commands_dir.mkdir(parents=True)
        (commands_dir / "same.sh").write_text(_NEW_SCRIPT)
    (state_dir / AGENT_DATA_RELATIVE_PATH).write_text("{}")
    compare_command = build_compare_staged_files_command(staging_dir, state_dir)

    first_result = local_host.execute_idempotent_command(compare_command)
    replayed_result = local_host.execute_idempotent_command(compare_command)

    assert (first_result.success, first_result.stdout) == (True, "")
    assert (replayed_result.success, replayed_result.stdout) == (True, "")
    assert not staging_dir.exists()


def test_upgrade_compare_command_removes_the_staging_and_the_state_dir_it_leaves_empty_once_the_record_is_gone(
    local_host: Host, tmp_path: Path
) -> None:
    state_dir = tmp_path / "state-59124"
    staging_dir = state_dir / UPGRADE_STAGING_DIR_NAME
    (staging_dir / "commands").mkdir(parents=True)
    (staging_dir / "commands" / "new.sh").write_text(_NEW_SCRIPT)

    result = local_host.execute_idempotent_command(build_compare_staged_files_command(staging_dir, state_dir))

    assert (result.success, result.stdout.strip()) == (True, "agent-record-missing")
    assert not state_dir.exists()


def _make_stopped_host_matches(harness: PinnedLookupHarness, names: Sequence[str]) -> list[AgentMatch]:
    return [
        AgentMatch(
            agent_id=AgentId.generate(),
            agent_name=AgentName(name),
            host_id=harness.first_host_id,
            host_name=HostName("pinned-first-host"),
            provider_name=harness.first_provider.name,
        )
        for name in names
    ]


def test_upgrade_agents_skips_the_agents_of_a_stopped_or_unreachable_host_as_offline_and_upgrades_the_rest(
    pinned_lookup_harness: PinnedLookupHarness, local_host: Host, temp_work_dir: Path
) -> None:
    mngr_ctx = pinned_lookup_harness.mngr_ctx
    local_agent = create_test_agent_state(local_host, temp_work_dir, "local-59124", "command")
    stopped_matches = _make_stopped_host_matches(pinned_lookup_harness, ("stopped-a-59124", "stopped-b-59124"))
    local_match = AgentMatch(
        agent_id=local_agent.id,
        agent_name=local_agent.name,
        host_id=local_host.id,
        host_name=local_host.get_name(),
        provider_name=LOCAL_PROVIDER_NAME,
    )
    unreachable_match = AgentMatch(
        agent_id=AgentId.generate(),
        agent_name=AgentName("unreachable-59124"),
        host_id=HostId.generate(),
        host_name=HostName("nowhere-59124"),
        provider_name=pinned_lookup_harness.second_provider.name,
    )
    matches = [stopped_matches[0], local_match, unreachable_match, stopped_matches[1]]

    outcomes = list(
        upgrade_agents(
            matches,
            is_forced=False,
            is_dry_run=False,
            error_behavior=ErrorBehavior.CONTINUE,
            caller=None,
            mngr_ctx=mngr_ctx,
        )
    )

    assert [format_upgrade_outcome(outcome) for outcome in outcomes] == [
        "skipped stopped-a-59124@pinned-first-host.pinned-first: its host is stopped "
        "(`mngr start stopped-a-59124@pinned-first-host.pinned-first` starts it)",
        "current local-59124",
        "skipped unreachable-59124@nowhere-59124.pinned-second: its host could not be reached: "
        f"{HostNotFoundError(pinned_lookup_harness.second_provider.name, unreachable_match.host_id)}",
        "skipped stopped-b-59124@pinned-first-host.pinned-first: its host is stopped "
        "(`mngr start stopped-b-59124@pinned-first-host.pinned-first` starts it)",
    ]
    assert all(
        outcome.kind == UpgradeSkipKind.NOT_IDLE for outcome in outcomes if isinstance(outcome, AgentUpgradeSkipped)
    )
    assert pinned_lookup_harness.first_provider.looked_up_hosts == [pinned_lookup_harness.first_host_id]


def test_upgrade_agents_skips_an_agent_whose_host_is_destroyed_as_offline_without_a_start_command(
    make_pinned_lookup_harness: PinnedLookupHarnessFactory,
) -> None:
    harness = make_pinned_lookup_harness.build(first_host_stop_reason=HostState.DESTROYED)

    outcomes = list(
        upgrade_agents(
            _make_stopped_host_matches(harness, ("destroyed-host-59124",)),
            is_forced=False,
            is_dry_run=False,
            error_behavior=ErrorBehavior.CONTINUE,
            caller=None,
            mngr_ctx=harness.mngr_ctx,
        )
    )

    assert [format_upgrade_outcome(outcome) for outcome in outcomes] == [
        "skipped destroyed-host-59124@pinned-first-host.pinned-first: its host is destroyed"
    ]


def test_upgrade_agents_skips_the_later_agents_of_a_host_whose_connection_was_lost_without_reaching_it_again(
    pinned_lookup_harness: PinnedLookupHarness,
    local_provider: LocalProviderInstance,
    temp_work_dir: Path,
    upgradable_agent_types: None,
) -> None:
    host_of_local_provider = make_local_host_of_class(local_provider, _ConnectionLosingHost)
    host = host_of_local_provider.model_copy_update(
        to_update(host_of_local_provider.field_ref().provider_instance, pinned_lookup_harness.first_provider)
    )
    assert isinstance(host, _ConnectionLosingHost)
    pinned_lookup_harness.first_provider.mock_hosts.append(host)
    agent_names = ("lost-first-59124", "lost-second-59124")
    matches = [
        AgentMatch(
            agent_id=agent.id,
            agent_name=agent.name,
            host_id=host.id,
            host_name=host.get_name(),
            provider_name=pinned_lookup_harness.first_provider.name,
        )
        for agent in (
            create_test_agent_state(host, temp_work_dir, name, UPGRADABLE_COMMAND_AGENT_TYPE) for name in agent_names
        )
    ]

    outcomes = list(
        upgrade_agents(
            matches,
            is_forced=False,
            is_dry_run=False,
            error_behavior=ErrorBehavior.CONTINUE,
            caller=None,
            mngr_ctx=pinned_lookup_harness.mngr_ctx,
        )
    )

    assert [format_upgrade_outcome(outcome) for outcome in outcomes] == [
        f"skipped {name}@{host.get_name()}.{pinned_lookup_harness.first_provider.name}: "
        f"its host could not be reached: {_CONNECTION_LOST_MESSAGE}"
        for name in agent_names
    ]
    assert [outcome.kind for outcome in outcomes if isinstance(outcome, AgentUpgradeSkipped)] == [
        UpgradeSkipKind.NOT_IDLE
    ] * len(agent_names)
    assert host.lock_timeouts == [START_HOST_LOCK_TIMEOUT_SECONDS]
