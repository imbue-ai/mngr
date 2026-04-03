"""Tests for the create CLI command."""

import os
import subprocess
import time
from pathlib import Path

import pluggy
import pytest
from click.testing import CliRunner

from imbue.imbue_common.model_update import to_update
from imbue.mngr.api.agent_addr import parse_agent_address
from imbue.mngr.cli.create import _create_agent
from imbue.mngr.cli.create import _setup_create
from imbue.mngr.cli.create import create
from imbue.mngr.config.data_types import CreateCliOptions
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.utils.logging import LoggingConfig
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import capture_tmux_pane_contents
from imbue.mngr.utils.testing import tmux_session_cleanup
from imbue.mngr.utils.testing import tmux_session_exists
from imbue.mngr.utils.testing import wait_for_agent_session


@pytest.mark.tmux
def test_cli_create_with_echo_command(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_host_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test creating an agent with a simple echo command."""
    agent_name = f"test-cli-echo-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--command",
                "echo 'hello from cli test' && sleep 958374",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"

        wait_for(
            lambda: tmux_session_exists(session_name),
            timeout=15.0,
            error_message=f"Expected tmux session {session_name} to exist",
        )

        # Agents live directly under the host dir
        agents_dir = temp_host_dir / "agents"
        wait_for(
            lambda: agents_dir.exists(),
            timeout=15.0,
            error_message="agents directory should exist under host dir",
        )


@pytest.mark.tmux
def test_cli_create_via_subprocess(
    temp_work_dir: Path,
    temp_host_dir: Path,
    mngr_test_prefix: str,
    mngr_test_root_name: str,
) -> None:
    """Test calling the mngr create command via subprocess."""
    agent_name = f"test-subprocess-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"
    env = os.environ.copy()
    # Pass the test environment variables to the subprocess for proper isolation
    env["MNGR_HOST_DIR"] = str(temp_host_dir)
    env["MNGR_PREFIX"] = mngr_test_prefix
    # Prevent loading project config (.mngr/settings.toml) which might have
    # settings like extra_window that would interfere with tests
    env["MNGR_ROOT_NAME"] = mngr_test_root_name

    with tmux_session_cleanup(session_name):
        result = subprocess.run(
            [
                "uv",
                "run",
                "mngr",
                "create",
                "--name",
                agent_name,
                "--command",
                "sleep 651472",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
                # Note: --command automatically implies --type generic
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

        wait_for(
            lambda: tmux_session_exists(session_name),
            timeout=15.0,
            error_message=f"Expected tmux session {session_name} to exist",
        )

        # Agents live directly under the host dir
        agents_dir = temp_host_dir / "agents"
        wait_for(
            lambda: agents_dir.exists(),
            timeout=15.0,
            error_message="agents directory should exist under host dir",
        )


@pytest.mark.tmux
def test_connect_flag_calls_tmux_attach_for_local_agent(
    temp_work_dir: Path,
    temp_mngr_ctx: MngrContext,
    mngr_test_prefix: str,
    default_create_cli_opts: CreateCliOptions,
) -> None:
    """Test that --connect flag results in connection options that would attach to the tmux session.

    Calls _setup_create + _create_agent directly (bypassing _post_create) so we
    can verify the agent was created and the returned options indicate a connect should happen,
    without actually calling os.execvp to attach to tmux.
    """
    agent_name = f"test-connect-local-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"
    address = parse_agent_address(agent_name)

    opts = default_create_cli_opts.model_copy_update(
        to_update(default_create_cli_opts.field_ref().command, "sleep 397265"),
        to_update(default_create_cli_opts.field_ref().source_path, str(temp_work_dir)),
        to_update(default_create_cli_opts.field_ref().transfer, "none"),
        to_update(default_create_cli_opts.field_ref().connect, True),
        to_update(default_create_cli_opts.field_ref().ensure_clean, False),
    )

    output_opts = OutputOptions()

    with tmux_session_cleanup(session_name):
        setup = _setup_create(temp_mngr_ctx, output_opts, opts, LoggingConfig(), address)
        result = _create_agent(temp_mngr_ctx, output_opts, opts, setup)

        assert result is not None
        create_result, connection_opts = result

        # Verify the agent was created and the tmux session is running
        assert create_result.agent is not None
        assert create_result.host is not None
        assert tmux_session_exists(session_name)

        # Verify the returned options indicate connect should happen
        # (_post_create would call connect_to_agent -> os.execvp with tmux attach)
        assert opts.connect is True
        assert connection_opts.is_reconnect is True


@pytest.mark.tmux
def test_no_connect_flag_skips_tmux_attach(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --no-connect flag skips attaching to the tmux session.

    When --no-connect is used, the command should complete and return control
    to the caller (not exec into tmux attach). We verify this by checking that
    the CLI completes and returns a result.
    """
    agent_name = f"test-no-connect-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--command",
                "sleep 529847",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        # --no-connect skips connecting to the agent after creation
        assert result.exit_code == 0, f"CLI failed with: {result.output}"

        wait_for(
            lambda: tmux_session_exists(session_name),
            timeout=15.0,
            error_message=f"Expected tmux session {session_name} to exist",
        )


@pytest.mark.tmux
def test_message_file_flag_reads_message_from_file(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    tmp_path: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --message-file reads the initial message from a file."""
    agent_name = f"test-message-file-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    message_file = tmp_path / "message.txt"
    message_content = "Hello from file"
    message_file.write_text(message_content)

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--command",
                "cat",
                "--message-file",
                str(message_file),
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"

        wait_for(
            lambda: tmux_session_exists(session_name),
            timeout=15.0,
            error_message=f"Expected tmux session {session_name} to exist",
        )

        wait_for(
            lambda: message_content in capture_tmux_pane_contents(session_name),
            timeout=15.0,
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
            "--command",
            "cat",
            "--message",
            "Hello from flag",
            "--message-file",
            str(message_file),
            "--source",
            str(temp_work_dir),
            "--transfer=none",
            "--no-connect",
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
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that multi-line messages are sent using tmux send-keys."""
    agent_name = f"test-multiline-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    message_file = tmp_path / "multiline.txt"
    multiline_message = "Line 1\nLine 2\nLine 3"
    message_file.write_text(multiline_message)

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--command",
                "cat",
                "--message-file",
                str(message_file),
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"

        wait_for(
            lambda: tmux_session_exists(session_name),
            timeout=15.0,
            error_message=f"Expected tmux session {session_name} to exist",
        )

        for line in ["Line 1", "Line 2", "Line 3"]:
            wait_for(
                lambda line=line: line in capture_tmux_pane_contents(session_name),
                timeout=15.0,
                error_message=f"Expected line '{line}' to appear in tmux pane output",
            )


@pytest.mark.tmux
def test_single_line_message_uses_echo(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that single-line messages are sent using tmux send-keys."""
    agent_name = f"test-single-line-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"
    single_line_message = "Hello single line"

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--command",
                "cat",
                "--message",
                single_line_message,
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"

        wait_for(
            lambda: tmux_session_exists(session_name),
            timeout=15.0,
            error_message=f"Expected tmux session {session_name} to exist",
        )

        wait_for(
            lambda: single_line_message in capture_tmux_pane_contents(session_name),
            timeout=15.0,
            error_message=f"Expected message '{single_line_message}' to appear in tmux pane output",
        )


@pytest.mark.tmux
def test_extra_window_with_named_window(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that -w with name=command syntax creates a tmux window with the specified name."""
    agent_name = f"test-named-window-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--command",
                "sleep 629481",
                "-w",
                'myserver="sleep 847192"',
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"

        wait_for(
            lambda: tmux_session_exists(session_name),
            timeout=15.0,
            error_message=f"Expected tmux session {session_name} to exist",
        )

        def has_myserver_window() -> bool:
            window_list_result = subprocess.run(
                ["tmux", "list-windows", "-t", session_name, "-F", "#{window_name}"],
                capture_output=True,
                text=True,
            )
            window_names = window_list_result.stdout.strip().split("\n")
            return "myserver" in window_names

        wait_for(
            has_myserver_window,
            timeout=15.0,
            error_message="Expected window 'myserver' to exist",
        )


@pytest.mark.tmux
def test_extra_window_without_name_uses_default_window_name(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that -w without name prefix creates a tmux window with default name (cmd-N)."""
    agent_name = f"test-default-window-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--command",
                "sleep 538274",
                "-w",
                "sleep 719283",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"

        wait_for(
            lambda: tmux_session_exists(session_name),
            timeout=15.0,
            error_message=f"Expected tmux session {session_name} to exist",
        )

        def has_cmd_1_window() -> bool:
            window_list_result = subprocess.run(
                ["tmux", "list-windows", "-t", session_name, "-F", "#{window_name}"],
                capture_output=True,
                text=True,
            )
            window_names = window_list_result.stdout.strip().split("\n")
            return "cmd-1" in window_names

        wait_for(
            has_cmd_1_window,
            timeout=15.0,
            error_message="Expected window 'cmd-1' to exist",
        )


def test_command_and_type_are_mutually_exclusive(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --command and --type (other than generic) are mutually exclusive."""
    agent_name = f"test-mutex-{int(time.time())}"

    # "claude" agent type should conflict with --command
    result = cli_runner.invoke(
        create,
        [
            "--name",
            agent_name,
            "--command",
            "sleep 123456",
            "--type",
            "claude",
            "--source",
            str(temp_work_dir),
            "--transfer=none",
            "--no-connect",
            "--no-ensure-clean",
        ],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "--command and --type are mutually exclusive" in result.output


@pytest.mark.tmux
def test_command_with_generic_type_is_allowed(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_host_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that --command with --type generic is allowed (they are compatible)."""
    agent_name = f"test-generic-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        # Explicit --type generic is OK with --command
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--command",
                "sleep 654321",
                "--type",
                "generic",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--no-ensure-clean",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"

        wait_for(
            lambda: tmux_session_exists(session_name),
            timeout=15.0,
            error_message=f"Expected tmux session {session_name} to exist",
        )


@pytest.mark.tmux
def test_edit_message_sends_edited_content(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    intercepted_execvp_calls: list[tuple[str, list[str]]],
) -> None:
    """Test that --edit-message opens an editor and sends the edited message."""
    agent_name = f"test-edit-message-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"
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
                "--command",
                "cat",
                "--edit-message",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--connect",
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
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    intercepted_execvp_calls: list[tuple[str, list[str]]],
) -> None:
    """Test that --edit-message with --message uses the message as initial content."""
    agent_name = f"test-edit-initial-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"
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
                "--command",
                "cat",
                "--edit-message",
                "--message",
                initial_content,
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--connect",
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


@pytest.mark.tmux
def test_edit_message_empty_content_does_not_send(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    intercepted_execvp_calls: list[tuple[str, list[str]]],
) -> None:
    """Test that empty content from editor does not send a message."""
    agent_name = f"test-edit-empty-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"
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
                "--command",
                f"echo '{marker_text}' && cat",
                "--edit-message",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--connect",
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
    mngr_test_prefix: str,
    mngr_test_root_name: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
) -> None:
    """Test that --template applies values from the config file."""
    agent_name = f"test-template-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    # Create a config directory with a template (using test root name)
    config_dir = tmp_path / "project"
    config_dir.mkdir()
    mngr_dir = config_dir / f".{mngr_test_root_name}"
    mngr_dir.mkdir()
    settings_file = mngr_dir / "settings.toml"
    settings_file.write_text("""
[create_templates.mytemplate]
ensure_clean = false
""")

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--command",
                "sleep 847192",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--context",
                str(config_dir),
                "--template",
                "mytemplate",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"

        wait_for(
            lambda: tmux_session_exists(session_name),
            timeout=15.0,
            error_message=f"Expected tmux session {session_name} to exist",
        )


@pytest.mark.tmux
def test_template_cli_args_take_precedence(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    temp_host_dir: Path,
    mngr_test_prefix: str,
    mngr_test_root_name: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
) -> None:
    """Test that CLI arguments override template values."""
    agent_name = f"test-template-cli-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    # Create a config with a template that sets a message (using test root name)
    config_dir = tmp_path / "project"
    config_dir.mkdir()
    mngr_dir = config_dir / f".{mngr_test_root_name}"
    mngr_dir.mkdir()
    settings_file = mngr_dir / "settings.toml"
    settings_file.write_text("""
[create_templates.mytemplate]
message = "template-message"
ensure_clean = false
""")

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--command",
                "cat",
                "--source",
                str(temp_work_dir),
                "--transfer=none",
                "--no-connect",
                "--context",
                str(config_dir),
                "--template",
                "mytemplate",
                "--message",
                "cli-message",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"

        wait_for(
            lambda: tmux_session_exists(session_name),
            timeout=15.0,
            error_message=f"Expected tmux session {session_name} to exist",
        )

        # CLI message should appear, not template message
        wait_for(
            lambda: "cli-message" in capture_tmux_pane_contents(session_name),
            timeout=15.0,
            error_message="Expected CLI message 'cli-message' to appear in tmux pane output",
        )


def test_template_unknown_template_raises_error(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    mngr_test_root_name: str,
    plugin_manager: pluggy.PluginManager,
    tmp_path: Path,
) -> None:
    """Test that using an unknown template raises an error."""
    agent_name = f"test-unknown-template-{int(time.time())}"

    # Create a config with one template (using test root name)
    config_dir = tmp_path / "project"
    config_dir.mkdir()
    mngr_dir = config_dir / f".{mngr_test_root_name}"
    mngr_dir.mkdir()
    settings_file = mngr_dir / "settings.toml"
    settings_file.write_text("""
[create_templates.existing]
ensure_clean = false
""")

    result = cli_runner.invoke(
        create,
        [
            "--name",
            agent_name,
            "--command",
            "sleep 123456",
            "--source",
            str(temp_work_dir),
            "--transfer=none",
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
# Tests for ensure-clean behavior with explicit base branch
# =============================================================================


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
            "--command",
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
def test_ensure_clean_skipped_with_explicit_base_branch(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    temp_host_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Creating an agent with an explicit base branch skips the ensure-clean check."""
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
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--command",
                "sleep 847192",
                "--source",
                str(temp_git_repo),
                "--branch",
                "other-branch:mngr/*",
                "--no-connect",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"
        assert "uncommitted changes" not in result.output

        # Wait for background session so cleanup can properly kill it
        wait_for_agent_session(session_name)


@pytest.mark.tmux
def test_ensure_clean_skipped_with_explicit_base_branch_git_mirror_mode(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    temp_host_dir: Path,
    mngr_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Ensure-clean check is skipped with an explicit base branch even in git-mirror mode (not just worktree)."""
    # Create a second branch to use as base
    subprocess.run(
        ["git", "branch", "other-branch"],
        cwd=temp_git_repo,
        check=True,
        capture_output=True,
    )

    # Make the repo dirty
    (temp_git_repo / "dirty.txt").write_text("uncommitted change")

    agent_name = f"test-copy-base-clean-{int(time.time())}"
    session_name = f"{mngr_test_prefix}{agent_name}"

    with tmux_session_cleanup(session_name):
        result = cli_runner.invoke(
            create,
            [
                "--name",
                agent_name,
                "--command",
                "sleep 847192",
                "--source",
                str(temp_git_repo),
                "--branch",
                "other-branch:mngr/*",
                "--transfer",
                "git-mirror",
                "--no-connect",
            ],
            obj=plugin_manager,
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"CLI failed with: {result.output}"
        assert "uncommitted changes" not in result.output

        # Wait for background session so cleanup can properly kill it
        wait_for_agent_session(session_name)


# =============================================================================
# Tests for --transfer flag validation
# =============================================================================


def test_transfer_rsync_rejected_for_git_repo(
    cli_runner: CliRunner,
    temp_git_repo: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--transfer=rsync should be rejected when the source is a git repo."""
    result = cli_runner.invoke(
        create,
        [
            "--name",
            "test-rsync-git",
            "--command",
            "sleep 1",
            "--source",
            str(temp_git_repo),
            "--transfer",
            "rsync",
            "--no-connect",
            "--no-ensure-clean",
        ],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "rsync" in result.output.lower()


def test_transfer_git_mirror_rejected_for_non_git(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--transfer=git-mirror should be rejected when the source is not a git repo."""
    result = cli_runner.invoke(
        create,
        [
            "--name",
            "test-mirror-no-git",
            "--command",
            "sleep 1",
            "--source",
            str(temp_work_dir),
            "--transfer",
            "git-mirror",
            "--no-connect",
            "--no-ensure-clean",
        ],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "git repository" in result.output.lower()


def test_transfer_git_worktree_rejected_for_non_git(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--transfer=git-worktree should be rejected when the source is not a git repo."""
    result = cli_runner.invoke(
        create,
        [
            "--name",
            "test-worktree-no-git",
            "--command",
            "sleep 1",
            "--source",
            str(temp_work_dir),
            "--transfer",
            "git-worktree",
            "--no-connect",
            "--no-ensure-clean",
        ],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "git repository" in result.output.lower()


def test_transfer_none_with_different_target_path_rejected(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    tmp_path: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """--transfer=none with --target-path pointing to a different directory should be rejected."""
    different_dir = tmp_path / "different_target"
    different_dir.mkdir()

    result = cli_runner.invoke(
        create,
        [
            "--name",
            "test-none-diff-target",
            "--command",
            "sleep 1",
            "--source",
            str(temp_work_dir),
            "--transfer",
            "none",
            "--target-path",
            str(different_dir),
            "--no-connect",
            "--no-ensure-clean",
        ],
        obj=plugin_manager,
    )

    assert result.exit_code != 0
    assert "incompatible" in result.output.lower()


def test_create_with_invalid_provider_name(
    cli_runner: CliRunner,
    temp_work_dir: Path,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """mngr create with an unknown provider name should fail with a clear error."""
    result = cli_runner.invoke(
        create,
        [
            "--name",
            "test-invalid-provider",
            "--provider",
            "nonexistent",
            "--source",
            str(temp_work_dir),
            "--transfer=none",
            "--no-connect",
            "--no-ensure-clean",
        ],
        obj=plugin_manager,
        catch_exceptions=True,
    )
    assert result.exit_code != 0
    assert "unknown provider" in result.output.lower()
    assert "nonexistent" in result.output
