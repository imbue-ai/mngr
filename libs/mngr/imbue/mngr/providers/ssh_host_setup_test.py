"""Tests for SSH host setup utilities."""

import importlib.resources
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import imbue.mngr.resources as mngr_resources
from imbue.mngr.providers.ssh_host_setup import RequiredHostPackage
from imbue.mngr.providers.ssh_host_setup import SSHD_PROVISIONED_MARKER_PATH
from imbue.mngr.providers.ssh_host_setup import WARNING_PREFIX
from imbue.mngr.providers.ssh_host_setup import _build_package_check_snippet
from imbue.mngr.providers.ssh_host_setup import build_add_authorized_keys_command
from imbue.mngr.providers.ssh_host_setup import build_add_known_hosts_command
from imbue.mngr.providers.ssh_host_setup import build_check_and_install_packages_command
from imbue.mngr.providers.ssh_host_setup import build_configure_ssh_command
from imbue.mngr.providers.ssh_host_setup import build_self_healing_host_entrypoint_command
from imbue.mngr.providers.ssh_host_setup import build_start_activity_watcher_command
from imbue.mngr.providers.ssh_host_setup import build_start_sshd_command
from imbue.mngr.providers.ssh_host_setup import build_start_volume_sync_command
from imbue.mngr.providers.ssh_host_setup import get_user_ssh_dir
from imbue.mngr.providers.ssh_host_setup import load_resource_script
from imbue.mngr.providers.ssh_host_setup import parse_warnings_from_output


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
    cmd = build_check_and_install_packages_command("/mngr/hosts/test")
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
    assert "#!/usr/bin/env bash" in script
    assert "activity_watcher" in script.lower() or "HOST_DATA_DIR" in script


def test_build_start_activity_watcher_command() -> None:
    """Should build a valid shell command to start the activity watcher."""
    cmd = build_start_activity_watcher_command("/mngr/hosts/test")
    assert isinstance(cmd, str)
    assert len(cmd) > 0
    assert "/mngr/hosts/test" in cmd
    assert "mkdir -p" in cmd
    assert "chmod +x" in cmd
    assert "nohup" in cmd


def test_build_start_activity_watcher_command_escapes_quotes() -> None:
    """Should properly escape single quotes in the script content."""
    cmd = build_start_activity_watcher_command("/mngr/hosts/test")
    # The command should contain the script content with proper escaping
    assert isinstance(cmd, str)
    # Single quotes in the script should be escaped as '\"'\"'
    # Since the script contains single quotes in strings like 'MNGR_HOST_DIR'
    # they should be properly escaped
    assert cmd.count("printf") >= 1


def test_build_check_command_creates_symlink_when_volume_provided() -> None:
    """When host_volume_mount_path is provided, should mkdir the target, remove existing dir, and create symlink."""
    cmd = build_check_and_install_packages_command("/mngr", host_volume_mount_path="/host_volume/host_dir")
    assert "mkdir -p /host_volume/host_dir" in cmd
    assert "ln -sfn /host_volume/host_dir /mngr" in cmd
    assert "rm -rf /mngr" in cmd
    assert "mkdir -p /mngr" not in cmd
    # The symlink-target mkdir must come before the symlink itself.
    assert cmd.index("mkdir -p /host_volume/host_dir") < cmd.index("ln -sfn /host_volume/host_dir /mngr")


def test_build_check_command_creates_mkdir_when_no_volume() -> None:
    """When no host_volume_mount_path, should create directory with mkdir."""
    cmd = build_check_and_install_packages_command("/mngr")
    assert "mkdir -p /mngr" in cmd
    assert "ln -sfn" not in cmd


def test_build_start_volume_sync_command() -> None:
    """Should build a command that starts a background volume sync loop."""
    cmd = build_start_volume_sync_command("/host_volume", "/mngr")
    assert "sync /host_volume" in cmd
    assert "nohup" in cmd
    assert "/mngr/commands/volume_sync.sh" in cmd
    assert "/mngr/logs/volume_sync.log" in cmd
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
# build_add_authorized_keys_command tests
# =============================================================================


def test_build_add_authorized_keys_command_empty() -> None:
    """Should return None when no entries are provided."""
    result = build_add_authorized_keys_command("root", ())
    assert result is None


def test_build_add_authorized_keys_command_regular_user() -> None:
    """Should use the correct path for non-root users."""
    entry = "ssh-ed25519 AAAAC3... user@host"
    cmd = build_add_authorized_keys_command("bob", (entry,))
    assert cmd is not None
    assert "/home/bob/.ssh" in cmd
    assert "/root" not in cmd


