from datetime import datetime
from datetime import timezone
from pathlib import Path

import click
import pluggy
import pytest
from click.testing import CliRunner

from imbue.mng.cli.cleanup import cleanup
from imbue.mng.cli.config import config
from imbue.mng.cli.connect import ConnectCliOptions
from imbue.mng.cli.connect import connect
from imbue.mng.cli.create import CreateCliOptions
from imbue.mng.cli.destroy import destroy
from imbue.mng.cli.exec import exec_command
from imbue.mng.cli.gc import gc
from imbue.mng.cli.limit import limit
from imbue.mng.cli.logs import logs
from imbue.mng.cli.message import message
from imbue.mng.cli.migrate import migrate
from imbue.mng.cli.provision import provision
from imbue.mng.cli.pull import pull
from imbue.mng.cli.push import push
from imbue.mng.cli.rename import rename
from imbue.mng.cli.self_upgrade import self_upgrade
from imbue.mng.cli.snapshot import snapshot
from imbue.mng.cli.start import start
from imbue.mng.cli.stop import stop
from imbue.mng.interfaces.data_types import AgentInfo
from imbue.mng.interfaces.data_types import HostInfo
from imbue.mng.interfaces.data_types import SnapshotInfo
from imbue.mng.main import cli
from imbue.mng.primitives import AgentId
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostId
from imbue.mng.primitives import HostState
from imbue.mng.primitives import ProviderInstanceName


def make_test_agent_info(
    name: str = "test-agent",
    state: AgentLifecycleState = AgentLifecycleState.RUNNING,
    create_time: datetime | None = None,
    snapshots: list[SnapshotInfo] | None = None,
    host_plugin: dict | None = None,
    host_tags: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
) -> AgentInfo:
    """Create a real AgentInfo for testing.

    Shared helper used across CLI test files to avoid duplicating AgentInfo
    construction logic. Accepts optional overrides for commonly varied fields.
    """
    host_info = HostInfo(
        id=HostId.generate(),
        name="test-host",
        provider_name=ProviderInstanceName("local"),
        snapshots=snapshots or [],
        state=HostState.RUNNING,
        plugin=host_plugin or {},
        tags=host_tags or {},
    )
    return AgentInfo(
        id=AgentId.generate(),
        name=AgentName(name),
        type="generic",
        command=CommandString("sleep 100"),
        work_dir=Path("/tmp/test"),
        create_time=create_time or datetime.now(timezone.utc),
        start_on_boot=False,
        state=state,
        labels=labels or {},
        host=host_info,
    )


@pytest.fixture
def default_create_cli_opts() -> CreateCliOptions:
    """Baseline CreateCliOptions with sensible defaults for all fields.

    Tests use .model_copy_update() with to_update_dict() to override only the fields
    relevant to each test case.
    """
    return CreateCliOptions(
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
        positional_name=None,
        positional_agent_type=None,
        agent_args=(),
        template=(),
        agent_type=None,
        reuse=False,
        connect=True,
        await_ready=None,
        await_agent_stopped=None,
        copy_work_dir=None,
        ensure_clean=True,
        snapshot_source=None,
        name=None,
        agent_id=None,
        name_style="english",
        agent_command=None,
        add_command=(),
        user=None,
        source=None,
        source_agent=None,
        source_host=None,
        source_path=None,
        target=None,
        target_path=None,
        in_place=False,
        copy_source=False,
        clone=False,
        worktree=False,
        rsync=None,
        rsync_args=None,
        include_git=True,
        include_unclean=None,
        include_gitignored=False,
        base_branch=None,
        new_branch="",
        new_branch_prefix="mng/",
        depth=None,
        shallow_since=None,
        agent_env=(),
        agent_env_file=(),
        pass_agent_env=(),
        host=None,
        new_host=None,
        host_name=None,
        host_name_style="astronomy",
        tag=(),
        label=(),
        project=None,
        host_env=(),
        host_env_file=(),
        pass_host_env=(),
        known_hosts=(),
        authorized_keys=(),
        snapshot=None,
        build_arg=(),
        build_args=None,
        start_arg=(),
        start_args=None,
        reconnect=True,
        interactive=None,
        message=None,
        message_file=None,
        edit_message=False,
        resume_message=None,
        resume_message_file=None,
        retry=3,
        retry_delay="5s",
        attach_command=None,
        connect_command=None,
        idle_timeout=None,
        idle_mode=None,
        activity_sources=None,
        start_on_boot=None,
        start_host=True,
        grant=(),
        user_command=(),
        sudo_command=(),
        upload_file=(),
        append_to_file=(),
        prepend_to_file=(),
        create_directory=(),
        ready_timeout=10.0,
        yes=False,
    )


