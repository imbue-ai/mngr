"""Tests for SSH host setup utilities."""

import importlib.resources
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import imbue.mng.resources as mng_resources
from imbue.mng.providers.ssh_host_setup import RequiredHostPackage
from imbue.mng.providers.ssh_host_setup import WARNING_PREFIX
from imbue.mng.providers.ssh_host_setup import _build_package_check_snippet
from imbue.mng.providers.ssh_host_setup import build_add_known_hosts_command
from imbue.mng.providers.ssh_host_setup import build_check_and_install_packages_command
from imbue.mng.providers.ssh_host_setup import build_configure_ssh_command
from imbue.mng.providers.ssh_host_setup import build_start_activity_watcher_command
from imbue.mng.providers.ssh_host_setup import build_start_volume_sync_command
from imbue.mng.providers.ssh_host_setup import get_user_ssh_dir
from imbue.mng.providers.ssh_host_setup import load_resource_script
from imbue.mng.providers.ssh_host_setup import parse_warnings_from_output


def test_root_user() -> None:
    """Root user should get /root/.ssh."""
    result = get_user_ssh_dir("root")
    assert result == Path("/root/.ssh")


def test_regular_user() -> None:
    """Regular users should get /home/<user>/.ssh."""
    result = get_user_ssh_dir("alice")
    assert result == Path("/home/alice/.ssh")


def test_valid_shell_command() -> None:
    """The command should be a valid shell command string."""
    cmd = build_check_and_install_packages_command("/mng/hosts/test")
    assert isinstance(cmd, str)
    assert len(cmd) > 0


def test_build_package_check_snippet_default_check() -> None:
    """When no check_cmd is given, should use 'command -v <binary>' and reference the package."""
    pkg = RequiredHostPackage(package="tmux", binary="tmux", check_cmd=None)
    snippet = _build_package_check_snippet(pkg)
    assert "command -v tmux >/dev/null 2>&1" in snippet
    assert f"{WARNING_PREFIX}tmux is not pre-installed" in snippet
    assert 'PKGS_TO_INSTALL="$PKGS_TO_INSTALL tmux"' in snippet


def test_build_package_check_snippet_custom_check() -> None:
    """When check_cmd is provided, should use that instead of the default."""
    pkg = RequiredHostPackage(package="openssh-server", binary="sshd", check_cmd="test -x /usr/sbin/sshd")
    snippet = _build_package_check_snippet(pkg)
    assert "test -x /usr/sbin/sshd" in snippet
    assert "command -v" not in snippet
    assert f"{WARNING_PREFIX}openssh-server is not pre-installed" in snippet
    assert 'PKGS_TO_INSTALL="$PKGS_TO_INSTALL openssh-server"' in snippet


def test_valid_configure_ssh_command() -> None:
    """The command should be a valid shell command string."""
    cmd = build_configure_ssh_command(
        user="root",
        client_public_key="ssh-ed25519 AAAA... user@host",
        host_private_key="-----BEGIN OPENSSH PRIVATE KEY-----\n...\n-----END OPENSSH PRIVATE KEY-----",
        host_public_key="ssh-ed25519 BBBB... hostkey",
    )
    assert isinstance(cmd, str)
    assert len(cmd) > 0


def test_extracts_warnings() -> None:
    """Should extract warning messages from output."""
    output = f"""
Some other output
{WARNING_PREFIX}This is a warning message
More output
{WARNING_PREFIX}Another warning
Final output
"""
    warnings = parse_warnings_from_output(output)
    assert len(warnings) == 2
    assert "This is a warning message" in warnings
    assert "Another warning" in warnings


def test_empty_output() -> None:
    """Empty output should return empty list."""
    warnings = parse_warnings_from_output("")
    assert warnings == []


def test_no_warnings() -> None:
    """Output without warnings should return empty list."""
    output = "Some normal output\nMore output\n"
    warnings = parse_warnings_from_output(output)
    assert warnings == []


def test_strips_whitespace() -> None:
    """Warning messages should have whitespace stripped."""
    output = f"{WARNING_PREFIX}  warning with spaces  "
    warnings = parse_warnings_from_output(output)
    assert warnings == ["warning with spaces"]


def test_skips_empty_warnings() -> None:
    """Empty warning messages should be skipped."""
    output = f"{WARNING_PREFIX}\n{WARNING_PREFIX}   \n{WARNING_PREFIX}actual warning"
    warnings = parse_warnings_from_output(output)
    assert warnings == ["actual warning"]


def test_load_resource_script_loads_activity_watcher() -> None:
    """Should load the activity watcher script from resources."""
    script = load_resource_script("activity_watcher.sh")
    assert isinstance(script, str)
    assert len(script) > 0
    assert "#!/bin/bash" in script
    assert "activity_watcher" in script.lower() or "HOST_DATA_DIR" in script