def test_build_add_authorized_keys_command_is_idempotent(tmp_path: Path) -> None:
    """Re-running the command must not duplicate entries.

    The imbue_cloud restart re-seed relies on this: on a host whose ``/root``
    persisted across the stop/start, the key is already present and a second run
    must leave the file unchanged rather than appending a duplicate line.
    """
    key_a = "ssh-ed25519 AAAAkeyA user-a@host"
    key_b = "ssh-ed25519 AAAAkeyB user-b@host"
    cmd = build_add_authorized_keys_command("bob", (key_a, key_b))
    assert cmd is not None
    # Redirect the hard-coded /home/bob/.ssh path at a writable temp dir so the
    # generated shell can actually run and we can inspect the file it produces.
    ssh_dir = tmp_path / "dot_ssh"
    runnable = cmd.replace("/home/bob/.ssh", str(ssh_dir))
    authorized_keys = ssh_dir / "authorized_keys"

    for _ in range(2):
        subprocess.run(["sh", "-c", runnable], check=True)

    lines = authorized_keys.read_text().splitlines()
    # Both keys present, each exactly once despite running the command twice.
    assert lines.count(key_a) == 1
    assert lines.count(key_b) == 1


# =============================================================================
# Activity Watcher Shell Function Tests
#
# These tests source the activity_watcher.sh script and exercise individual
# functions in isolation via bash subprocess calls.
# =============================================================================


def _get_activity_watcher_script_path() -> str:
    """Get the absolute path to the activity_watcher.sh resource file."""
    resource_files = importlib.resources.files(mngr_resources)
    return str(resource_files.joinpath("activity_watcher.sh"))


