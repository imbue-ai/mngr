"""Shared test fixtures for API tests."""

import json
import shlex
import shutil
import subprocess
import types
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final
from typing import TypeVar

from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.agents.default_plugins.command_agent import CommandAgent
from imbue.mngr.agents.upgrade import build_shared_shell_lib_files
from imbue.mngr.api.find import AgentMatch
from imbue.mngr.api.git import LocalGitContext
from imbue.mngr.api.upgrade import AgentCurrent
from imbue.mngr.api.upgrade import AgentUpgradeFailed
from imbue.mngr.api.upgrade import AgentUpgradeOutcome
from imbue.mngr.api.upgrade import AgentUpgradeSkipped
from imbue.mngr.api.upgrade import AgentUpgraded
from imbue.mngr.api.upgrade import ReachedHost
from imbue.mngr.api.upgrade import UpgradeFailedStep
from imbue.mngr.api.upgrade import UpgradeOutcomeHost
from imbue.mngr.api.upgrade import UpgradePlan
from imbue.mngr.api.upgrade import UpgradeSkip
from imbue.mngr.api.upgrade import UpgradeSkipKind
from imbue.mngr.api.upgrade import format_agent_label
from imbue.mngr.api.upgrade import upgrade_matched_agent
from imbue.mngr.config.agent_class_registry import register_agent_class
from imbue.mngr.config.agent_config_registry import register_agent_config
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentStartError
from imbue.mngr.errors import LockNotHeldError
from imbue.mngr.errors import PluginMngrError
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.data_types import HarnessFileSpec
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import UpgradeSkipDetail
from imbue.mngr.utils.env_utils import build_source_env_shell_commands
from imbue.mngr.utils.polling import poll_until


@contextmanager
def created_host(
    provider: ProviderInstanceInterface,
    host_name: HostName,
    **create_kwargs: Any,
) -> Generator[OnlineHostInterface, None, None]:
    """Create a host via ``provider`` and destroy it on exit.

    Replaces the create-host / ``try``-``finally``-``destroy_host`` boilerplate that
    recurs across provider tests (modal, docker, ssh) with a single ``with`` block.
    Extra keyword arguments are forwarded to ``provider.create_host``.
    """
    host = provider.create_host(host_name, **create_kwargs)
    try:
        yield host
    finally:
        provider.destroy_host(host)


class FakeAgent(FrozenModel):
    """Minimal test double for AgentInterface -- only implements work_dir and name."""

    work_dir: Path = Field(description="Working directory for this agent")
    name: AgentName = Field(default=AgentName("fake-agent"), description="Agent name")


