from collections.abc import Callable
from pathlib import Path
from typing import Generator

import click
import pluggy
import pytest
from click.testing import CliRunner

from imbue.mngr.cli.archive import archive
from imbue.mngr.cli.capture import capture
from imbue.mngr.cli.cleanup import cleanup
from imbue.mngr.cli.config import config
from imbue.mngr.cli.connect import ConnectCliOptions
from imbue.mngr.cli.connect import connect
from imbue.mngr.cli.destroy import destroy
from imbue.mngr.cli.events import events
from imbue.mngr.cli.exec import exec_command
from imbue.mngr.cli.gc import gc
from imbue.mngr.cli.help import help_command
from imbue.mngr.cli.label import label
from imbue.mngr.cli.limit import limit
from imbue.mngr.cli.message import message
from imbue.mngr.cli.migrate import migrate
from imbue.mngr.cli.provision import provision
from imbue.mngr.cli.pull import pull
from imbue.mngr.cli.push import push
from imbue.mngr.cli.rename import rename
from imbue.mngr.cli.snapshot import snapshot
from imbue.mngr.cli.start import start
from imbue.mngr.cli.stop import stop
from imbue.mngr.cli.transcript import transcript
from imbue.mngr.config.data_types import CreateCliOptions
from imbue.mngr.main import cli
from imbue.mngr.utils.testing import cleanup_tmux_session
from imbue.mngr.utils.testing import create_test_agent_via_cli


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
        type=None,
        reuse=False,
        connect=True,
        ensure_clean=True,
        name=None,
        id=None,
        name_style="coolname",
        command=None,
        extra_window=(),
        source=None,
        source_agent=None,
        source_host=None,
        source_path=None,
        target=None,
        target_path=None,
        transfer=None,
        rsync=None,
        rsync_args=None,
        include_git=True,
        include_unclean=None,
        include_gitignored=False,
        branch=":mngr/*",
        depth=None,
        shallow_since=None,
        env=(),
        env_file=(),
        pass_env=(),
        provider=None,
        new_host=False,
        host_name_style="coolname",
        host_label=(),
        label=(),
        project=None,
        host_env=(),
        host_env_file=(),
        pass_host_env=(),
        snapshot=None,
        build_arg=(),
        start_arg=(),
        reconnect=True,
        interactive=None,
        message=None,
        message_file=None,
        edit_message=False,
        retry=3,
        retry_delay="5s",
        attach_command=None,
        connect_command=None,
        idle_timeout=None,
        idle_mode=None,
        activity_sources=None,
        worktree_base_folder=None,
        start_on_boot=None,
        start_host=True,
        grant=(),
        extra_provision_command=(),
        upload_file=(),
        append_to_file=(),
        prepend_to_file=(),
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
        "imbue.mngr.api.connect.os.execvpe",
        lambda program, args, env: calls.append((program, args)),
    )
    return calls


def _create_and_track_test_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    created_sessions: list[str],
    agent_name: str,
    agent_cmd: str = "sleep 482917",
) -> str:
    """Create a test agent via CLI and track its session for cleanup."""
    session_name = create_test_agent_via_cli(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, agent_name, agent_cmd
    )
    created_sessions.append(session_name)
    return session_name


@pytest.fixture
def create_test_agent(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> Generator[Callable[..., str], None, None]:
    """Factory fixture that creates test agents via CLI and cleans up automatically.

    Usage:
        def test_something(create_test_agent):
            session_name = create_test_agent("my-agent")
            # ... test logic ...
            # cleanup happens automatically on fixture teardown

    Supports creating multiple agents per test -- all are cleaned up.
    """
    created_sessions: list[str] = []
    yield lambda agent_name, agent_cmd="sleep 482917": _create_and_track_test_agent(
        cli_runner, temp_work_dir, mngr_test_prefix, plugin_manager, created_sessions, agent_name, agent_cmd
    )

    for session_name in created_sessions:
        cleanup_tmux_session(session_name)


@pytest.fixture
def editor_recovery_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Provide a temporary recovery directory and EDITOR for editor recovery tests.

    Sets EDITOR=true so EditorSession.create() works without a real editor.
    Returns a recovery directory under tmp_path that tests pass to
    _rescue_editor_content / _editor_cleanup_scope via the recovery_dir parameter.
    """
    monkeypatch.setenv("EDITOR", "true")
    recovery_dir = tmp_path / ".mngr"
    return recovery_dir


# =============================================================================
# Parametrized --help tests (replaces per-file test_*_help_exits_zero)
# =============================================================================

_HELP_TEST_CASES: list[tuple[click.Command, list[str], str]] = [
    (archive, ["--help"], "archive"),
    (capture, ["--help"], "capture"),
    (cleanup, ["--help"], "cleanup"),
    (config, ["--help"], "config"),
    (connect, ["--help"], "connect"),
    (destroy, ["--help"], "destroy"),
    (exec_command, ["--help"], "exec"),
    (gc, ["--help"], "gc"),
    (help_command, ["--help"], "help"),
    (label, ["--help"], "label"),
    (limit, ["--help"], "limit"),
    (events, ["--help"], "events"),
    (transcript, ["--help"], "transcript"),
    (message, ["--help"], "message"),
    (migrate, ["--help"], "migrate"),
    (provision, ["--help"], "provision"),
    (pull, ["--help"], "pull"),
    (push, ["--help"], "push"),
    (rename, ["--help"], "rename"),
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
    (capture, ["nonexistent-agent-55123"], "capture"),
    (destroy, ["nonexistent-agent-88421"], "destroy"),
    (exec_command, ["nonexistent-agent-99999", "echo hello"], "exec"),
    (label, ["nonexistent-agent-44321", "--label", "key=value"], "label"),
    (limit, ["nonexistent-agent-77234", "--idle-timeout", "300"], "limit"),
    (events, ["nonexistent-agent-34892"], "events"),
    (transcript, ["nonexistent-agent-82341"], "transcript"),
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
