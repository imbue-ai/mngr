"""Tests for the create CLI command."""

import os
import subprocess
import time
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.imbue_common.model_update import to_update
from imbue.mng.cli.create import CreateCliOptions
from imbue.mng.cli.create import _handle_create
from imbue.mng.cli.create import create
from imbue.mng.config.data_types import MngContext
from imbue.mng.config.data_types import OutputOptions
from imbue.mng.utils.polling import wait_for
from imbue.mng.utils.testing import capture_tmux_pane_contents
from imbue.mng.utils.testing import tmux_session_cleanup
from imbue.mng.utils.testing import tmux_session_exists


@pytest.mark.tmux
def test_cli_create_with_echo_command(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_host_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test creating an agent with a simple echo command."""
    agent_name = f"test-cli-echo-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "echo 'hello from cli test' && sleep 958374",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"
        assert "Done." in result.output
        assert tmux_session_exists(session_name), f"Expected tmux session {session_name} to exist"

        # Agents live directly under the host dir
        agents_dir = temp_host_dir / "agents"
        assert agents_dir.exists(), "agents directory should exist under host dir"


@pytest.mark.tmux
def test_cli_create_via_subprocess(
    temp_work_dir: Path,
    temp_host_dir: Path,
    mng_test_prefix: str,
    mng_test_root_name: str,
) -> None:
    """Test calling the mng create command via subprocess."""
    agent_name = f"test-subprocess-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"
    env = os.environ.copy()
    # Pass the test environment variables to the subprocess for proper isolation
    env["MNG_HOST_DIR"] = str(temp_host_dir)
    env["MNG_PREFIX"] = mng_test_prefix
    # Prevent loading project config (.mng/settings.toml) which might have
    # settings like add_command that would interfere with tests
    env["MNG_ROOT_NAME"] = mng_test_root_name

    with tmux_session_cleanup(session_name):
        result = subprocess.run(
            [
                "uv",
                "run",
                "mng",
                "create",
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 651472",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
                # Note: --agent-cmd automatically implies --agent-type generic
                # Disable external providers to avoid connection errors in CI
                "--disable-plugin",
                "modal",
                "--disable-plugin",
                "docker",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

        assert result.returncode == 0, f"CLI failed with stderr: {result.stderr}\nstdout: {result.stdout}"
        assert tmux_session_exists(session_name), f"Expected tmux session {session_name} to exist"

        # Agents live directly under the host dir
        agents_dir = temp_host_dir / "agents"
        assert agents_dir.exists(), "agents directory should exist under host dir"


@pytest.mark.tmux
def test_connect_flag_calls_tmux_attach_for_local_agent(
    temp_work_dir: Path,
    temp_mng_ctx: MngContext,
    mng_test_prefix: str,
    default_create_cli_opts: CreateCliOptions,
) -> None:
    """Test that --connect flag results in connection options that would attach to the tmux session.

    Calls _handle_create directly (bypassing _post_create) so we can verify the agent
    was created and the returned options indicate a connect should happen, without
    actually calling os.execvp to attach to tmux.
    """
    agent_name = f"test-connect-local-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().name, agent_name),
        to_update(default_create_cli_opts.field_ref().agent_command, "sleep 397265"),
        to_update(default_create_cli_opts.field_ref().source_path, str(temp_work_dir)),
        to_update(default_create_cli_opts.field_ref().connect, True),
        to_update(default_create_cli_opts.field_ref().copy_work_dir, False),
        to_update(default_create_cli_opts.field_ref().ensure_clean, False),
    )

    output_opts = OutputOptions()

    with tmux_session_cleanup(session_name):
        result = _handle_create(temp_mng_ctx, output_opts, opts)

        assert result is not None
        create_result, connection_opts, _, returned_opts, _ = result

        # Verify the agent was created and the tmux session is running
        assert create_result.agent is not None
        assert create_result.host is not None
        assert tmux_session_exists(session_name)

        # Verify the returned options indicate connect should happen
        # (_post_create would call connect_to_agent -> os.execvp with tmux attach)
        assert returned_opts.connect is True
        assert connection_opts.is_reconnect is True


@pytest.mark.tmux
def test_no_connect_flag_skips_tmux_attach(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --no-connect flag skips attaching to the tmux session.

    When --no-connect is used, the command should complete and return control
    to the caller (not exec into tmux attach). We verify this by checking that
    the CLI completes and returns a result.
    """
    agent_name = f"test-no-connect-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 529847",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        # If --no-connect works, the CLI should complete and return 0
        # (if it had called execvp, the test process would be replaced)
        assert result.exit_code == 0, f"CLI failed with: {result.output}"
        assert tmux_session_exists(session_name), f"Expected tmux session {session_name} to exist"


@pytest.mark.tmux
def test_message_file_flag_reads_message_from_file(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    tmp_path: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --message-file reads the initial message from a file."""
    agent_name = f"test-message-file-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    message_file = tmp_path / "message.txt"
    message_content = "Hello from file"
    message_file.write_text(message_content)

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "cat",
                "--message-file",
                str(message_file),
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
                "--ready-timeout",
                "0.01",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"

        wait_for(
            lambda: tmux_session_exists(session_name),
            error_message=f"Expected tmux session {session_name} to exist",
        )

        wait_for(
            lambda: message_content in capture_tmux_pane_contents(session_name),
            error_message=f"Expected message '{message_content}' to appear in tmux pane output",
        )


def test_message_and_message_file_both_provided_raises_error(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that providing both --message and --message-file raises an error."""
    agent_name = f"test-both-message-{int(time.time())}"

    message_file = tmp_path / "message.txt"
    message_file.write_text("Hello from file")

    result = cli_runner.invoke(
        create,
        [
            "--name",
            agent_name,
            "--agent-cmd",
            "cat",
            "--message",
            "Hello from flag",
            "--message-file",
            str(message_file),
            "--source",
            str(temp_work_dir),
            "--no-connect",
            "--no-copy-work-dir",
            "--no-ensure-clean",
        ],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "Cannot provide both --message and --message-file" in result.output


@pytest.mark.tmux
def test_multiline_message_creates_file_and_pipes(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    tmp_path: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that multi-line messages are sent using tmux send-keys."""
    agent_name = f"test-multiline-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    message_file = tmp_path / "multiline.txt"
    multiline_message = "Line 1\nLine 2\nLine 3"
    message_file.write_text(multiline_message)

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "cat",
                "--message-file",
                str(message_file),
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
                "--ready-timeout",
                "0.01",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"

        wait_for(
            lambda: tmux_session_exists(session_name),
            error_message=f"Expected tmux session {session_name} to exist",
        )

        for line in ["Line 1", "Line 2", "Line 3"]:
            wait_for(
                lambda line=line: line in capture_tmux_pane_contents(session_name),
                error_message=f"Expected line '{line}' to appear in tmux pane output",
            )


@pytest.mark.tmux
def test_single_line_message_uses_echo(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that single-line messages are sent using tmux send-keys."""
    agent_name = f"test-single-line-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"
    single_line_message = "Hello single line"

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "cat",
                "--message",
                single_line_message,
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
                "--ready-timeout",
                "0.01",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"

        wait_for(
            lambda: tmux_session_exists(session_name),
            error_message=f"Expected tmux session {session_name} to exist",
        )

        wait_for(
            lambda: single_line_message in capture_tmux_pane_contents(session_name),
            error_message=f"Expected message '{single_line_message}' to appear in tmux pane output",
        )


@pytest.mark.tmux
def test_no_await_ready_creates_agent_in_background(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --no-await-ready creates agent in background and exits immediately."""
    agent_name = f"test-no-await-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 817364",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--no-await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"
        assert "Agent creation started in background" in result.output
        assert agent_name in result.output

        # Use a longer timeout than the default 5s because --no-await-ready forks a
        # child process that runs api_create() asynchronously. On loaded CI systems
        # the forked process may need more time to set up the tmux session.
        background_timeout = 15.0

        wait_for(
            lambda: tmux_session_exists(session_name),
            timeout=background_timeout,
            error_message=f"Expected tmux session {session_name} to exist",
        )

        # Wait for the command to actually start running in the session.
        # The background thread may still be sending keys after the session is created.
        def command_is_running() -> bool:
            pane_content = capture_tmux_pane_contents(session_name)
            return "sleep" in pane_content

        wait_for(
            command_is_running,
            timeout=background_timeout,
            error_message="Expected sleep command to be running",
        )


@pytest.mark.tmux
def test_add_command_with_named_window(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that -c with name=command syntax creates a tmux window with the specified name."""
    agent_name = f"test-named-window-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 629481",
                "-c",
                'myserver="sleep 847192"',
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"
        assert tmux_session_exists(session_name), f"Expected tmux session {session_name} to exist"

        window_list_result = subprocess.run(
            ["tmux", "list-windows", "-t", session_name, "-F", "#{window_name}"],
            capture_output=True,
            text=True,
        )
        window_names = window_list_result.stdout.strip().split("\n")
        assert "myserver" in window_names, f"Expected window 'myserver' in {window_names}"


@pytest.mark.tmux
def test_add_command_without_name_uses_default_window_name(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that -c without name prefix creates a tmux window with default name (cmd-N)."""
    agent_name = f"test-default-window-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 538274",
                "-c",
                "sleep 719283",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"
        assert tmux_session_exists(session_name), f"Expected tmux session {session_name} to exist"

        window_list_result = subprocess.run(
            ["tmux", "list-windows", "-t", session_name, "-F", "#{window_name}"],
            capture_output=True,
            text=True,
        )
        window_names = window_list_result.stdout.strip().split("\n")
        assert "cmd-1" in window_names, f"Expected window 'cmd-1' in {window_names}"


def test_agent_cmd_and_agent_type_are_mutually_exclusive(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --agent-cmd and --agent-type (other than generic) are mutually exclusive."""
    agent_name = f"test-mutex-{int(time.time())}"

    # "claude" agent type should conflict with --agent-cmd
    result = cli_runner.invoke(
        create,
        [
            "--name",
            agent_name,
            "--agent-cmd",
            "sleep 123456",
            "--agent-type",
            "claude",
            "--source",
            str(temp_work_dir),
            "--no-connect",
            "--no-copy-work-dir",
            "--no-ensure-clean",
        ],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "--agent-cmd and --agent-type are mutually exclusive" in result.output


@pytest.mark.tmux
def test_agent_cmd_with_generic_type_is_allowed(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_host_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --agent-cmd with --agent-type generic is allowed (they are compatible)."""
    agent_name = f"test-generic-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Explicit --agent-type generic is OK with --agent-cmd
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 654321",
                "--agent-type",
                "generic",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"
        assert tmux_session_exists(session_name), f"Expected tmux session {session_name} to exist"


@pytest.mark.tmux
def test_await_agent_stopped_waits_for_agent_to_exit(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --await-agent-stopped waits for the agent to completely finish running."""
    agent_name = f"test-await-stopped-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    # Use tmux_session_cleanup to ensure cleanup even if test fails
    with tmux_session_cleanup(session_name):
        # Run a command that echoes something and then exits
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "echo 'hello from await-stopped test' && exit 0",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-agent-stopped",
                "--no-connect",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"
        # The key assertion: we waited for the agent to stop
        assert "Waiting for agent to stop..." in result.output
        assert "Done." in result.output

        # wait for the tmux session to be gone
        wait_for(
            lambda: not tmux_session_exists(session_name),
            error_message=f"Expected tmux session {session_name} to be gone after agent stopped",
        )


@pytest.mark.tmux
def test_edit_message_sends_edited_content(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that --edit-message opens an editor and sends the edited message."""
    agent_name = f"test-edit-message-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"
    edited_message = "Hello from edited message"

    # Create a script that acts as the "editor" and writes the message to the file
    editor_script = tmp_path / "test_editor.sh"
    editor_script.write_text(f'#!/bin/bash\necho -n "{edited_message}" > "$1"\n')
    editor_script.chmod(0o755)

    monkeypatch.setenv("EDITOR", str(editor_script))
    monkeypatch.delenv("VISUAL", raising=False)

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "cat",
                "--edit-message",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"

        wait_for(
            lambda: tmux_session_exists(session_name),
            error_message=f"Expected tmux session {session_name} to exist",
        )

        wait_for(
            lambda: edited_message in capture_tmux_pane_contents(session_name),
            error_message=f"Expected message '{edited_message}' to appear in tmux pane output",
        )


@pytest.mark.tmux
def test_edit_message_with_initial_content(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that --edit-message with --message uses the message as initial content."""
    agent_name = f"test-edit-initial-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"
    initial_content = "Initial content"
    edited_message = "Edited: " + initial_content

    # Create a file to capture the initial content that was in the temp file
    captured_file = tmp_path / "captured_initial.txt"

    # Create a script that captures the initial content, then writes the edited message
    editor_script = tmp_path / "test_editor.sh"
    editor_script.write_text(f'#!/bin/bash\ncp "$1" "{captured_file}"\necho -n "{edited_message}" > "$1"\n')
    editor_script.chmod(0o755)

    monkeypatch.setenv("EDITOR", str(editor_script))
    monkeypatch.delenv("VISUAL", raising=False)

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "cat",
                "--edit-message",
                "--message",
                initial_content,
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"

        # Verify the captured initial content
        assert captured_file.exists(), "Editor script should have captured the initial content"
        captured_initial_content = captured_file.read_text()
        assert captured_initial_content == initial_content, (
            f"Expected initial content '{initial_content}' but got '{captured_initial_content}'"
        )

        wait_for(
            lambda: tmux_session_exists(session_name),
            error_message=f"Expected tmux session {session_name} to exist",
        )

        wait_for(
            lambda: edited_message in capture_tmux_pane_contents(session_name),
            error_message=f"Expected message '{edited_message}' to appear in tmux pane output",
        )


def test_edit_message_incompatible_with_background_creation(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --edit-message cannot be used with background creation."""
    agent_name = f"test-edit-bg-{int(time.time())}"

    result = cli_runner.invoke(
        create,
        [
            "--name",
            agent_name,
            "--agent-cmd",
            "sleep 123456",
            "--edit-message",
            "--source",
            str(temp_work_dir),
            "--no-connect",
            "--no-await-ready",
            "--no-copy-work-dir",
            "--no-ensure-clean",
        ],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "--edit-message cannot be used with background creation" in result.output


@pytest.mark.tmux
def test_edit_message_empty_content_does_not_send(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that empty content from editor does not send a message."""
    agent_name = f"test-edit-empty-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"
    marker_text = "AGENT_READY_MARKER"

    # Create a script that clears the file (simulating user saving empty file)
    editor_script = tmp_path / "test_editor.sh"
    editor_script.write_text('#!/bin/bash\necho -n "" > "$1"\n')
    editor_script.chmod(0o755)

    monkeypatch.setenv("EDITOR", str(editor_script))
    monkeypatch.delenv("VISUAL", raising=False)

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                f"echo '{marker_text}' && cat",
                "--edit-message",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"

        wait_for(
            lambda: tmux_session_exists(session_name),
            error_message=f"Expected tmux session {session_name} to exist",
        )

        # Verify agent started (marker appears)
        wait_for(
            lambda: marker_text in capture_tmux_pane_contents(session_name),
            error_message=f"Expected marker '{marker_text}' to appear in tmux pane output",
        )

        # Warning should be logged about no message being sent
        assert "No message to send" in result.output or "empty" in result.output.lower()


@pytest.mark.tmux
def test_template_applies_values_from_config(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_host_dir: Path,
    mng_test_prefix: str,
    mng_test_root_name: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
) -> None:
    """Test that --template applies values from the config file."""
    agent_name = f"test-template-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    # Create a config directory with a template (using test root name)
    config_dir = tmp_path / "project"
    config_dir.mkdir()
    mng_dir = config_dir / f".{mng_test_root_name}"
    mng_dir.mkdir()
    settings_file = mng_dir / "settings.toml"
    settings_file.write_text("""
[create_templates.mytemplate]
no_copy_work_dir = true
no_ensure_clean = true
""")

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 847192",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--context",
                str(config_dir),
                "--template",
                "mytemplate",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"
        assert tmux_session_exists(session_name), f"Expected tmux session {session_name} to exist"


@pytest.mark.tmux
def test_template_cli_args_take_precedence(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_host_dir: Path,
    mng_test_prefix: str,
    mng_test_root_name: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
) -> None:
    """Test that CLI arguments override template values."""
    agent_name = f"test-template-cli-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    # Create a config with a template that sets a message (using test root name)
    config_dir = tmp_path / "project"
    config_dir.mkdir()
    mng_dir = config_dir / f".{mng_test_root_name}"
    mng_dir.mkdir()
    settings_file = mng_dir / "settings.toml"
    settings_file.write_text("""
[create_templates.mytemplate]
message = "template-message"
no_copy_work_dir = true
no_ensure_clean = true
""")

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "cat",
                "--source",
                str(temp_work_dir),
                "--no-connect",
                "--await-ready",
                "--context",
                str(config_dir),
                "--template",
                "mytemplate",
                "--message",
                "cli-message",
                "--ready-timeout",
                "0.01",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"

        wait_for(
            lambda: tmux_session_exists(session_name),
            error_message=f"Expected tmux session {session_name} to exist",
        )

        # CLI message should appear, not template message
        wait_for(
            lambda: "cli-message" in capture_tmux_pane_contents(session_name),
            error_message="Expected CLI message 'cli-message' to appear in tmux pane output",
        )


def test_template_unknown_template_raises_error(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mng_test_root_name: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
) -> None:
    """Test that using an unknown template raises an error."""
    agent_name = f"test-unknown-template-{int(time.time())}"

    # Create a config with one template (using test root name)
    config_dir = tmp_path / "project"
    config_dir.mkdir()
    mng_dir = config_dir / f".{mng_test_root_name}"
    mng_dir.mkdir()
    settings_file = mng_dir / "settings.toml"
    settings_file.write_text("""
[create_templates.existing]
no_copy_work_dir = true
""")

    result = cli_runner.invoke(
        create,
        [
            "--name",
            agent_name,
            "--agent-cmd",
            "sleep 123456",
            "--source",
            str(temp_work_dir),
            "--no-connect",
            "--context",
            str(config_dir),
            "--template",
            "nonexistent",
        ],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "Template 'nonexistent' not found" in result.output
    assert "existing" in result.output


# =============================================================================
# Tests for ensure-clean behavior with --base-branch
# =============================================================================


@pytest.mark.git
def test_ensure_clean_rejects_dirty_worktree_by_default(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Creating an agent from a dirty git repo fails when ensure-clean is enabled (the default)."""
    # Make the repo dirty by creating an untracked file
    (temp_git_repo / "dirty.txt").write_text("uncommitted change")

    result = cli_runner.invoke(
        create,
        [
            "--name",
            "test-dirty",
            "--agent-cmd",
            "sleep 1",
            "--source",
            str(temp_git_repo),
            "--no-connect",
        ],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "uncommitted changes" in result.output


@pytest.mark.tmux
@pytest.mark.git
def test_ensure_clean_skipped_with_explicit_base_branch(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    temp_host_dir: Path,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Creating an agent with --base-branch skips the ensure-clean check."""
    # Create a second branch to use as base
    subprocess.run(
        ["git", "branch", "other-branch"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )

    # Make the repo dirty
    (temp_git_repo / "dirty.txt").write_text("uncommitted change")

    agent_name = f"test-base-branch-clean-{int(time.time())}"
    session_name = f"{mng_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--agent-cmd",
                "sleep 847192",
                "--source",
                str(temp_git_repo),
                "--base-branch",
                "other-branch",
                "--no-connect",
                "--await-ready",
                "--no-copy-work-dir",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"
        assert "uncommitted changes" not in result.output