@pytest.fixture
def default_connect_cli_opts() -> ConnectCliOptions:
    """Baseline ConnectCliOptions with sensible defaults for all fields.

    Tests use .model_copy_update() with to_update() to override only the fields
    relevant to each test case.
    """
    return ConnectCliOptions(
        output_format="human",
        quiet=False,
        verbose=0,
        log_file=None,
        log_commands=None,
        log_command_output=None,
        log_env_vars=None,
        project_context_path=None,
        plugin=(),
        disable_plugin=(),
        agent=None,
        start=True,
        reconnect=True,
        message=None,
        message_file=None,
        ready_timeout=10.0,
        retry=3,
        retry_delay="5s",
        attach_command=None,
        allow_unknown_host=False,
    )


@pytest.fixture
def intercepted_execvp_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str]]]:
    """Intercept os.execvpe in connect_to_agent and return the captured calls.

    os.execvpe replaces the current process, making it impossible to test
    CLI-level connect flows without interception. This fixture uses pytest
    monkeypatch to replace it with a simple recorder.
    """
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        "imbue.mng.api.connect.os.execvpe",
        lambda program, args, env: calls.append((program, args)),
    )
    return calls


# =============================================================================
# Parametrized --help tests (replaces per-file test_*_help_exits_zero)
# =============================================================================

_HELP_TEST_CASES: list[tuple[click.Command, list[str], str]] = [
    (cleanup, ["--help"], "cleanup"),
    (config, ["--help"], "config"),
    (connect, ["--help"], "connect"),
    (destroy, ["--help"], "destroy"),
    (exec_command, ["--help"], "exec"),
    (gc, ["--help"], "gc"),
    (limit, ["--help"], "limit"),
    (logs, ["--help"], "logs"),
    (message, ["--help"], "message"),
    (migrate, ["--help"], "migrate"),
    (provision, ["--help"], "provision"),
    (pull, ["--help"], "pull"),
    (push, ["--help"], "push"),
    (rename, ["--help"], "rename"),
    (self_upgrade, ["--help"], "selfupgrade"),
    (start, ["--help"], "start"),
    (stop, ["--help"], "stop"),
    (cli, ["snapshot", "create", "--help"], "snapshot_create"),
    (cli, ["snapshot", "list", "--help"], "snapshot_list"),
    (cli, ["snapshot", "destroy", "--help"], "snapshot_destroy"),
]


@pytest.mark.parametrize(
    ("command", "args", "test_id"),
    [pytest.param(cmd, a, tid, id=tid) for cmd, a, tid in _HELP_TEST_CASES],
)
def test_help_exits_zero(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    command: click.Command,
    args: list[str],
    test_id: str,
) -> None:
    """Every CLI command's --help should exit 0."""
    result = cli_runner.invoke(command, args, obj=plugin_manager, catch_exceptions=False)
    assert result.exit_code == 0


# =============================================================================
# Parametrized nonexistent-agent tests (replaces per-file test_*_nonexistent_agent)
# =============================================================================

_NONEXISTENT_AGENT_CASES: list[tuple[click.Command, list[str], str]] = [
    (destroy, ["nonexistent-agent-88421"], "destroy"),
    (exec_command, ["nonexistent-agent-99999", "echo hello"], "exec"),
    (limit, ["nonexistent-agent-77234", "--idle-timeout", "300"], "limit"),
    (logs, ["nonexistent-agent-34892"], "logs"),
    (provision, ["nonexistent-agent-77412"], "provision"),
    (pull, ["nonexistent-agent-66201"], "pull"),
    (push, ["nonexistent-agent-77312"], "push"),
    (rename, ["nonexistent-agent-99812", "new-name"], "rename"),
    (snapshot, ["create", "nonexistent-agent-xyz"], "snapshot_create"),
    (snapshot, ["list", "nonexistent-agent-xyz"], "snapshot_list"),
    (start, ["nonexistent-agent-98732"], "start"),
    (stop, ["nonexistent-agent-45721"], "stop"),
]


@pytest.mark.parametrize(
    ("command", "args", "test_id"),
    [pytest.param(cmd, a, tid, id=tid) for cmd, a, tid in _NONEXISTENT_AGENT_CASES],
)
def test_nonexistent_agent_fails(
    cli_runner: CliRunner,
    plugin_manager: pluggy.PluginManager,
    command: click.Command,
    args: list[str],
    test_id: str,
) -> None:
    """Commands invoked with a nonexistent agent name should exit non-zero."""
    result = cli_runner.invoke(command, args, obj=plugin_manager, catch_exceptions=True)
    assert result.exit_code != 0