class FakeHost(MutableModel):
    """Minimal test double for OnlineHostInterface that executes commands locally."""

    is_local: bool = Field(default=True, description="Whether this is a local host")
    host_dir: Path = Field(default_factory=lambda: Path("/fake/host_dir"), description="Host state directory")
    ssh_info: tuple[str, str, int, Path] | None = Field(
        default=None,
        description="SSH connection info (user, hostname, port, key_path) for remote hosts",
    )
    ssh_known_hosts_file: str | None = Field(
        default=None,
        description="Path to known_hosts file for SSH host key verification",
    )

    @property
    def connector(self) -> types.SimpleNamespace:
        """Provide a connector-like attribute with host data for SSH configuration."""
        data: dict[str, str] = {}
        if self.ssh_known_hosts_file is not None:
            data["ssh_known_hosts_file"] = self.ssh_known_hosts_file
        return types.SimpleNamespace(host=types.SimpleNamespace(data=data))

    def _execute_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Execute a command locally and return the result.

        The user, env, and timeout_seconds parameters are accepted for interface
        compatibility but are not applied to the subprocess call.
        """
        result = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        return CommandResult(
            stdout=result.stdout,
            stderr=result.stderr,
            success=result.returncode == 0,
        )

    def build_source_env_prefix(self, agent: Any) -> str:
        """Build the env-sourcing shell prefix exactly like a real host does."""
        host_env_path = self.host_dir / "env"
        agent_env_path = self.host_dir / "agents" / str(agent.id) / "env"
        commands = build_source_env_shell_commands(host_env_path, agent_env_path)
        return " && ".join(commands) + " && "

    def is_directory(self, path: Path) -> bool:
        """Mirror OnlineHostInterface.is_directory for the local-execution double."""
        if self.is_local:
            return path.is_dir()
        return self._execute_command(f"test -d {shlex.quote(str(path))}").success

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Execute an idempotent command locally."""
        return self._execute_command(command, user=user, cwd=cwd, env=env, timeout_seconds=timeout_seconds)

    def execute_stateful_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: Callable[[str, bool], None] | None = None,
    ) -> CommandResult:
        """Execute a stateful command locally.

        When ``on_output`` is provided, the captured stdout/stderr lines are
        replayed to it (stdout first, then stderr) so callers relying on the
        streaming contract still see their per-line callback fire.
        """
        result = self._execute_command(command, user=user, cwd=cwd, env=env, timeout_seconds=timeout_seconds)
        if on_output is not None:
            for line in result.stdout.splitlines():
                on_output(line, True)
            for line in result.stderr.splitlines():
                on_output(line, False)
        return result

    def read_text_file(self, path: Path, encoding: str = "utf-8") -> str:
        """Read a file from the local filesystem."""
        return path.read_text(encoding=encoding)

    def write_text_file(
        self,
        path: Path,
        content: str,
        encoding: str = "utf-8",
        mode: str | None = None,
        is_atomic: bool = True,
    ) -> None:
        """Write a text file to the local filesystem."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding=encoding)

    def write_file(self, path: Path, content: bytes, mode: str | None = None, is_atomic: bool = True) -> None:
        """Write a binary file to the local filesystem."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def copy_directory(
        self,
        source_host: object,
        source_path: Path,
        target_path: Path,
        extra_args: str | None = None,
        exclude_git: bool = False,
    ) -> None:
        """Copy a directory using local filesystem operations.

        FakeHost always operates on the local filesystem, so this uses
        shutil.copytree regardless of the is_local flag.
        """
        target_path.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_path, target_path, dirs_exist_ok=True)

    def copy_local_directory(self, source_path: Path, target_path: Path, extra_args: str | None = None) -> None:
        """Merge a local directory into target_path file-by-file (mimics rsync -r, additive).

        Unlike a plain copytree, this handles a ``target_path`` of "/" (the real
        upload path stages absolute remote paths and rsyncs to the filesystem root):
        each staged file lands at ``target_path / <relative>``, which reconstructs the
        intended absolute path. Ignores ``extra_args`` (include/exclude filters), like
        ``copy_directory``.
        """
        for staged in Path(source_path).rglob("*"):
            if staged.is_file():
                dest = target_path / staged.relative_to(source_path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(staged, dest)

    def get_ssh_connection_info(self) -> tuple[str, str, int, Path] | None:
        """Return configured SSH connection info, or None for local hosts."""
        if self.is_local:
            return None
        return self.ssh_info

    def get_env_var(self, key: str) -> str | None:
        """No-op env-var lookup. Tests that need env-var introspection should use a richer fake."""
        del key
        return None

    def get_env_vars(self) -> dict[str, str]:
        """No-op env-var dump. Tests that need env-var introspection should use a richer fake."""
        return {}


class SyncTestContext(FrozenModel):
    """Shared test context for sync integration tests (pull, push, pair)."""

    agent_dir: Path = Field(description="Agent working directory")
    local_dir: Path = Field(description="Local directory")
    agent: Any = Field(description="Test agent (FakeAgent)")
    host: Any = Field(description="Test host (FakeHost)")


def has_uncommitted_changes(path: Path, cg: ConcurrencyGroup) -> bool:
    """Check for uncommitted changes using LocalGitContext."""
    return LocalGitContext(cg=cg).has_uncommitted_changes(path)


UPGRADED_SCRIPT_NAME: Final[str] = "written_by_upgrade.sh"

DIALOG_MARKER_NAME: Final[str] = "dialog_open"

READY_MARKER_NAME: Final[str] = "ready_after_restart"

NEVER_READY_TIMEOUT_SECONDS: Final[float] = 0.5

UPGRADABLE_COMMAND_AGENT_TYPE: Final[str] = "upgradable-command"

UPGRADE_FAILING_COMMAND_AGENT_TYPE: Final[str] = "upgrade-failing-command"

UPGRADE_FAILURE_MESSAGE: Final[str] = "cannot build the hooks"

UPGRADE_CRASHING_COMMAND_AGENT_TYPE: Final[str] = "upgrade-crashing-command"


class UpgradeCrashError(Exception):
    """An error from an agent's build that ``mngr upgrade`` does not turn into an agent's outcome."""


class UpgradableCommandAgent(CommandAgent):
    """A command agent whose upgrade builds the shared shell libs and one script, by default ``commands/UPGRADED_SCRIPT_NAME``, or raises.

    It is on a dialog while ``DIALOG_MARKER_NAME`` exists in its state dir, and ready as soon as it restarts.
    """

    upgrade_unsupported_detail: str | None = Field(
        default=None, description="What get_upgrade_unsupported_detail returns"
    )
    upgraded_script_path: Path = Field(
        default=Path("commands") / UPGRADED_SCRIPT_NAME,
        description="Where the staged script goes, relative to the staging dir",
    )
    upgraded_script_content: str = Field(default="", description="The content of the staged script")
    build_error_message: str | None = Field(default=None, description="When set, the build raises with it")
    upgrade_input_paths: tuple[Path, ...] = Field(default=(), description="What get_upgrade_input_paths returns")
    ready_signal_paths: tuple[Path, ...] = Field(
        default=(Path(READY_MARKER_NAME),), description="What get_ready_signal_paths returns"
    )

    def get_upgrade_input_paths(self) -> tuple[Path, ...]:
        return self.upgrade_input_paths

    def get_upgrade_unsupported_detail(self, input_content_by_path: Mapping[Path, str | None]) -> str | None:
        return self.upgrade_unsupported_detail

    def is_blocked_on_dialog(self) -> bool:
        return self._check_file_exists(self._get_agent_dir() / DIALOG_MARKER_NAME)

    def build_upgraded_files(self, input_content_by_path: Mapping[Path, str | None]) -> dict[Path, HarnessFileSpec]:
        if self.build_error_message is not None:
            raise PluginMngrError(self.build_error_message)
        return {
            **build_shared_shell_lib_files(),
            self.upgraded_script_path: HarnessFileSpec(content=self.upgraded_script_content, mode="0755"),
        }

    def get_ready_signal_paths(self) -> tuple[Path, ...]:
        return self.ready_signal_paths

    def wait_for_ready_after_restart(self, timeout: float) -> None:
        return None


class ScriptedStateCommandAgent(UpgradableCommandAgent):
    """An upgradable command agent whose lifecycle state reads as each of ``lifecycle_states`` in turn, the last one from then on."""

    lifecycle_states: list[AgentLifecycleState] = Field(
        default_factory=list, description="The states its lifecycle reads return, in order"
    )

    def get_lifecycle_state(self) -> AgentLifecycleState:
        return self.lifecycle_states.pop(0) if len(self.lifecycle_states) > 1 else self.lifecycle_states[0]


class NeverReadyCommandAgent(UpgradableCommandAgent):
    """An upgradable command agent that is ready once ``READY_MARKER_NAME`` exists in its state dir, which nothing writes."""

    def get_ready_timeout_seconds(self) -> float:
        return NEVER_READY_TIMEOUT_SECONDS

    def wait_for_ready_after_restart(self, timeout: float) -> None:
        ready_marker_path = self._get_agent_dir() / READY_MARKER_NAME
        if not poll_until(lambda: self._check_file_exists(ready_marker_path), timeout=timeout, poll_interval=0.05):
            raise AgentStartError(str(self.name), f"{READY_MARKER_NAME} did not appear within {timeout}s")


class UpgradeFailingCommandAgent(UpgradableCommandAgent):
    """An upgradable command agent whose build always raises ``UPGRADE_FAILURE_MESSAGE``."""

    build_error_message: str | None = Field(
        default=UPGRADE_FAILURE_MESSAGE, description="When set, the build raises with it"
    )


class UpgradeCrashingCommandAgent(UpgradableCommandAgent):
    """An upgradable command agent whose build always raises ``UpgradeCrashError``."""

    def build_upgraded_files(self, input_content_by_path: Mapping[Path, str | None]) -> dict[Path, HarnessFileSpec]:
        raise UpgradeCrashError(UPGRADE_FAILURE_MESSAGE)


def register_upgradable_command_agent_types() -> None:
    """Register the upgradable test agent types, so a host loads an agent of each type as its class."""
    register_agent_class(UPGRADABLE_COMMAND_AGENT_TYPE, UpgradableCommandAgent)
    register_agent_config(UPGRADABLE_COMMAND_AGENT_TYPE, AgentTypeConfig)
    register_agent_class(UPGRADE_FAILING_COMMAND_AGENT_TYPE, UpgradeFailingCommandAgent)
    register_agent_config(UPGRADE_FAILING_COMMAND_AGENT_TYPE, AgentTypeConfig)
    register_agent_class(UPGRADE_CRASHING_COMMAND_AGENT_TYPE, UpgradeCrashingCommandAgent)
    register_agent_config(UPGRADE_CRASHING_COMMAND_AGENT_TYPE, AgentTypeConfig)


class LockRecordingHost(Host):
    """Local Host whose cooperative lock is observable and optionally refused, and whose agent stops and starts only record whether that lock was held."""

    is_lock_refused: bool = Field(default=False, description="Refuse every acquisition with LockNotHeldError")
    is_currently_locked: bool = Field(default=False, description="Whether the (fake) lock is held right now")
    lock_timeouts: list[float | None] = Field(
        default_factory=list, description="The timeout_seconds passed to each acquisition attempt"
    )
    is_lock_held_at_stop: list[bool] = Field(
        default_factory=list, description="Whether the lock was held when each stop_agents call ran"
    )
    is_lock_held_at_start: list[bool] = Field(
        default_factory=list, description="Whether the lock was held when each start_agents call ran"
    )

    @contextmanager
    def lock_cooperatively(self, timeout_seconds: float | None = 300.0) -> Generator[None, None, None]:
        self.lock_timeouts.append(timeout_seconds)
        if self.is_lock_refused:
            raise LockNotHeldError("Timed out waiting to acquire the host lock")
        self.is_currently_locked = True
        try:
            yield
        finally:
            self.is_currently_locked = False

    def stop_agents(self, agent_ids: Sequence[AgentId], timeout_seconds: float = 5.0) -> None:
        self.is_lock_held_at_stop.append(self.is_currently_locked)

    def start_agents(self, agent_ids: Sequence[AgentId]) -> None:
        self.is_lock_held_at_start.append(self.is_currently_locked)


UpgradableCommandAgentT = TypeVar("UpgradableCommandAgentT", bound=UpgradableCommandAgent)


def make_upgradable_command_agent(
    host: OnlineHostInterface,
    agent_id: AgentId,
    agent_name: AgentName,
    work_dir: Path,
    mngr_ctx: MngrContext,
    upgrade_unsupported_detail: str | None,
    upgraded_script_content: str,
    build_error_message: str | None,
    agent_class: type[UpgradableCommandAgentT],
) -> UpgradableCommandAgentT:
    """An upgradable view, of ``agent_class``, of the command agent ``agent_id`` on ``host``, built without validation."""
    return agent_class.model_construct(
        id=agent_id,
        name=agent_name,
        agent_type=AgentTypeName("command"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=mngr_ctx,
        agent_config=AgentTypeConfig(),
        host=host,
        upgrade_unsupported_detail=upgrade_unsupported_detail,
        upgraded_script_content=upgraded_script_content,
        build_error_message=build_error_message,
    )


def write_command_agent_record(
    host: Host, agent_id: AgentId, agent_name: AgentName, work_dir: Path, agent_data: Mapping[str, object]
) -> Path:
    """Write the data.json of a command agent in its new state dir on ``host``, with ``agent_data`` over its fields, and return that dir."""
    state_dir = get_agent_state_dir_path(host.host_dir, agent_id)
    state_dir.mkdir(parents=True)
    (state_dir / "data.json").write_text(
        json.dumps(
            {
                "id": str(agent_id),
                "name": str(agent_name),
                "type": "command",
                "command": "sleep 948226",
                "work_dir": str(work_dir),
                "create_time": datetime.now(timezone.utc).isoformat(),
                **agent_data,
            }
        )
    )
    return state_dir


def make_recorded_upgradable_agent(
    host: Host,
    work_dir: Path,
    mngr_ctx: MngrContext,
    agent_data: Mapping[str, object],
    upgraded_script_content: str,
    agent_class: type[UpgradableCommandAgentT],
) -> UpgradableCommandAgentT:
    """An upgradable view, of ``agent_class``, of a never-started command agent whose state dir holds only its data.json, with ``agent_data`` over its fields."""
    agent_id = AgentId.generate()
    agent_name = AgentName(f"recorded-{agent_id.get_uuid().hex[:8]}")
    write_command_agent_record(host, agent_id, agent_name, work_dir, agent_data)
    return make_upgradable_command_agent(
        host,
        agent_id,
        agent_name,
        work_dir,
        mngr_ctx,
        upgrade_unsupported_detail=None,
        upgraded_script_content=upgraded_script_content,
        build_error_message=None,
        agent_class=agent_class,
    )


def make_upgrade_outcome_host(host: OnlineHostInterface) -> UpgradeOutcomeHost:
    """``host`` as an outcome of ``mngr upgrade`` names it."""
    return UpgradeOutcomeHost(id=host.id, name=host.get_name(), provider_name=host.get_provider_name())


class InProcessSshHost(Host):
    """An SSH host on the in-process paramiko server that records its reads, uploads and lock acquisitions.

    That server has no SFTP subsystem and runs a command only once its stdin closes, so reads, uploads and the host
    lock go through the filesystem it shares with this process.
    """

    read_paths: list[Path] = Field(default_factory=list, description="Each file read, in order")
    upload_count: int = Field(default=0, description="How many directory uploads ran")
    lock_count: int = Field(default=0, description="How many times the host lock was taken")

    def read_file(self, path: Path) -> bytes:
        self.read_paths.append(path)
        return path.read_bytes()

    @contextmanager
    def lock_cooperatively(self, timeout_seconds: float | None = 300.0) -> Generator[None, None, None]:
        self.lock_count += 1
        yield

    def copy_local_directory(self, source_path: Path, target_path: Path, extra_args: str | None) -> None:
        self.upload_count += 1
        self.execute_idempotent_command(f"mkdir -p {shlex.quote(str(target_path))}")
        shutil.copytree(source_path, target_path, dirs_exist_ok=True)

    def count_operations_besides_ssh_commands(self) -> int:
        """The reads, uploads and lock acquisitions so far, each of which is one round trip to a real host."""
        return len(self.read_paths) + self.upload_count + self.lock_count


InProcessSshHostT = TypeVar("InProcessSshHostT", bound=InProcessSshHost)


def make_in_process_ssh_host(
    create_ssh_host: Callable[[str], Host], host_class: type[InProcessSshHostT], **extra_fields: Any
) -> InProcessSshHostT:
    """A ``host_class`` view of an SSH host ``create_ssh_host`` makes on the in-process server, which takes over that host's connection."""
    ssh_host = create_ssh_host("upgrade-remote")
    in_process_host = host_class(
        id=ssh_host.id,
        host_name=ssh_host.host_name,
        connector=ssh_host.connector,
        provider_instance=ssh_host.provider_instance,
        mngr_ctx=ssh_host.mngr_ctx,
        **extra_fields,
    )
    # The factory host has not connected yet, so this only stops it closing the shared client when it is collected.
    ssh_host.disconnect()
    return in_process_host


def upgrade_agent(
    agent: AgentInterface[Any],
    host: OnlineHostInterface,
    is_forced: bool,
    is_dry_run: bool,
    is_caller: bool,
    mngr_ctx: MngrContext,
) -> AgentUpgradeOutcome:
    """The outcome of ``mngr upgrade`` for ``agent`` on ``host``, which it reached.

    A connection lost at the host lock, the input read, the staging, the dialog check or the start check raises
    HostConnectionError, and one lost at the stop, swap or start raises ConnectionLostAtUpgradeStepError; one lost while
    the agent's tmux panes and work state are read skips the agent as unreadable.
    """
    match = AgentMatch(
        agent_id=agent.id,
        agent_name=agent.name,
        host_id=host.id,
        host_name=host.get_name(),
        provider_name=host.get_provider_name(),
    )
    return upgrade_matched_agent(
        match, ReachedHost(host=host, agent_by_id={agent.id: agent}), is_forced, is_dry_run, is_caller, mngr_ctx
    )


def run_upgrade_as_cli(agent: AgentInterface[Any]) -> AgentUpgradeOutcome:
    """The outcome of ``mngr upgrade NAME`` for ``agent``: on its own host, not forced and not a dry run."""
    return upgrade_agent(
        agent, agent.get_host(), is_forced=False, is_dry_run=False, is_caller=False, mngr_ctx=agent.mngr_ctx
    )


@pure
def make_current_outcome(agent: AgentInterface[Any]) -> AgentCurrent:
    """The outcome of a real run that finds ``agent`` current."""
    return AgentCurrent(
        agent_id=agent.id, agent_name=agent.name, host=make_upgrade_outcome_host(agent.get_host()), is_dry_run=False
    )


@pure
def make_upgraded_while_stopped_outcome(agent: AgentInterface[Any], changed_paths: Sequence[Path]) -> AgentUpgraded:
    """The outcome of a real run that rewrote ``changed_paths`` on ``agent``, which stays stopped."""
    return AgentUpgraded(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(agent.get_host()),
        plan=UpgradePlan.LEAVE_STOPPED,
        prior_state=AgentLifecycleState.STOPPED,
        changed_paths=tuple(changed_paths),
        is_dry_run=False,
    )


@pure
def make_skipped_outcome(agent: AgentInterface[Any], skip: UpgradeSkip) -> AgentUpgradeSkipped:
    """The outcome of a real run that skips ``agent`` for ``skip``."""
    return AgentUpgradeSkipped(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(agent.get_host()),
        kind=skip.kind,
        detail=skip.detail,
        is_dry_run=False,
    )


@pure
def make_unsupported_until_recreated_outcome(agent: AgentInterface[Any], harness_detail: str) -> AgentUpgradeSkipped:
    """The outcome of a real run that skips ``agent`` for ``harness_detail``, the reason its harness gives, followed by the re-create command."""
    assert "mngr create" not in harness_detail, (
        f"mngr upgrade appends the re-create command, so a harness's unsupported detail leaves it out: {harness_detail!r}"
    )
    agent_label = format_agent_label(agent.name, make_upgrade_outcome_host(agent.get_host()))
    return make_skipped_outcome(
        agent,
        UpgradeSkip(
            kind=UpgradeSkipKind.UNSUPPORTED,
            detail=UpgradeSkipDetail(
                f"{harness_detail}; re-create it with `mngr create {agent_label} --reuse --update`"
            ),
        ),
    )


@pure
def make_prepare_failed_outcome(agent: AgentInterface[Any], error_message: str) -> AgentUpgradeFailed:
    """The outcome of a run whose prepare step failed for ``agent`` with ``error_message``."""
    return AgentUpgradeFailed(
        agent_id=agent.id,
        agent_name=agent.name,
        host=make_upgrade_outcome_host(agent.get_host()),
        step=UpgradeFailedStep.PREPARE,
        error_message=error_message,
    )