def test_build_start_activity_watcher_command() -> None:
    """Should build a valid shell command to start the activity watcher."""
    cmd = build_start_activity_watcher_command("/mng/hosts/test")
    assert isinstance(cmd, str)
    assert len(cmd) > 0
    assert "/mng/hosts/test" in cmd
    assert "mkdir -p" in cmd
    assert "chmod +x" in cmd
    assert "nohup" in cmd


def test_build_start_activity_watcher_command_escapes_quotes() -> None:
    """Should properly escape single quotes in the script content."""
    cmd = build_start_activity_watcher_command("/mng/hosts/test")
    # The command should contain the script content with proper escaping
    assert isinstance(cmd, str)
    # Single quotes in the script should be escaped as '\"'\"'
    # Since the script contains single quotes in strings like 'MNG_HOST_DIR'
    # they should be properly escaped
    assert cmd.count("printf") >= 1


def test_build_check_command_creates_symlink_when_volume_provided() -> None:
    """When host_volume_mount_path is provided, should remove existing dir and create symlink."""
    cmd = build_check_and_install_packages_command("/mng", host_volume_mount_path="/host_volume")
    assert "ln -sfn /host_volume /mng" in cmd
    assert "rm -rf /mng" in cmd
    assert "mkdir -p /mng" not in cmd


def test_build_check_command_creates_mkdir_when_no_volume() -> None:
    """When no host_volume_mount_path, should create directory with mkdir."""
    cmd = build_check_and_install_packages_command("/mng")
    assert "mkdir -p /mng" in cmd
    assert "ln -sfn" not in cmd


def test_build_start_volume_sync_command() -> None:
    """Should build a command that starts a background volume sync loop."""
    cmd = build_start_volume_sync_command("/host_volume", "/mng")
    assert "sync /host_volume" in cmd
    assert "nohup" in cmd
    assert "/mng/commands/volume_sync.sh" in cmd
    assert "/mng/logs/volume_sync.log" in cmd
    assert "sleep 60" in cmd


def test_build_add_known_hosts_command_empty() -> None:
    """Should return None when no entries are provided."""
    result = build_add_known_hosts_command("root", ())
    assert result is None


def test_build_add_known_hosts_command_single_entry() -> None:
    """Should build a valid command for a single known_hosts entry."""
    entry = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
    cmd = build_add_known_hosts_command("root", (entry,))
    assert cmd is not None
    assert isinstance(cmd, str)
    assert "mkdir -p '/root/.ssh'" in cmd
    assert "github.com" in cmd
    assert "chmod 600" in cmd
    assert "/root/.ssh/known_hosts" in cmd


def test_build_add_known_hosts_command_multiple_entries() -> None:
    """Should build a command that adds all entries."""
    entries = (
        "github.com ssh-ed25519 AAAAC3...",
        "gitlab.com ssh-rsa AAAAB3...",
    )
    cmd = build_add_known_hosts_command("root", entries)
    assert cmd is not None
    assert "github.com" in cmd
    assert "gitlab.com" in cmd
    # Should have two printf commands for the entries
    assert cmd.count("printf") == 2


def test_build_add_known_hosts_command_regular_user() -> None:
    """Should use the correct path for non-root users."""
    entry = "github.com ssh-ed25519 AAAAC3..."
    cmd = build_add_known_hosts_command("alice", (entry,))
    assert cmd is not None
    assert "/home/alice/.ssh" in cmd
    assert "/root" not in cmd


def test_build_add_known_hosts_command_escapes_quotes() -> None:
    """Should properly escape single quotes in entries."""
    entry = "host.example.com ssh-rsa key'with'quotes"
    cmd = build_add_known_hosts_command("root", (entry,))
    assert cmd is not None
    # Single quotes should be escaped as '\"'\"'
    assert "'\"'\"'" in cmd


# =============================================================================
# Activity Watcher Shell Function Tests
#
# These tests source the activity_watcher.sh script and exercise individual
# functions in isolation via bash subprocess calls.
# =============================================================================


def _get_activity_watcher_script_path() -> str:
    """Get the absolute path to the activity_watcher.sh resource file."""
    resource_files = importlib.resources.files(mng_resources)
    return str(resource_files.joinpath("activity_watcher.sh"))


def _create_test_script(script_path: str, host_data_dir: str, function_call: str) -> str:
    """Create a bash script string that sources the activity watcher and calls a function.

    Creates a modified version of the activity watcher script where the main()
    call at the end is replaced with the given function call. This allows testing
    individual functions without running the main loop.
    """
    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        "",
        f'HOST_DATA_DIR="{host_data_dir}"',
        "",
    ]

    # Read the script and extract everything between 'set -euo pipefail' and 'main' (exclusive)
    with open(script_path) as f:
        script_lines = f.readlines()

    in_body = False
    for line in script_lines:
        stripped = line.rstrip("\n")
        # Start capturing after the HOST_DATA_DIR assignment block
        if stripped.startswith("DATA_JSON_PATH="):
            in_body = True
        # Stop before the final main call
        if in_body and stripped == "main":
            break
        if in_body:
            lines.append(stripped)

    lines.append("")
    lines.append(function_call)
    return "\n".join(lines)