def _create_test_script(script_path: str, host_data_dir: str, function_call: str) -> str:
    """Create a bash script string that sources the activity watcher and calls a function.

    Creates a modified version of the activity watcher script where the main()
    call at the end is replaced with the given function call. This allows testing
    individual functions without running the main loop.
    """
    lines = [
        "#!/usr/bin/env bash",
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
    data_json.write_text(json.dumps({"tmux_session_prefix": "mngr-"}))

    script_path = _get_activity_watcher_script_path()
    result = _run_bash_function(script_path, str(tmp_path), "get_tmux_session_prefix")
    assert result.returncode == 0
    assert result.stdout.strip() == "mngr-"


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
    data_json.write_text(json.dumps({"tmux_session_prefix": "mngr-"}))

    script_path = _get_activity_watcher_script_path()
    result = _run_bash_function(script_path, str(tmp_path), "has_running_agent_sessions")
    assert result.returncode == 0


def test_has_running_agent_sessions_returns_true_when_agents_dir_empty(tmp_path: Path) -> None:
    """has_running_agent_sessions should return 0 (true) when agents dir exists but is empty."""
    data_json = tmp_path / "data.json"
    data_json.write_text(json.dumps({"tmux_session_prefix": "mngr-"}))
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
    data_json.write_text(json.dumps({"tmux_session_prefix": "mngr-test-unlikely-prefix-"}))
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
    data_json.write_text(json.dumps({"tmux_session_prefix": "mngr-test-unlikely-prefix-"}))
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


def test_get_activity_sources_returns_empty_when_no_data_json(tmp_path: Path) -> None:
    """get_activity_sources should return empty when data.json doesn't exist."""
    script_path = _get_activity_watcher_script_path()
    result = _run_bash_function(script_path, str(tmp_path), "get_activity_sources")
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_get_activity_sources_returns_empty_when_disabled(tmp_path: Path) -> None:
    """get_activity_sources should return empty when activity_sources is an empty array (disabled mode)."""
    data_json = tmp_path / "data.json"
    data_json.write_text(json.dumps({"activity_sources": []}))

    script_path = _get_activity_watcher_script_path()
    result = _run_bash_function(script_path, str(tmp_path), "get_activity_sources")
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_get_activity_sources_returns_sources_when_configured(tmp_path: Path) -> None:
    """get_activity_sources should return space-separated lowercase sources when configured."""
    data_json = tmp_path / "data.json"
    data_json.write_text(json.dumps({"activity_sources": ["BOOT", "AGENT"]}))

    script_path = _get_activity_watcher_script_path()
    result = _run_bash_function(script_path, str(tmp_path), "get_activity_sources")
    assert result.returncode == 0
    sources = result.stdout.strip().split()
    assert "boot" in sources
    assert "agent" in sources


# =========================================================================
# sshd start + self-healing entrypoint commands
# =========================================================================


def test_self_healing_entrypoint_is_valid_shell_and_backgrounds_only_idle() -> None:
    """The entrypoint must be valid shell and keep PID 1 alive after the sshd check."""
    cmd = build_self_healing_host_entrypoint_command()
    # Valid POSIX shell.
    assert subprocess.run(["sh", "-n", "-c", cmd], capture_output=True).returncode == 0
    # SIGTERM trap (clean docker stop) and the idle keep-alive are both present.
    assert "trap 'exit 0' TERM" in cmd
    assert "tail -f /dev/null & wait" in cmd
    # sshd is only (re)started once mngr has provisioned it (marker gate), not on
    # the mere presence of an image-baked host key.
    assert SSHD_PROVISIONED_MARKER_PATH in cmd
    assert "/usr/sbin/sshd" in cmd


def test_self_healing_entrypoint_skips_sshd_without_provisioned_marker(tmp_path: Path) -> None:
    """Without mngr's provisioned-marker the entrypoint must not start sshd.

    This is the regression guard for image-baked host keys: the gate must be the
    mngr marker, not the host key file (which a base image may already ship).
    """
    # Run only the synchronous prefix (drop the blocking `tail -f` keep-alive) and
    # rewrite the root-only paths / sshd binary into observable, unprivileged probes.
    sshd_started = tmp_path / "sshd_was_called"
    marker_path = tmp_path / "mngr_host_provisioned"
    cmd = build_self_healing_host_entrypoint_command()
    prefix = cmd.split("; tail -f /dev/null")[0]
    probe = (
        prefix.replace(SSHD_PROVISIONED_MARKER_PATH, str(marker_path))
        .replace("mkdir -p /run/sshd; ", "")
        .replace("/usr/sbin/sshd -o MaxSessions=100", f"touch {sshd_started}")
    )
    # No marker present -> sshd branch is skipped (the `[ -f ] && {{...}}` is a no-op).
    subprocess.run(["sh", "-c", probe], check=False)
    assert not sshd_started.exists()
    # Marker present -> sshd branch runs.
    marker_path.write_text("")
    subprocess.run(["sh", "-c", probe], check=False)
    assert sshd_started.exists()


def test_configure_ssh_command_writes_provisioned_marker() -> None:
    """build_configure_ssh_command must write the provisioned marker after the host key.

    The marker must be written after the `rm -f /etc/ssh/ssh_host_*` cleanup so it
    survives, and must not itself match that glob.
    """
    cmd = build_configure_ssh_command(
        user="root",
        client_public_key="ssh-ed25519 AAAA... user@host",
        host_private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nx\n-----END OPENSSH PRIVATE KEY-----",
        host_public_key="ssh-ed25519 BBBB... hostkey",
    )
    assert f"touch '{SSHD_PROVISIONED_MARKER_PATH}'" in cmd
    # The marker write must come after the host-key removal so it is not deleted.
    assert cmd.index("rm -f /etc/ssh/ssh_host_*") < cmd.index(f"touch '{SSHD_PROVISIONED_MARKER_PATH}'")
    # The marker must not be caught by the ssh_host_* removal glob.
    assert not SSHD_PROVISIONED_MARKER_PATH.rsplit("/", 1)[-1].startswith("ssh_host_")


def test_start_sshd_command_is_guarded_and_valid_shell() -> None:
    """The explicit sshd start must be valid shell and guarded against a running sshd."""
    cmd = build_start_sshd_command()
    assert subprocess.run(["sh", "-n", "-c", cmd], capture_output=True).returncode == 0
    assert "mkdir -p /run/sshd" in cmd
    assert "/usr/sbin/sshd -D" in cmd
    # The guard uses /proc (procps-free) rather than pgrep/pidof.
    assert "/proc/" in cmd
    assert "pgrep" not in cmd


def test_start_sshd_command_is_a_noop_when_sshd_is_already_running(tmp_path: Path) -> None:
    """When an sshd process is detected the guard must skip starting another one."""
    # Rewrite the root-only `mkdir -p /run/sshd` and the real sshd binary into
    # unprivileged probes so the guard's effect is observable without root.
    marker = tmp_path / "sshd_started"
    cmd = (
        build_start_sshd_command()
        .replace("mkdir -p /run/sshd && ", "")
        .replace("/usr/sbin/sshd -D -o MaxSessions=100", f"touch {marker}")
    )
    # Force the not-running check to report "already running" (false): start is skipped.
    running_cmd = cmd.replace("! grep -lxs sshd /proc/[0-9]*/comm >/dev/null 2>&1", "false")
    subprocess.run(["sh", "-c", running_cmd], check=False)
    assert not marker.exists()
    # When the check reports "not running" (true), the start runs.
    not_running_cmd = cmd.replace("! grep -lxs sshd /proc/[0-9]*/comm >/dev/null 2>&1", "true")
    subprocess.run(["sh", "-c", not_running_cmd], check=False)
    assert marker.exists()
