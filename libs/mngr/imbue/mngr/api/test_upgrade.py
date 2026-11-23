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
from imbue.mngr.api.testing import UPGRADED_SCRIPT_NAME
from imbue.mngr.api.testing import UpgradableCommandAgent
from imbue.mngr.api.testing import make_recorded_upgradable_agent
from imbue.mngr.api.testing import make_skipped_outcome
from imbue.mngr.api.testing import make_upgradable_command_agent
from imbue.mngr.api.testing import write_command_agent_record
from imbue.mngr.api.upgrade import AgentCurrent
from imbue.mngr.api.upgrade import AgentUpgradeFailed
from imbue.mngr.api.upgrade import AgentUpgradeSkipped
from imbue.mngr.api.upgrade import AgentUpgraded
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
from imbue.mngr.api.upgrade import iter_upgrade_outcomes
from imbue.mngr.api.upgrade import upgrade_agent
from imbue.mngr.api.upgrade import upgrade_matched_agent
from imbue.mngr.cli.upgrade import format_upgrade_outcome
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MessageLockTimeoutError
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.host import Host
from imbue.mngr.hosts.host import SHARED_SHELL_LIB_NAMES
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.cleanup_failures import CleanupFailedGroup
from imbue.mngr.interfaces.data_types import CleanupFailure
from imbue.mngr.interfaces.data_types import CleanupFailureCategory
from imbue.mngr.interfaces.data_types import HarnessFileSpec
from imbue.mngr.interfaces.data_types import HostLifecycleSnapshot
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import ErrorBehavior
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


class _DestroyingAfterRecordCheckHost(LockRecordingHost):
    """A LockRecordingHost on which another program destroys an agent right after its upgrade found its record still there."""

    def path_exists(self, path: Path) -> bool:
        is_existing = super().path_exists(path)
        if is_existing and path.name == AGENT_DATA_RELATIVE_PATH.name:
            shutil.rmtree(path.parent)
        return is_existing


class _DestroyingAtInputReadHost(LockRecordingHost):
    """A LockRecordingHost on which another program destroys an agent just before its upgrade reads its record."""

    def read_text_file(self, path: Path, encoding: str = "utf-8") -> str:
        if path.name == AGENT_DATA_RELATIVE_PATH.name:
            shutil.rmtree(path.parent)
        return super().read_text_file(path, encoding)


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
    ) == (AgentCurrent(agent_id=agent.id, agent_name=agent.name, is_dry_run=True))
    assert upgrade_agent(
        agent, local_host, is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=temp_mngr_ctx
    ) == (AgentCurrent(agent_id=agent.id, agent_name=agent.name, is_dry_run=False))


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
    host = make_local_host_of_class(local_provider, _DestroyingAtInputReadHost)
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
        kind=SKIP_DESTROYED.kind,
        detail=SKIP_DESTROYED.detail,
        is_dry_run=is_dry_run,
    )
    assert host.is_lock_held_at_stop == []
    assert host.is_lock_held_at_start == []
    assert not state_dir.exists()


@pytest.mark.rsync
def test_upgrade_skips_an_agent_destroyed_between_its_record_check_and_the_rename_without_recreating_its_state_dir(
    local_provider: LocalProviderInstance, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = make_local_host_of_class(local_provider, _DestroyingAfterRecordCheckHost)
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
                host,
                {agent.id: agent},
                is_forced=False,
                is_dry_run=False,
                caller_agent_id=None,
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

        assert outcome == AgentCurrent(agent_id=agent.id, agent_name=agent.name, is_dry_run=False)
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
        ) == (AgentCurrent(agent_id=agent.id, agent_name=agent.name, is_dry_run=False))


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
        step=UpgradeFailedStep.LOCK,
        error_message=str(MessageLockTimeoutError(str(agent.name), _SHORT_MESSAGE_LOCK_WAIT_SECONDS)),
    )
    assert format_upgrade_outcome(outcome).endswith("; untouched")
    assert agent.message_lock_timeouts == [UPGRADE_MESSAGE_LOCK_TIMEOUT_SECONDS]
    assert host.is_lock_held_at_stop == []
    assert host.is_lock_held_at_start == []
    assert files_after == files_before


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
        plan=expected_plan,
        prior_state=lifecycle_state,
        changed_paths=expected_changed_paths,
        is_dry_run=False,
    )
    assert host.is_written
    assert json.loads(settings_path.read_text()) == {**written_settings, "mngr_key": "new"}
    assert host.is_lock_held_at_start == ([True] if expected_plan == UpgradePlan.RESTART_IDLE else [])