def _run_bash_function(script_path: str, host_data_dir: str, function_call: str) -> subprocess.CompletedProcess[str]:
    """Source the activity_watcher.sh script and run a function in bash."""
    bash_code = _create_test_script(script_path, host_data_dir, function_call)
    return subprocess.run(
        ["bash", "-c", bash_code],
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_get_tmux_session_prefix_returns_empty_when_no_data_json(tmp_path: Path) -> None:
    """get_tmux_session_prefix should return empty when data.json doesn't exist."""
    script_path = _get_activity_watcher_script_path()
    result = _run_bash_function(script_path, str(tmp_path), "get_tmux_session_prefix")
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_get_tmux_session_prefix_returns_empty_when_field_missing(tmp_path: Path) -> None:
    """get_tmux_session_prefix should return empty when field is not in data.json."""
    data_json = tmp_path / "data.json"
    data_json.write_text(json.dumps({"host_id": "test", "host_name": "test"}))

    script_path = _get_activity_watcher_script_path()
    result = _run_bash_function(script_path, str(tmp_path), "get_tmux_session_prefix")
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_get_tmux_session_prefix_returns_prefix_value(tmp_path: Path) -> None:
    """get_tmux_session_prefix should return the prefix from data.json."""
    data_json = tmp_path / "data.json"
    data_json.write_text(json.dumps({"tmux_session_prefix": "mng-"}))

    script_path = _get_activity_watcher_script_path()
    result = _run_bash_function(script_path, str(tmp_path), "get_tmux_session_prefix")
    assert result.returncode == 0
    assert result.stdout.strip() == "mng-"


def test_has_running_agent_sessions_returns_true_when_no_prefix(tmp_path: Path) -> None:
    """has_running_agent_sessions should return 0 (true) when no prefix is configured."""
    data_json = tmp_path / "data.json"
    data_json.write_text(json.dumps({"host_id": "test"}))

    script_path = _get_activity_watcher_script_path()
    result = _run_bash_function(script_path, str(tmp_path), "has_running_agent_sessions")
    assert result.returncode == 0


def test_has_running_agent_sessions_returns_true_when_no_agents_dir(tmp_path: Path) -> None:
    """has_running_agent_sessions should return 0 (true) when agents dir doesn't exist yet."""
    data_json = tmp_path / "data.json"
    data_json.write_text(json.dumps({"tmux_session_prefix": "mng-"}))

    script_path = _get_activity_watcher_script_path()
    result = _run_bash_function(script_path, str(tmp_path), "has_running_agent_sessions")
    assert result.returncode == 0


def test_has_running_agent_sessions_returns_true_when_agents_dir_empty(tmp_path: Path) -> None:
    """has_running_agent_sessions should return 0 (true) when agents dir exists but is empty."""
    data_json = tmp_path / "data.json"
    data_json.write_text(json.dumps({"tmux_session_prefix": "mng-"}))
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()

    script_path = _get_activity_watcher_script_path()
    result = _run_bash_function(script_path, str(tmp_path), "has_running_agent_sessions")
    assert result.returncode == 0


def test_has_running_agent_sessions_returns_true_during_grace_period(
    tmp_path: Path,
) -> None:
    """has_running_agent_sessions should return 0 (true) when agent dir was created recently."""
    data_json = tmp_path / "data.json"
    data_json.write_text(json.dumps({"tmux_session_prefix": "mng-test-unlikely-prefix-"}))
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "agent-abc123").mkdir()

    script_path = _get_activity_watcher_script_path()
    result = _run_bash_function(script_path, str(tmp_path), "has_running_agent_sessions")
    assert result.returncode == 0


@pytest.mark.tmux
@pytest.mark.skipif(sys.platform == "darwin", reason="Script reads /proc/uptime; tmux never reached on macOS")
def test_has_running_agent_sessions_returns_false_when_agents_exist_but_no_sessions(
    tmp_path: Path,
) -> None:
    """has_running_agent_sessions should return 1 (false) when agent dirs are old and no tmux sessions match."""
    data_json = tmp_path / "data.json"
    data_json.write_text(json.dumps({"tmux_session_prefix": "mng-test-unlikely-prefix-"}))
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    agent_dir = agents_dir / "agent-abc123"
    agent_dir.mkdir()
    # Set the agent dir mtime to be older than the grace period (120s)
    old_time = time.time() - 200
    os.utime(str(agent_dir), (old_time, old_time))

    script_path = _get_activity_watcher_script_path()
    # Override AGENT_SESSION_GRACE_PERIOD to 0 so the container uptime check
    # doesn't cause a false positive on freshly started CI runners.
    result = _run_bash_function(script_path, str(tmp_path), "AGENT_SESSION_GRACE_PERIOD=0\nhas_running_agent_sessions")
    assert result.returncode != 0
