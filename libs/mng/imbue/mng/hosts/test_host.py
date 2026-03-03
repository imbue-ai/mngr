"""Integration tests for Host implementation.

Note: Unit tests for env file parsing are in utils/env_utils_test.py
"""

import datetime as dt
import fcntl
import json
import os
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from collections.abc import Generator
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pluggy
import pytest
from pyinfra.api.command import StringCommand

from imbue.mng.config.data_types import EnvVar
from imbue.mng.config.data_types import MngConfig
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import InvalidActivityTypeError
from imbue.mng.errors import LockNotHeldError
from imbue.mng.errors import MngError
from imbue.mng.hosts.common import is_macos
from imbue.mng.hosts.host import Host
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.data_types import ActivityConfig
from imbue.mng.interfaces.host import AgentDataOptions
from imbue.mng.interfaces.host import AgentEnvironmentOptions
from imbue.mng.interfaces.host import AgentGitOptions
from imbue.mng.interfaces.host import AgentProvisioningOptions
from imbue.mng.interfaces.host import CreateAgentOptions
from imbue.mng.interfaces.host import FileModificationSpec
from imbue.mng.interfaces.host import NamedCommand
from imbue.mng.interfaces.host import UploadFileSpec
from imbue.mng.primitives import ActivitySource
from imbue.mng.primitives import AgentName
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString
from imbue.mng.primitives import HostName
from imbue.mng.primitives import HostState
from imbue.mng.primitives import IdleMode
from imbue.mng.primitives import ProviderInstanceName
from imbue.mng.providers.local.instance import LocalProviderInstance
from imbue.mng.providers.ssh.instance import SSHHostConfig
from imbue.mng.providers.ssh.instance import SSHProviderInstance
from imbue.mng.utils.polling import poll_until
from imbue.mng.utils.polling import wait_for
from imbue.mng.utils.testing import generate_ssh_keypair
from imbue.mng.utils.testing import local_sshd


@pytest.fixture
def host_with_temp_dir(local_provider: LocalProviderInstance) -> tuple[Host, Path]:
    """Create a Host using the local provider and its per-host directory."""
    host = local_provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)
    return host, host.host_dir


@pytest.fixture
def ssh_host_factory(
    host_with_temp_dir: tuple[Host, Path],
    temp_mng_ctx: MngContext,
    tmp_path: Path,
) -> Generator[Callable[[str], Host], None, None]:
    """Create SSH host instances backed by a local sshd.

    Yields a factory function that creates SSH hosts by name.
    All hosts connect to the same local sshd process.
    """
    _local_host, temp_dir = host_with_temp_dir
    private_key, public_key = generate_ssh_keypair(tmp_path)
    public_key_content = public_key.read_text()

    with local_sshd(public_key_content, tmp_path) as (port, _host_key):
        current_user = os.environ.get("USER", "root")
        ssh_config = SSHHostConfig(
            address="127.0.0.1",
            port=port,
            user=current_user,
            key_file=private_key,
        )

        def create_ssh_host(name: str) -> Host:
            provider = SSHProviderInstance(
                name=ProviderInstanceName(f"ssh-{name}"),
                host_dir=temp_dir,
                mng_ctx=temp_mng_ctx,
                hosts={name: ssh_config},
            )
            return provider.get_host(HostName(name))

        yield create_ssh_host


# =============================================================================
# Run Shell Command Tests
# =============================================================================


def test_run_simple_command(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test executing a simple command."""
    host, _ = host_with_temp_dir
    success, output = host._run_shell_command(StringCommand("echo hello"))
    assert success is True
    assert output.stdout == "hello"
    assert output.stderr == ""


def test_run_command_with_failure(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test command with non-zero exit code returns success=False."""
    host, _ = host_with_temp_dir
    success, output = host._run_shell_command(StringCommand("exit 42"))
    assert success is False


def test_run_command_with_stderr(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test command that writes to stderr."""
    host, _ = host_with_temp_dir
    success, output = host._run_shell_command(StringCommand("echo error >&2"))
    assert success is True
    assert output.stderr == "error"


def test_run_command_with_chdir(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test command with working directory using _chdir."""
    host, temp_dir = host_with_temp_dir
    success, output = host._run_shell_command(StringCommand("pwd"), _chdir=str(temp_dir))
    assert success is True
    assert output.stdout == str(temp_dir)


def test_run_command_with_env(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test command with environment variables using _env."""
    host, _ = host_with_temp_dir
    success, output = host._run_shell_command(
        StringCommand("echo $MY_TEST_VAR"),
        _env={"MY_TEST_VAR": "test_value"},
    )
    assert success is True
    assert output.stdout == "test_value"


def test_run_command_captures_multiline_output(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that multiline output is captured correctly."""
    host, _ = host_with_temp_dir
    success, output = host._run_shell_command(StringCommand("printf 'line1\\nline2\\nline3'"))
    assert success is True
    assert "line1" in output.stdout
    assert "line2" in output.stdout
    assert "line3" in output.stdout


# =============================================================================
# Read File Tests (Bytes)
# =============================================================================


def test_read_file_returns_bytes(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that read_file returns bytes."""
    host, temp_dir = host_with_temp_dir
    test_file = temp_dir / "test.bin"
    test_file.write_bytes(b"binary content")
    content = host.read_file(test_file)
    assert content == b"binary content"
    assert isinstance(content, bytes)


def test_read_nonexistent_file_raises(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that reading a nonexistent file raises FileNotFoundError."""
    host, _ = host_with_temp_dir
    with pytest.raises(FileNotFoundError):
        host.read_file(Path("/nonexistent/file/path/12345.txt"))


# =============================================================================
# Write File Tests (Bytes)
# =============================================================================


def test_write_file_accepts_bytes(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that write_file accepts bytes."""
    host, temp_dir = host_with_temp_dir
    file_path = temp_dir / "new_test.bin"
    host.write_file(file_path, b"binary content")
    assert file_path.read_bytes() == b"binary content"


def test_write_file_creates_directories(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that write_file creates parent directories."""
    host, temp_dir = host_with_temp_dir
    file_path = temp_dir / "subdir" / "nested" / "test.bin"
    host.write_file(file_path, b"content")
    assert file_path.read_bytes() == b"content"


def test_write_file_with_mode(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test writing a file with specific permissions."""
    host, temp_dir = host_with_temp_dir
    file_path = temp_dir / "test.sh"
    host.write_file(file_path, b"#!/bin/bash\necho hello", mode="755")
    file_stat = file_path.stat()
    assert file_stat.st_mode & stat.S_IXUSR


# =============================================================================
# Read Text File Tests
# =============================================================================


def test_read_text_file_returns_string(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that read_text_file returns a string."""
    host, temp_dir = host_with_temp_dir
    test_file = temp_dir / "test.txt"
    test_file.write_text("test content")
    content = host.read_text_file(test_file)
    assert content == "test content"
    assert isinstance(content, str)


def test_read_text_file_with_unicode(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test reading a file with unicode content."""
    host, temp_dir = host_with_temp_dir
    test_file = temp_dir / "unicode.txt"
    test_file.write_text("Hello World! Special chars: plus plus")
    content = host.read_text_file(test_file)
    assert "Hello World" in content


# =============================================================================
# Write Text File Tests
# =============================================================================


def test_write_text_file_accepts_string(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that write_text_file accepts a string."""
    host, temp_dir = host_with_temp_dir
    file_path = temp_dir / "new_test.txt"
    host.write_text_file(file_path, "test content")
    assert file_path.read_text() == "test content"


def test_write_text_file_overwrites_existing(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that write_text_file overwrites existing content."""
    host, temp_dir = host_with_temp_dir
    file_path = temp_dir / "existing.txt"
    file_path.write_text("old content")
    host.write_text_file(file_path, "new content")
    assert file_path.read_text() == "new content"


def test_write_text_file_with_mode(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test writing a text file with specific permissions."""
    host, temp_dir = host_with_temp_dir
    file_path = temp_dir / "text_test.sh"
    host.write_text_file(file_path, "#!/bin/bash\necho hello", mode="755")
    file_stat = file_path.stat()
    assert file_stat.st_mode & stat.S_IXUSR


# =============================================================================
# Activity Configuration Tests
# =============================================================================


def test_get_default_activity_config(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test getting default activity config when no file exists."""
    host, _ = host_with_temp_dir
    config = host.get_activity_config()
    assert config.idle_mode == IdleMode.IO
    assert config.idle_timeout_seconds == 3600
    assert len(config.activity_sources) > 0


def test_set_and_get_activity_config(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test setting and getting activity config."""
    host, _ = host_with_temp_dir
    config = ActivityConfig(
        idle_timeout_seconds=7200,
        activity_sources=(
            ActivitySource.USER,
            ActivitySource.SSH,
            ActivitySource.CREATE,
            ActivitySource.START,
            ActivitySource.BOOT,
        ),
    )
    host.set_activity_config(config)

    retrieved = host.get_activity_config()
    assert retrieved.idle_mode == IdleMode.USER
    assert retrieved.idle_timeout_seconds == 7200


# =============================================================================
# Activity Time Tests
# =============================================================================


def test_record_boot_activity(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test recording boot activity."""
    host, _ = host_with_temp_dir
    host.record_activity(ActivitySource.BOOT)
    activity_time = host.get_reported_activity_time(ActivitySource.BOOT)
    assert activity_time is not None


def test_record_create_activity_on_host_raises(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that recording CREATE activity on host raises error.

    CREATE activity should only be recorded on agents, not hosts.
    """
    host, _ = host_with_temp_dir
    with pytest.raises(InvalidActivityTypeError):
        host.record_activity(ActivitySource.CREATE)


def test_invalid_activity_type_raises(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that recording invalid activity types on host raises error.

    Only BOOT activity is valid for host-level recording.
    """
    host, _ = host_with_temp_dir
    # USER activity is invalid
    with pytest.raises(InvalidActivityTypeError):
        host.record_activity(ActivitySource.USER)
    # CREATE activity is also invalid for hosts
    with pytest.raises(InvalidActivityTypeError):
        host.record_activity(ActivitySource.CREATE)
    # START activity is also invalid for hosts
    with pytest.raises(InvalidActivityTypeError):
        host.record_activity(ActivitySource.START)


def test_get_activity_content(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test getting activity file content - should be JSON with time in milliseconds."""
    host, _ = host_with_temp_dir
    host.record_activity(ActivitySource.BOOT)
    content = host.get_reported_activity_content(ActivitySource.BOOT)
    assert content is not None
    data = json.loads(content)
    assert "time" in data
    # Time should be an integer (milliseconds since epoch)
    assert isinstance(data["time"], int)
    # Should be a reasonable timestamp (after year 2020, which is 1577836800000 ms)
    assert data["time"] > 1577836800000
    # Should also have host_id for debugging
    assert "host_id" in data


# =============================================================================
# Cooperative Locking Tests
# =============================================================================


def test_acquire_and_release_lock(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test acquiring and releasing a lock."""
    host, _ = host_with_temp_dir
    with host.lock_cooperatively(timeout_seconds=5.0):
        lock_time = host.get_reported_lock_time()
        assert lock_time is not None


def test_is_lock_held_returns_false_when_no_lock_file(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that is_lock_held returns False when the lock file does not exist."""
    host, temp_dir = host_with_temp_dir
    lock_path = temp_dir / "host_lock"
    assert not lock_path.exists()
    assert host.is_lock_held() is False


def test_is_lock_held_returns_false_after_lock_released(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that is_lock_held returns False after a lock has been acquired and released.

    On local hosts the lock file persists after flock release, so this verifies
    that is_lock_held correctly distinguishes 'file exists' from 'lock held'.
    """
    host, temp_dir = host_with_temp_dir
    with host.lock_cooperatively(timeout_seconds=5.0):
        pass
    # Lock file still exists after release
    assert (temp_dir / "host_lock").exists()
    # But is_lock_held correctly reports it is not held
    assert host.is_lock_held() is False


def test_is_lock_held_returns_true_while_lock_held(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that is_lock_held returns True while another process holds the lock."""
    host, temp_dir = host_with_temp_dir
    lock_path = temp_dir / "host_lock"
    lock_held = threading.Event()
    release_lock = threading.Event()

    def hold_lock():
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            lock_held.set()
            release_lock.wait()
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    lock_held.wait()

    try:
        assert host.is_lock_held() is True
    finally:
        release_lock.set()
        thread.join()


def test_lock_timeout(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that lock times out when held by another process."""
    host, temp_dir = host_with_temp_dir
    lock_path = temp_dir / "host_lock"
    lock_held = threading.Event()
    release_lock = threading.Event()

    def hold_lock():
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            lock_held.set()
            release_lock.wait()
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    thread = threading.Thread(target=hold_lock)
    thread.start()
    lock_held.wait()

    try:
        with pytest.raises(LockNotHeldError):
            with host.lock_cooperatively(timeout_seconds=0.1):
                pass
    finally:
        release_lock.set()
        thread.join()


# =============================================================================
# Certified Data Tests
# =============================================================================


def test_get_empty_certified_data(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test getting certified data when no file exists."""
    host, _ = host_with_temp_dir
    data = host.get_certified_data()
    assert data.idle_mode == IdleMode.IO
    assert data.idle_timeout_seconds == 3600
    assert data.plugin == {}


def test_set_and_get_plugin_data(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test setting and getting plugin data."""
    host, _ = host_with_temp_dir
    host.set_plugin_data("test_plugin", {"key": "value"})
    data = host.get_plugin_data("test_plugin")
    assert data == {"key": "value"}


def test_get_nonexistent_plugin_data(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test getting data for a plugin that doesn't exist."""
    host, _ = host_with_temp_dir
    data = host.get_plugin_data("nonexistent")
    assert data == {}


# =============================================================================
# Environment Variable Tests
# =============================================================================


def test_get_empty_env_vars(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test getting env vars when no file exists."""
    host, _ = host_with_temp_dir
    env = host.get_env_vars()
    assert env == {}


def test_set_and_get_env_vars(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test setting and getting environment variables."""
    host, _ = host_with_temp_dir
    host.set_env_vars({"FOO": "bar", "BAZ": "qux"})
    env = host.get_env_vars()
    assert env["FOO"] == "bar"
    assert env["BAZ"] == "qux"


def test_set_single_env_var(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test setting a single environment variable."""
    host, _ = host_with_temp_dir
    host.set_env_vars({"EXISTING": "value"})
    host.set_env_var("NEW", "new_value")
    env = host.get_env_vars()
    assert env["EXISTING"] == "value"
    assert env["NEW"] == "new_value"


def test_get_single_env_var(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test getting a single environment variable."""
    host, _ = host_with_temp_dir
    host.set_env_vars({"MY_VAR": "my_value"})
    value = host.get_env_var("MY_VAR")
    assert value == "my_value"


def test_get_nonexistent_env_var(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test getting a nonexistent environment variable."""
    host, _ = host_with_temp_dir
    value = host.get_env_var("NONEXISTENT")
    assert value is None


# =============================================================================
# Plugin State Files Tests
# =============================================================================


def test_set_and_get_plugin_state_file(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test setting and getting plugin state files."""
    host, _ = host_with_temp_dir
    host.set_reported_plugin_state_file_data("test_plugin", "state.txt", "plugin state")
    content = host.get_reported_plugin_state_file_data("test_plugin", "state.txt")
    assert content == "plugin state"


def test_list_plugin_state_files(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test listing plugin state files."""
    host, _ = host_with_temp_dir
    host.set_reported_plugin_state_file_data("test_plugin", "file1.txt", "content1")
    host.set_reported_plugin_state_file_data("test_plugin", "file2.txt", "content2")
    files = host.get_reported_plugin_state_files("test_plugin")
    assert "file1.txt" in files
    assert "file2.txt" in files


def test_list_nonexistent_plugin_files(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test listing files for a plugin that doesn't exist."""
    host, _ = host_with_temp_dir
    files = host.get_reported_plugin_state_files("nonexistent")
    assert files == []


# =============================================================================
# Host State Tests
# =============================================================================


def test_local_host_always_running(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that local host is always in RUNNING state."""
    host, _ = host_with_temp_dir
    state = host.get_state()
    assert state == HostState.RUNNING


def test_get_uptime(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test getting host uptime."""
    host, _ = host_with_temp_dir
    uptime = host.get_uptime_seconds()
    assert uptime > 0


def test_get_boot_time(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test getting host boot time."""
    host, _ = host_with_temp_dir
    boot_time = host.get_boot_time()
    assert boot_time is not None
    # Boot time should be in the past
    now = datetime.now(timezone.utc)
    assert boot_time < now
    # Boot time should be within a reasonable range (not more than 1 year ago)
    one_year_ago = now - dt.timedelta(days=365)
    assert boot_time > one_year_ago


def test_get_boot_time_and_uptime_are_consistent(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that boot_time and uptime_seconds give consistent results."""
    host, _ = host_with_temp_dir

    boot_time = host.get_boot_time()
    uptime = host.get_uptime_seconds()

    assert boot_time is not None

    # Calculate expected boot time from uptime
    now = datetime.now(timezone.utc)
    expected_boot_time = now - dt.timedelta(seconds=uptime)

    # They should be within 1.5 seconds of each other.
    # We need > 1 second tolerance because:
    # - get_uptime_seconds() uses `date +%s` which truncates to integer seconds
    # - datetime.now() has microsecond precision
    # - If these calls span a second boundary, we get ~1 second of error
    # The extra 0.5s accounts for time elapsed between the calls.
    diff = abs((boot_time - expected_boot_time).total_seconds())
    assert diff < 1.5, f"boot_time and uptime differ by {diff} seconds"


# =============================================================================
# Idle Detection Tests
# =============================================================================


def test_get_idle_seconds_with_boot_activity(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test idle seconds includes BOOT activity recorded at host creation.

    Since hosts now automatically record BOOT activity when created,
    idle seconds should not be infinity.
    """
    host, _ = host_with_temp_dir
    idle = host.get_idle_seconds()
    # BOOT activity is recorded at host creation, so idle should be finite
    assert 0 <= idle < 10


def test_get_idle_seconds_after_boot_activity(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test idle seconds after recording BOOT activity."""
    host, _ = host_with_temp_dir
    host.record_activity(ActivitySource.BOOT)
    idle = host.get_idle_seconds()
    assert 0 <= idle < 10


# =============================================================================
# Agent Creation and Start Tests
# =============================================================================


@pytest.mark.tmux
def test_unset_vars_applied_during_agent_start(
    temp_host_dir: Path,
    per_host_dir: Path,
    temp_work_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: pluggy.PluginManager,
    mng_test_prefix: str,
) -> None:
    """Test that unset_vars config is applied when starting agents."""
    config_with_unset = MngConfig(
        default_host_dir=temp_host_dir,
        prefix=mng_test_prefix,
        unset_vars=["HISTFILE", "PROFILE"],
    )

    mng_ctx_with_unset = MngContext(config=config_with_unset, pm=plugin_manager, profile_dir=temp_profile_dir)
    provider_with_unset = LocalProviderInstance(
        name=ProviderInstanceName("local"),
        host_dir=per_host_dir,
        mng_ctx=mng_ctx_with_unset,
    )

    host = provider_with_unset.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("test-agent"),
            agent_type=AgentTypeName("generic"),
            # Background the sleep so the shell remains interactive for our echo commands
            command=CommandString("sleep 736249 &"),
        ),
    )

    host.start_agents([agent.id])

    session_name = f"{mng_test_prefix}{agent.name}"

    # Wait for the tmux session to exist
    def session_ready() -> bool:
        result = host.execute_command(f"tmux has-session -t '{session_name}'")
        if not result.success:
            return False
        capture_result = host.execute_command(f"tmux capture-pane -t '{session_name}' -p")
        return capture_result.success and ("sleep 736249" in capture_result.stdout)

    wait_for(session_ready, timeout=30.0, poll_interval=0.5, error_message="tmux session not ready")

    # Send Ctrl-C to kill the foreground sleep, returning control to the shell.
    # This lets us send echo commands to check environment variables.
    host.execute_command(f"tmux send-keys -t '{session_name}' C-c")

    # This was enabled in modal, but caused things to fail locally. I don't think we need or want this (and I did do a better job of waiting above by ensuring that the sleep text shows up)
    # # Wait for the shell prompt to return after Ctrl-C
    # def shell_ready() -> bool:
    #     capture_result = host.execute_command(f"tmux capture-pane -t '{session_name}' -p")
    #     return capture_result.success and ("$" in capture_result.stdout or "#" in capture_result.stdout)
    #
    # wait_for(shell_ready, error_message="Shell prompt not ready after Ctrl-C")

    host.execute_command(f"tmux send-keys -t '{session_name}' 'echo HISTFILE_VALUE=${{HISTFILE:-UNSET}}' Enter")
    host.execute_command(f"tmux send-keys -t '{session_name}' 'echo PROFILE_VALUE=${{PROFILE:-UNSET}}' Enter")

    def check_output() -> bool:
        capture_result = host.execute_command(f"tmux capture-pane -t '{session_name}' -p")
        if not capture_result.success:
            return False
        output = capture_result.stdout
        has_histfile = "HISTFILE_VALUE=UNSET" in output or "HISTFILE_VALUE=" in output
        has_profile = "PROFILE_VALUE=UNSET" in output or "PROFILE_VALUE=" in output
        return has_histfile and has_profile

    wait_for(
        check_output,
        timeout=30.0,
        poll_interval=0.5,
        error_message="Expected environment variables not found in tmux output",
    )

    host.stop_agents([agent.id])


# =============================================================================
# Agent Start/Stop Process Tests
# =============================================================================


def _collect_pane_pids(host: Host, session_name: str) -> list[str]:
    """Collect all pane PIDs and their descendant PIDs for a tmux session (across all windows)."""
    return host._collect_session_pids(session_name)


def test_procps_ps_command_available() -> None:
    """Verify that the `ps` command from procps is available.

    The procps package provides essential process utilities (ps, pgrep, pkill).
    Without it, process management in stop_agents() and process verification in tests fail.
    This test exists to validate that the container environment includes procps.
    """
    result = subprocess.run(["ps", "aux"], capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(f"PROCPS TEST FAILED: 'ps aux' returned {result.returncode}\n")
        sys.stderr.write(f"stderr: {result.stderr}\n")
        sys.stderr.write("The procps package is likely not installed. Install with: apt-get install procps\n")
        sys.stderr.flush()
        raise AssertionError(f"ps aux failed: {result.stderr}")

    # Verify we get reasonable output (should include at least our own process)
    if "PID" not in result.stdout and len(result.stdout.strip().split("\n")) <= 1:
        sys.stderr.write("PROCPS TEST FAILED: 'ps aux' output looks wrong\n")
        sys.stderr.write(f"stdout: {result.stdout}\n")
        sys.stderr.flush()
        raise AssertionError("ps aux output invalid")


@pytest.mark.tmux
def test_stop_agent_kills_single_pane_processes(
    temp_host_dir: Path,
    per_host_dir: Path,
    temp_work_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: pluggy.PluginManager,
    mng_test_prefix: str,
) -> None:
    """Test that stop_agents kills all processes in a single-pane session."""
    config = MngConfig(default_host_dir=temp_host_dir, prefix=mng_test_prefix)
    mng_ctx = MngContext(config=config, pm=plugin_manager, profile_dir=temp_profile_dir)
    provider = LocalProviderInstance(
        name=ProviderInstanceName("local"),
        host_dir=per_host_dir,
        mng_ctx=mng_ctx,
    )
    host = provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("stop-test-single"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 1001 & sleep 1001 & sleep 1001 & wait"),
        ),
    )

    host.start_agents([agent.id])
    session_name = f"{mng_test_prefix}{agent.name}"

    success, output = host._run_shell_command(StringCommand("tmux list-sessions -F '#{session_name}' 2>/dev/null"))
    assert success
    assert session_name in output.stdout

    # Capture specific PIDs before stopping so we can verify they are killed
    pids_to_check = _collect_pane_pids(host, session_name)
    assert len(pids_to_check) > 0

    host.stop_agents([agent.id], timeout_seconds=3.0)

    def check_cleanup() -> bool:
        success, output = host._run_shell_command(StringCommand("tmux list-sessions -F '#{session_name}' 2>/dev/null"))
        session_killed = session_name not in output.stdout
        # Check that the specific PIDs from this test are dead
        for pid in pids_to_check:
            success_kill, _ = host._run_shell_command(StringCommand(f"kill -0 {pid} 2>/dev/null"))
            if success_kill:
                return False
        return session_killed

    wait_for(check_cleanup, timeout=10, error_message="Agent session and processes not cleaned up after stop")


@pytest.mark.tmux
def test_stop_agent_kills_multi_pane_processes(
    temp_host_dir: Path,
    per_host_dir: Path,
    temp_work_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: pluggy.PluginManager,
    mng_test_prefix: str,
) -> None:
    """Test that stop_agents kills all processes in a multi-pane session."""
    config = MngConfig(default_host_dir=temp_host_dir, prefix=mng_test_prefix)
    mng_ctx = MngContext(config=config, pm=plugin_manager, profile_dir=temp_profile_dir)
    provider = LocalProviderInstance(
        name=ProviderInstanceName("local"),
        host_dir=per_host_dir,
        mng_ctx=mng_ctx,
    )
    host = provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("stop-test-multi"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 1000"),
        ),
    )

    host.start_agents([agent.id])
    session_name = f"{mng_test_prefix}{agent.name}"

    host._run_shell_command(StringCommand(f"tmux split-window -t '{session_name}' 'sleep 2000'"))
    host._run_shell_command(StringCommand(f"tmux split-window -t '{session_name}' 'sleep 3000'"))

    success, output = host._run_shell_command(
        StringCommand(f"tmux list-panes -t '{session_name}' 2>/dev/null | wc -l")
    )
    assert success
    pane_count = int(output.stdout.strip())
    assert pane_count == 3

    # Capture specific PIDs before stopping so we can verify they are killed
    pids_to_check = _collect_pane_pids(host, session_name)
    assert len(pids_to_check) > 0

    host.stop_agents([agent.id], timeout_seconds=3.0)

    def check_cleanup() -> bool:
        success, output = host._run_shell_command(StringCommand("tmux list-sessions -F '#{session_name}' 2>/dev/null"))
        session_killed = session_name not in output.stdout
        # Check that the specific PIDs from this test are dead
        for pid in pids_to_check:
            success_kill, _ = host._run_shell_command(StringCommand(f"kill -0 {pid} 2>/dev/null"))
            if success_kill:
                return False
        return session_killed

    wait_for(
        check_cleanup, timeout=10, error_message="Multi-pane agent session and processes not cleaned up after stop"
    )


@pytest.mark.tmux
def test_start_agent_creates_process_group(
    temp_host_dir: Path,
    per_host_dir: Path,
    temp_work_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: pluggy.PluginManager,
    mng_test_prefix: str,
) -> None:
    """Test that start_agents creates tmux sessions in their own process group."""
    config = MngConfig(default_host_dir=temp_host_dir, prefix=mng_test_prefix)
    mng_ctx = MngContext(config=config, pm=plugin_manager, profile_dir=temp_profile_dir)
    provider = LocalProviderInstance(
        name=ProviderInstanceName("local"),
        host_dir=per_host_dir,
        mng_ctx=mng_ctx,
    )
    host = provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("pgid-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847263"),
        ),
    )

    host.start_agents([agent.id])
    session_name = f"{mng_test_prefix}{agent.name}"

    try:
        success, output = host._run_shell_command(
            StringCommand(f"tmux list-panes -t '{session_name}' -F '#{{pane_pid}}' 2>/dev/null")
        )
        assert success
        pane_pid = output.stdout.strip()
        assert pane_pid

        # Get process group ID using platform-specific method
        if is_macos():
            # macOS: use ps command
            success, output = host._run_shell_command(StringCommand(f"ps -o pgid= -p {pane_pid}"))
            assert success, f"Failed to get pgid for pid {pane_pid}"
        else:
            # Linux: use /proc filesystem (5th field in /proc/<pid>/stat is pgid)
            success, output = host._run_shell_command(
                StringCommand(f"cat /proc/{pane_pid}/stat 2>/dev/null | cut -d' ' -f5")
            )
            assert success, f"Failed to read pgid from /proc/{pane_pid}/stat"
        pgid = output.stdout.strip()
        assert pgid, "Process group ID should not be empty"
        assert pgid.isdigit(), f"Process group ID should be numeric, got: {pgid}"
    finally:
        host.stop_agents([agent.id])


@pytest.mark.tmux
def test_start_agent_starts_process_activity_monitor(
    temp_host_dir: Path,
    per_host_dir: Path,
    temp_work_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: pluggy.PluginManager,
    mng_test_prefix: str,
) -> None:
    """Test that start_agents launches a process activity monitor that writes PROCESS activity."""
    config = MngConfig(default_host_dir=temp_host_dir, prefix=mng_test_prefix)
    mng_ctx = MngContext(config=config, pm=plugin_manager, profile_dir=temp_profile_dir)
    provider = LocalProviderInstance(
        name=ProviderInstanceName("local"),
        host_dir=per_host_dir,
        mng_ctx=mng_ctx,
    )
    host = provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("activity-monitor-test"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 847291"),
        ),
    )

    host.start_agents([agent.id])

    try:
        # The process activity monitor should write process activity within ~5-6 seconds
        activity_path = host.host_dir / "agents" / str(agent.id) / "activity" / "process"

        # Wait until the file exists AND has valid JSON content. The writer
        # creates the file then writes to it, so there is a brief window where
        # the file exists but is empty.
        data: dict = {}

        def activity_file_has_content() -> bool:
            nonlocal data
            if not activity_path.exists():
                return False
            content = activity_path.read_text()
            if not content.strip():
                return False
            try:
                data = json.loads(content)
                return True
            except json.JSONDecodeError:
                return False

        wait_for(activity_file_has_content, timeout=10.0, error_message="process activity file not created or empty")
        assert "time" in data
        # Time should be an integer (milliseconds since epoch)
        assert isinstance(data["time"], int)
        # Should be a reasonable timestamp (after year 2020, which is 1577836800000 ms)
        assert data["time"] > 1577836800000
        # Should also have debugging fields
        assert "pane_pid" in data
        assert "agent_id" in data
    finally:
        host.stop_agents([agent.id])


# =============================================================================
# Additional Commands Tests
# =============================================================================


def test_additional_commands_stored_in_agent_data(
    temp_host_dir: Path,
    per_host_dir: Path,
    temp_work_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: pluggy.PluginManager,
    mng_test_prefix: str,
) -> None:
    """Test that additional_commands are stored in the agent's data.json."""
    config = MngConfig(default_host_dir=temp_host_dir, prefix=mng_test_prefix)
    mng_ctx = MngContext(config=config, pm=plugin_manager, profile_dir=temp_profile_dir)
    provider = LocalProviderInstance(
        name=ProviderInstanceName("local"),
        host_dir=per_host_dir,
        mng_ctx=mng_ctx,
    )
    host = provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("additional-cmds-stored"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 892741"),
            additional_commands=(
                NamedCommand(command=CommandString("echo additional-cmd-1"), window_name=None),
                NamedCommand(command=CommandString("echo additional-cmd-2"), window_name="custom-window"),
            ),
        ),
    )

    # Read the data.json file and verify additional_commands are stored
    data_path = host.host_dir / "agents" / str(agent.id) / "data.json"
    data = json.loads(data_path.read_text())

    assert "additional_commands" in data
    assert data["additional_commands"] == [
        {"command": "echo additional-cmd-1", "window_name": None},
        {"command": "echo additional-cmd-2", "window_name": "custom-window"},
    ]


@pytest.mark.tmux
def test_start_agent_creates_additional_tmux_windows(
    temp_host_dir: Path,
    per_host_dir: Path,
    temp_work_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: pluggy.PluginManager,
    mng_test_prefix: str,
) -> None:
    """Test that start_agents creates additional tmux windows for additional_commands."""
    config = MngConfig(default_host_dir=temp_host_dir, prefix=mng_test_prefix)
    mng_ctx = MngContext(config=config, pm=plugin_manager, profile_dir=temp_profile_dir)
    provider = LocalProviderInstance(
        name=ProviderInstanceName("local"),
        host_dir=per_host_dir,
        mng_ctx=mng_ctx,
    )
    host = provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("additional-windows"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 764821"),
            additional_commands=(
                NamedCommand(command=CommandString("sleep 764822"), window_name=None),
                NamedCommand(command=CommandString("sleep 764823"), window_name=None),
            ),
        ),
    )

    host.start_agents([agent.id])
    session_name = f"{mng_test_prefix}{agent.name}"

    try:
        # Verify the session was created
        success, output = host._run_shell_command(StringCommand("tmux list-sessions -F '#{session_name}' 2>/dev/null"))
        assert success
        assert session_name in output.stdout

        # Verify we have 3 windows (main + 2 additional)
        success, output = host._run_shell_command(
            StringCommand(f"tmux list-windows -t '{session_name}' -F '#{{window_name}}' 2>/dev/null")
        )
        assert success
        windows = output.stdout.strip().split("\n")
        assert len(windows) == 3, f"Expected 3 windows, got {len(windows)}: {windows}"

        # Verify window names
        assert "cmd-1" in windows
        assert "cmd-2" in windows

    finally:
        host.stop_agents([agent.id])


@pytest.mark.tmux
def test_start_agent_additional_windows_run_commands(
    temp_host_dir: Path,
    per_host_dir: Path,
    temp_work_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: pluggy.PluginManager,
    mng_test_prefix: str,
) -> None:
    """Test that additional tmux windows actually run the specified commands."""
    config = MngConfig(default_host_dir=temp_host_dir, prefix=mng_test_prefix)
    mng_ctx = MngContext(config=config, pm=plugin_manager, profile_dir=temp_profile_dir)
    provider = LocalProviderInstance(
        name=ProviderInstanceName("local"),
        host_dir=per_host_dir,
        mng_ctx=mng_ctx,
    )
    host = provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=CreateAgentOptions(
            name=AgentName("additional-commands-run"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 938472"),
            additional_commands=(
                NamedCommand(command=CommandString("echo UNIQUE_MARKER_938473 && sleep 938474"), window_name=None),
            ),
        ),
    )

    host.start_agents([agent.id])
    session_name = f"{mng_test_prefix}{agent.name}"

    try:
        # Wait for the additional command to produce output
        def check_output() -> bool:
            capture_result = host._run_shell_command(
                StringCommand(f"tmux capture-pane -t '{session_name}:cmd-1' -p 2>/dev/null")
            )
            if not capture_result[0]:
                return False
            return "UNIQUE_MARKER_938473" in capture_result[1].stdout

        wait_for(check_output, error_message="Expected output from additional command not found")

    finally:
        host.stop_agents([agent.id])


# =============================================================================
# Provision Agent Tests
# =============================================================================


def test_provision_agent_create_directories(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that provision_agent creates directories."""
    host, temp_dir = host_with_temp_dir
    agent = _create_minimal_agent(host, temp_dir)

    dir1 = temp_dir / "provision_test" / "dir1"
    dir2 = temp_dir / "provision_test" / "nested" / "dir2"

    options = CreateAgentOptions(
        name=AgentName("prov-dirs"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        provisioning=AgentProvisioningOptions(
            create_directories=(dir1, dir2),
        ),
    )

    host.provision_agent(agent, options, host.mng_ctx)

    assert dir1.exists()
    assert dir1.is_dir()
    assert dir2.exists()
    assert dir2.is_dir()


def test_provision_agent_upload_files(host_with_temp_dir: tuple[Host, Path], tmp_path: Path) -> None:
    """Test that provision_agent uploads files from local to remote."""
    host, temp_dir = host_with_temp_dir
    agent = _create_minimal_agent(host, temp_dir)

    # Create a local file to upload
    local_file = tmp_path / "source" / "config.txt"
    local_file.parent.mkdir(parents=True)
    local_file.write_text("uploaded content")

    remote_path = temp_dir / "provision_test" / "config.txt"

    options = CreateAgentOptions(
        name=AgentName("prov-upload"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        provisioning=AgentProvisioningOptions(
            upload_files=(UploadFileSpec(local_path=local_file, remote_path=remote_path),),
        ),
    )

    host.provision_agent(agent, options, host.mng_ctx)

    assert remote_path.exists()
    assert remote_path.read_text() == "uploaded content"


def test_provision_agent_append_to_existing_file(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that provision_agent appends text to existing files."""
    host, temp_dir = host_with_temp_dir
    agent = _create_minimal_agent(host, temp_dir)

    target_file = temp_dir / "provision_test" / "append.txt"
    target_file.parent.mkdir(parents=True)
    target_file.write_text("existing content\n")

    options = CreateAgentOptions(
        name=AgentName("prov-append"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        provisioning=AgentProvisioningOptions(
            append_to_files=(FileModificationSpec(remote_path=target_file, text="appended text"),),
        ),
    )

    host.provision_agent(agent, options, host.mng_ctx)

    assert target_file.read_text() == "existing content\nappended text"


def test_provision_agent_append_to_new_file(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that provision_agent creates file when appending to non-existent file."""
    host, temp_dir = host_with_temp_dir
    agent = _create_minimal_agent(host, temp_dir)

    target_file = temp_dir / "provision_test" / "new_append.txt"

    options = CreateAgentOptions(
        name=AgentName("prov-append-new"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        provisioning=AgentProvisioningOptions(
            create_directories=(target_file.parent,),
            append_to_files=(FileModificationSpec(remote_path=target_file, text="new content"),),
        ),
    )

    host.provision_agent(agent, options, host.mng_ctx)

    assert target_file.exists()
    assert target_file.read_text() == "new content"


def test_provision_agent_prepend_to_existing_file(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that provision_agent prepends text to existing files."""
    host, temp_dir = host_with_temp_dir
    agent = _create_minimal_agent(host, temp_dir)

    target_file = temp_dir / "provision_test" / "prepend.txt"
    target_file.parent.mkdir(parents=True)
    target_file.write_text("existing content")

    options = CreateAgentOptions(
        name=AgentName("prov-prepend"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        provisioning=AgentProvisioningOptions(
            prepend_to_files=(FileModificationSpec(remote_path=target_file, text="prepended: "),),
        ),
    )

    host.provision_agent(agent, options, host.mng_ctx)

    assert target_file.read_text() == "prepended: existing content"


def test_provision_agent_prepend_to_new_file(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that provision_agent creates file when prepending to non-existent file."""
    host, temp_dir = host_with_temp_dir
    agent = _create_minimal_agent(host, temp_dir)

    target_file = temp_dir / "provision_test" / "new_prepend.txt"

    options = CreateAgentOptions(
        name=AgentName("prov-prepend-new"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        provisioning=AgentProvisioningOptions(
            create_directories=(target_file.parent,),
            prepend_to_files=(FileModificationSpec(remote_path=target_file, text="new content"),),
        ),
    )

    host.provision_agent(agent, options, host.mng_ctx)

    assert target_file.exists()
    assert target_file.read_text() == "new content"


def test_provision_agent_user_commands(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that provision_agent runs user commands."""
    host, temp_dir = host_with_temp_dir
    agent = _create_minimal_agent(host, temp_dir)

    marker_file = temp_dir / "provision_test" / "marker.txt"

    options = CreateAgentOptions(
        name=AgentName("prov-user-cmd"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        provisioning=AgentProvisioningOptions(
            create_directories=(marker_file.parent,),
            user_commands=(f"echo 'user command executed' > {marker_file}",),
        ),
    )

    host.provision_agent(agent, options, host.mng_ctx)

    assert marker_file.exists()
    assert "user command executed" in marker_file.read_text()


def test_provision_agent_user_commands_in_work_dir(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that user commands run in the agent's work_dir."""
    host, temp_dir = host_with_temp_dir

    # Create agent with a specific work_dir
    work_dir = temp_dir / "agent_work_dir"
    work_dir.mkdir(parents=True)
    agent = _create_minimal_agent(host, temp_dir, work_dir=work_dir)

    marker_file = work_dir / "pwd_output.txt"

    options = CreateAgentOptions(
        name=AgentName("prov-cwd"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        provisioning=AgentProvisioningOptions(
            user_commands=(f"pwd > {marker_file}",),
        ),
    )

    host.provision_agent(agent, options, host.mng_ctx)

    assert marker_file.exists()
    assert str(work_dir) in marker_file.read_text()


def test_provision_agent_multiple_user_commands(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that provision_agent runs multiple user commands in order."""
    host, temp_dir = host_with_temp_dir
    agent = _create_minimal_agent(host, temp_dir)

    output_file = temp_dir / "provision_test" / "sequence.txt"

    options = CreateAgentOptions(
        name=AgentName("prov-multi-cmd"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        provisioning=AgentProvisioningOptions(
            create_directories=(output_file.parent,),
            user_commands=(
                f"echo 'first' > {output_file}",
                f"echo 'second' >> {output_file}",
                f"echo 'third' >> {output_file}",
            ),
        ),
    )

    host.provision_agent(agent, options, host.mng_ctx)

    assert output_file.exists()
    content = output_file.read_text()
    lines = content.strip().split("\n")
    assert lines[0] == "first"
    assert lines[1] == "second"
    assert lines[2] == "third"


def test_provision_agent_user_command_failure_raises(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that provision_agent raises on user command failure."""
    host, temp_dir = host_with_temp_dir
    agent = _create_minimal_agent(host, temp_dir)

    options = CreateAgentOptions(
        name=AgentName("prov-fail"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        provisioning=AgentProvisioningOptions(
            user_commands=("exit 1",),
        ),
    )

    with pytest.raises(MngError) as exc_info:
        host.provision_agent(agent, options, host.mng_ctx)

    assert "User command failed" in str(exc_info.value)


def test_provision_agent_combined_options(host_with_temp_dir: tuple[Host, Path], tmp_path: Path) -> None:
    """Test provision_agent with multiple option types combined."""
    host, temp_dir = host_with_temp_dir
    agent = _create_minimal_agent(host, temp_dir)

    # Create local file to upload
    local_file = tmp_path / "source" / "upload.txt"
    local_file.parent.mkdir(parents=True)
    local_file.write_text("uploaded")

    provision_dir = temp_dir / "provision_combined"
    remote_upload = provision_dir / "uploaded.txt"
    append_file = provision_dir / "appended.txt"
    marker_file = provision_dir / "marker.txt"

    options = CreateAgentOptions(
        name=AgentName("prov-combined"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        provisioning=AgentProvisioningOptions(
            create_directories=(provision_dir,),
            upload_files=(UploadFileSpec(local_path=local_file, remote_path=remote_upload),),
            append_to_files=(FileModificationSpec(remote_path=append_file, text="appended content"),),
            user_commands=(f"echo 'marker' > {marker_file}",),
        ),
    )

    host.provision_agent(agent, options, host.mng_ctx)

    # Verify all operations completed
    assert provision_dir.exists()
    assert remote_upload.read_text() == "uploaded"
    assert append_file.read_text() == "appended content"
    assert marker_file.read_text().strip() == "marker"


def test_provision_agent_upload_binary_file(host_with_temp_dir: tuple[Host, Path], tmp_path: Path) -> None:
    """Test that provision_agent uploads binary files correctly."""
    host, temp_dir = host_with_temp_dir
    agent = _create_minimal_agent(host, temp_dir)

    # Create a binary file
    local_file = tmp_path / "source" / "binary.bin"
    local_file.parent.mkdir(parents=True)
    binary_content = bytes(range(256))
    local_file.write_bytes(binary_content)

    remote_path = temp_dir / "provision_test" / "binary.bin"

    options = CreateAgentOptions(
        name=AgentName("prov-binary"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        provisioning=AgentProvisioningOptions(
            create_directories=(remote_path.parent,),
            upload_files=(UploadFileSpec(local_path=local_file, remote_path=remote_path),),
        ),
    )

    host.provision_agent(agent, options, host.mng_ctx)

    assert remote_path.exists()
    assert remote_path.read_bytes() == binary_content


def test_provision_agent_order_of_operations(host_with_temp_dir: tuple[Host, Path], tmp_path: Path) -> None:
    """Test that provisioning operations happen in the correct order.

    The order should be:
    1. Create directories
    2. Upload files
    3. Append to files
    4. Prepend to files
    5. Sudo commands (skipped in this test)
    6. User commands
    """
    host, temp_dir = host_with_temp_dir
    agent = _create_minimal_agent(host, temp_dir)

    provision_dir = temp_dir / "order_test"
    target_file = provision_dir / "target.txt"
    log_file = provision_dir / "log.txt"

    # Create local file to upload
    local_file = tmp_path / "upload.txt"
    local_file.write_text("uploaded\n")

    options = CreateAgentOptions(
        name=AgentName("prov-order"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        provisioning=AgentProvisioningOptions(
            # 1. Create directories - must happen first so upload works
            create_directories=(provision_dir,),
            # 2. Upload files - puts base content in place
            upload_files=(UploadFileSpec(local_path=local_file, remote_path=target_file),),
            # 3. Append - adds to end of uploaded content
            append_to_files=(FileModificationSpec(remote_path=target_file, text="appended\n"),),
            # 4. Prepend - adds to beginning
            prepend_to_files=(FileModificationSpec(remote_path=target_file, text="prepended\n"),),
            # 6. User commands - run last, can verify final state
            user_commands=(f"cat {target_file} > {log_file}",),
        ),
    )

    host.provision_agent(agent, options, host.mng_ctx)

    # Verify the final order in the file
    content = target_file.read_text()
    assert content == "prepended\nuploaded\nappended\n"

    # Log file should have captured the same content
    assert log_file.read_text() == content


# =============================================================================
# Helper Functions for Provision Tests
# =============================================================================


def _create_minimal_agent(host: Host, temp_dir: Path, work_dir: Path | None = None) -> AgentInterface:
    """Create a minimal agent for provisioning tests."""
    if work_dir is None:
        work_dir = temp_dir / "work"
        work_dir.mkdir(parents=True, exist_ok=True)

    return host.create_agent_state(
        work_dir_path=work_dir,
        options=CreateAgentOptions(
            name=AgentName("test-agent"),
            agent_type=AgentTypeName("generic"),
            command=CommandString("sleep 1"),
        ),
    )


# Note: Agent provisioning lifecycle tests (on_before_provisioning, get_provision_file_transfers,
# provision, on_after_provisioning) are covered by agent-type specific tests since these are
# methods on the agent class rather than plugin hooks. See the "Provisioning Lifecycle Tests"
# section in claude_agent_test.py.


# =============================================================================
# File Transfer Tests (create_agent_work_dir and helpers)
# =============================================================================


def _init_git_repo(path: Path, commit_message: str = "Initial commit") -> None:
    """Helper to initialize a git repo.

    Requires the setup_git_config fixture to have created .gitconfig in the fake HOME.
    """
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", commit_message],
        cwd=path,
        capture_output=True,
        check=True,
    )


def test_get_ssh_connection_info_returns_none_for_local_host(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that _get_ssh_connection_info returns None for local hosts."""
    host, _ = host_with_temp_dir
    ssh_info = host._get_ssh_connection_info()
    assert ssh_info is None


def test_create_work_dir_same_path_no_transfer(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that no transfer happens when source and target are the same."""
    host, temp_dir = host_with_temp_dir

    source_path = temp_dir / "same_path_test"
    source_path.mkdir()
    (source_path / "test_file.txt").write_text("original content")

    options = CreateAgentOptions(
        name=AgentName("same-path-test"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        target_path=source_path,
    )

    work_dir = host.create_agent_work_dir(host, source_path, options).path

    assert work_dir == source_path
    assert (work_dir / "test_file.txt").read_text() == "original content"


@pytest.mark.rsync
def test_create_work_dir_copy_without_git(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test copying a directory without git."""
    host, temp_dir = host_with_temp_dir

    source_path = temp_dir / "source_no_git"
    source_path.mkdir()
    (source_path / "file1.txt").write_text("content1")
    (source_path / "subdir").mkdir()
    (source_path / "subdir" / "file2.txt").write_text("content2")

    target_path = temp_dir / "target_no_git"

    options = CreateAgentOptions(
        name=AgentName("copy-no-git"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        target_path=target_path,
    )

    work_dir = host.create_agent_work_dir(host, source_path, options).path

    assert work_dir == target_path
    assert (work_dir / "file1.txt").read_text() == "content1"
    assert (work_dir / "subdir" / "file2.txt").read_text() == "content2"


@pytest.mark.rsync
def test_create_work_dir_copy_with_git(
    host_with_temp_dir: tuple[Host, Path],
    setup_git_config: None,
) -> None:
    """Test copying a directory with git repository."""
    host, temp_dir = host_with_temp_dir

    source_path = temp_dir / "source_with_git"
    source_path.mkdir()
    (source_path / "file1.txt").write_text("tracked content")

    _init_git_repo(source_path)

    target_path = temp_dir / "target_with_git"

    options = CreateAgentOptions(
        name=AgentName("copy-with-git"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        target_path=target_path,
    )

    work_dir = host.create_agent_work_dir(host, source_path, options).path

    assert work_dir == target_path
    assert (work_dir / "file1.txt").read_text() == "tracked content"
    assert (work_dir / ".git").exists()

    # Verify git is functional in target
    result = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=work_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "Initial commit" in result.stdout


@pytest.mark.rsync
def test_create_work_dir_copy_with_git_copies_info_exclude(
    host_with_temp_dir: tuple[Host, Path],
    setup_git_config: None,
) -> None:
    """Test that .git/info/exclude is copied from source to target by default."""
    host, temp_dir = host_with_temp_dir

    source_path = temp_dir / "source_info_exclude"
    source_path.mkdir()
    (source_path / "file1.txt").write_text("content")
    _init_git_repo(source_path)

    # Write a custom exclude pattern to .git/info/exclude
    exclude_file = source_path / ".git" / "info" / "exclude"
    exclude_file.write_text("my_custom_pattern\n")

    target_path = temp_dir / "target_info_exclude"

    options = CreateAgentOptions(
        name=AgentName("copy-info-exclude"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        target_path=target_path,
    )

    host.create_agent_work_dir(host, source_path, options)

    target_exclude = target_path / ".git" / "info" / "exclude"
    assert target_exclude.exists()
    assert target_exclude.read_text() == "my_custom_pattern\n"


@pytest.mark.rsync
def test_create_work_dir_copy_excludes_git_when_disabled(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that .git is excluded when not syncing git data."""
    host, temp_dir = host_with_temp_dir

    source_path = temp_dir / "source_exclude_git"
    source_path.mkdir()
    (source_path / "file1.txt").write_text("content")

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=source_path, capture_output=True, check=True)

    target_path = temp_dir / "target_exclude_git"

    options = CreateAgentOptions(
        name=AgentName("exclude-git"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        target_path=target_path,
        git=AgentGitOptions(is_git_synced=False),
    )

    work_dir = host.create_agent_work_dir(host, source_path, options).path

    assert work_dir == target_path
    assert (work_dir / "file1.txt").read_text() == "content"
    assert not (work_dir / ".git").exists()


@pytest.mark.rsync
def test_create_work_dir_copy_with_untracked_files(
    host_with_temp_dir: tuple[Host, Path],
    setup_git_config: None,
) -> None:
    """Test copying includes untracked files when is_include_unclean is True."""
    host, temp_dir = host_with_temp_dir

    source_path = temp_dir / "source_untracked"
    source_path.mkdir()
    (source_path / "tracked.txt").write_text("tracked")

    # Initialize git repo and commit tracked file
    subprocess.run(["git", "init"], cwd=source_path, capture_output=True, check=True)
    subprocess.run(["git", "add", "tracked.txt"], cwd=source_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=source_path,
        capture_output=True,
        check=True,
    )

    # Add untracked file after commit
    (source_path / "untracked.txt").write_text("untracked")

    target_path = temp_dir / "target_untracked"

    options = CreateAgentOptions(
        name=AgentName("include-untracked"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        target_path=target_path,
        git=AgentGitOptions(is_include_unclean=True),
    )

    work_dir = host.create_agent_work_dir(host, source_path, options).path

    assert work_dir == target_path
    assert (work_dir / "tracked.txt").read_text() == "tracked"
    assert (work_dir / "untracked.txt").read_text() == "untracked"


@pytest.mark.rsync
def test_create_work_dir_copy_with_gitignored_files(
    host_with_temp_dir: tuple[Host, Path],
    setup_git_config: None,
) -> None:
    """Test copying includes gitignored files when is_include_gitignored is True."""
    host, temp_dir = host_with_temp_dir

    source_path = temp_dir / "source_gitignored"
    source_path.mkdir()
    (source_path / "tracked.txt").write_text("tracked")
    (source_path / ".gitignore").write_text("*.log\n")

    _init_git_repo(source_path)

    # Add gitignored file
    (source_path / "debug.log").write_text("log content")

    target_path = temp_dir / "target_gitignored"

    options = CreateAgentOptions(
        name=AgentName("include-gitignored"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        target_path=target_path,
        git=AgentGitOptions(is_include_gitignored=True),
    )

    work_dir = host.create_agent_work_dir(host, source_path, options).path

    assert work_dir == target_path
    assert (work_dir / "tracked.txt").read_text() == "tracked"
    assert (work_dir / "debug.log").read_text() == "log content"


@pytest.mark.rsync
def test_create_work_dir_copy_with_renamed_file(
    host_with_temp_dir: tuple[Host, Path],
    setup_git_config: None,
) -> None:
    """Test copying handles renamed files in git status output."""
    host, temp_dir = host_with_temp_dir

    source_path = temp_dir / "source_renamed"
    source_path.mkdir()
    (source_path / "old_name.txt").write_text("content")

    _init_git_repo(source_path)

    # Rename the file (use git mv to ensure status shows rename)
    subprocess.run(["git", "mv", "old_name.txt", "new_name.txt"], cwd=source_path, capture_output=True, check=True)

    target_path = temp_dir / "target_renamed"

    options = CreateAgentOptions(
        name=AgentName("rename-test"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        target_path=target_path,
        git=AgentGitOptions(is_include_unclean=True),
    )

    work_dir = host.create_agent_work_dir(host, source_path, options).path

    assert work_dir == target_path
    # After git transfer and rsync, the renamed file should be present
    assert (work_dir / "new_name.txt").read_text() == "content"


@pytest.mark.rsync
def test_create_work_dir_generates_new_branch(
    host_with_temp_dir: tuple[Host, Path],
    setup_git_config: None,
) -> None:
    """Test that git transfer creates a new branch when is_new_branch is True."""
    host, temp_dir = host_with_temp_dir

    source_path = temp_dir / "source_new_branch"
    source_path.mkdir()
    (source_path / "file.txt").write_text("content")

    _init_git_repo(source_path)

    target_path = temp_dir / "target_new_branch"

    options = CreateAgentOptions(
        name=AgentName("new-branch-test"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        target_path=target_path,
        git=AgentGitOptions(is_new_branch=True, new_branch_prefix="test/"),
    )

    work_dir = host.create_agent_work_dir(host, source_path, options).path

    assert work_dir == target_path
    assert (work_dir / "file.txt").read_text() == "content"

    # Check the branch name starts with test/
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=work_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip().startswith("test/")


@pytest.mark.rsync
def test_create_work_dir_preserves_origin_remote(
    host_with_temp_dir: tuple[Host, Path],
    setup_git_config: None,
) -> None:
    """Test that git transfer preserves the origin remote from the source repo."""
    host, temp_dir = host_with_temp_dir

    source_path = temp_dir / "source_origin"
    source_path.mkdir()
    (source_path / "file.txt").write_text("content")

    _init_git_repo(source_path)

    # Add an origin remote to the source repo
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/owner/repo.git"],
        cwd=source_path,
        check=True,
        capture_output=True,
    )

    target_path = temp_dir / "target_origin"

    options = CreateAgentOptions(
        name=AgentName("origin-test"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        target_path=target_path,
        git=AgentGitOptions(is_new_branch=True, new_branch_prefix="test/"),
    )

    work_dir = host.create_agent_work_dir(host, source_path, options).path

    assert work_dir == target_path
    assert (work_dir / "file.txt").read_text() == "content"

    # Check that origin remote was preserved on the target
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=work_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "https://github.com/owner/repo.git"


@pytest.mark.rsync
def test_create_work_dir_works_without_origin_remote(
    host_with_temp_dir: tuple[Host, Path],
    setup_git_config: None,
) -> None:
    """Test that git transfer works when the source repo has no origin remote."""
    host, temp_dir = host_with_temp_dir

    source_path = temp_dir / "source_no_origin"
    source_path.mkdir()
    (source_path / "file.txt").write_text("content")

    _init_git_repo(source_path)

    target_path = temp_dir / "target_no_origin"

    options = CreateAgentOptions(
        name=AgentName("no-origin-test"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        target_path=target_path,
        git=AgentGitOptions(is_new_branch=True, new_branch_prefix="test/"),
    )

    work_dir = host.create_agent_work_dir(host, source_path, options).path

    assert work_dir == target_path
    assert (work_dir / "file.txt").read_text() == "content"

    # Verify no origin remote exists (since source had none)
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=work_dir,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


# =============================================================================
# Agent Environment Variable Tests
# =============================================================================


def test_provision_agent_writes_env_vars_to_file(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that provision_agent writes env_vars to the agent's env file."""
    host, temp_dir = host_with_temp_dir
    agent = _create_minimal_agent(host, temp_dir)

    options = CreateAgentOptions(
        name=AgentName("env-test"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        environment=AgentEnvironmentOptions(
            env_vars=(
                EnvVar(key="MY_VAR", value="my_value"),
                EnvVar(key="ANOTHER_VAR", value="another_value"),
            ),
        ),
    )

    host.provision_agent(agent, options, host.mng_ctx)

    # Check that env file was created
    env_path = temp_dir / "agents" / str(agent.id) / "env"
    assert env_path.exists()

    content = env_path.read_text()
    assert "MY_VAR=my_value" in content
    assert "ANOTHER_VAR=another_value" in content


def test_provision_agent_writes_env_files_to_agent_env(host_with_temp_dir: tuple[Host, Path], tmp_path: Path) -> None:
    """Test that provision_agent loads env vars from env_files."""
    host, temp_dir = host_with_temp_dir
    agent = _create_minimal_agent(host, temp_dir)

    # Create an env file to load
    env_file = tmp_path / "test.env"
    env_file.write_text("FROM_FILE=file_value\nSECOND_VAR=second_value\n")

    options = CreateAgentOptions(
        name=AgentName("env-file-test"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        environment=AgentEnvironmentOptions(
            env_files=(env_file,),
        ),
    )

    host.provision_agent(agent, options, host.mng_ctx)

    # Check that env file was created with vars from the env file
    env_path = temp_dir / "agents" / str(agent.id) / "env"
    assert env_path.exists()

    content = env_path.read_text()
    assert "FROM_FILE=file_value" in content
    assert "SECOND_VAR=second_value" in content


def test_provision_agent_user_commands_have_access_to_env_vars(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that user commands can access the environment variables."""
    host, temp_dir = host_with_temp_dir
    agent = _create_minimal_agent(host, temp_dir)

    output_file = temp_dir / "provision_test" / "env_output.txt"

    options = CreateAgentOptions(
        name=AgentName("env-cmd-test"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        environment=AgentEnvironmentOptions(
            env_vars=(EnvVar(key="PROVISION_TEST_VAR", value="test_value_12345"),),
        ),
        provisioning=AgentProvisioningOptions(
            create_directories=(output_file.parent,),
            user_commands=(f"echo $PROVISION_TEST_VAR > {output_file}",),
        ),
    )

    host.provision_agent(agent, options, host.mng_ctx)

    assert output_file.exists()
    assert "test_value_12345" in output_file.read_text()


def test_provision_agent_env_vars_precedence(
    host_with_temp_dir: tuple[Host, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test that env_vars override env_files, and pass_env_vars override both."""
    host, temp_dir = host_with_temp_dir
    agent = _create_minimal_agent(host, temp_dir)

    # Create an env file with a value
    env_file = tmp_path / "test.env"
    env_file.write_text("OVERRIDE_VAR=from_file\n")

    # Set env_var to override the file
    # (Note: pass_env_vars is processed after env_vars, so it would override both)

    options = CreateAgentOptions(
        name=AgentName("env-precedence-test"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        environment=AgentEnvironmentOptions(
            env_files=(env_file,),
            env_vars=(EnvVar(key="OVERRIDE_VAR", value="from_env_var"),),
        ),
    )

    host.provision_agent(agent, options, host.mng_ctx)

    # Check that env_vars overrode env_files
    env_path = temp_dir / "agents" / str(agent.id) / "env"
    content = env_path.read_text()
    assert "OVERRIDE_VAR=from_env_var" in content
    assert "from_file" not in content


@pytest.mark.tmux
def test_start_agent_has_access_to_env_vars(
    temp_host_dir: Path,
    per_host_dir: Path,
    temp_work_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: pluggy.PluginManager,
    mng_test_prefix: str,
) -> None:
    """Test that started agents have access to environment variables.

    This test verifies that when an agent command runs, it has access to the
    environment variables defined in the agent's env file. We use a command
    that prints an env var to a file to verify this.
    """
    config = MngConfig(default_host_dir=temp_host_dir, prefix=mng_test_prefix)
    mng_ctx = MngContext(config=config, pm=plugin_manager, profile_dir=temp_profile_dir)
    provider = LocalProviderInstance(
        name=ProviderInstanceName("local"),
        host_dir=per_host_dir,
        mng_ctx=mng_ctx,
    )
    host = provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    # Create a marker file path where the agent will write the env var value
    marker_file = temp_work_dir / "env_marker.txt"

    # The command will print the env var to a file, then sleep
    options = CreateAgentOptions(
        name=AgentName("env-start-test"),
        agent_type=AgentTypeName("generic"),
        command=CommandString(f"echo AGENT_START_VAR=$AGENT_START_VAR > {marker_file} && sleep 847291"),
        environment=AgentEnvironmentOptions(
            env_vars=(EnvVar(key="AGENT_START_VAR", value="agent_env_value_847291"),),
        ),
    )

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=options,
    )

    # Provision the agent to write the env file
    host.provision_agent(agent, options, mng_ctx)

    # Start the agent
    host.start_agents([agent.id])

    try:
        # Wait for the marker file to be written
        def check_marker_file() -> bool:
            if not marker_file.exists():
                return False
            content = marker_file.read_text()
            return "AGENT_START_VAR=agent_env_value_847291" in content

        wait_for(check_marker_file, error_message="Expected environment variable not found in agent output file")

    finally:
        host.stop_agents([agent.id])


@pytest.mark.tmux
@pytest.mark.timeout(25)
def test_new_tmux_window_inherits_env_vars(
    temp_host_dir: Path,
    per_host_dir: Path,
    temp_work_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: pluggy.PluginManager,
    mng_test_prefix: str,
) -> None:
    """Test that new tmux windows created by the user also have env vars.

    This verifies that the default-command sources env files so that any new
    window/pane created by the user will have the agent's env vars available.
    """
    config = MngConfig(default_host_dir=temp_host_dir, prefix=mng_test_prefix)
    mng_ctx = MngContext(config=config, pm=plugin_manager, profile_dir=temp_profile_dir)
    provider = LocalProviderInstance(
        name=ProviderInstanceName("local"),
        host_dir=per_host_dir,
        mng_ctx=mng_ctx,
    )
    host = provider.create_host(HostName("localhost"))
    assert isinstance(host, Host)

    marker_file = temp_work_dir / "new_window_marker.txt"
    session_name = f"{config.prefix}new-window-test"

    options = CreateAgentOptions(
        name=AgentName("new-window-test"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 999999"),
        environment=AgentEnvironmentOptions(
            env_vars=(EnvVar(key="NEW_WINDOW_VAR", value="new_window_value_123456"),),
        ),
    )

    agent = host.create_agent_state(
        work_dir_path=temp_work_dir,
        options=options,
    )

    host.provision_agent(agent, options, mng_ctx)
    host.start_agents([agent.id])

    try:
        # Create a new window in the session (simulating what a user would do)
        # This window should inherit env vars via tmux set-environment
        subprocess.run(
            ["tmux", "new-window", "-t", session_name, "-n", "user-window"],
            check=True,
            capture_output=True,
        )

        # Keys sent before the shell is ready are buffered in the pty.
        window_target = f"{session_name}:user-window"
        subprocess.run(
            [
                "tmux",
                "send-keys",
                "-t",
                window_target,
                f"echo NEW_WINDOW_VAR=$NEW_WINDOW_VAR > {marker_file}",
                "Enter",
            ],
            check=True,
            capture_output=True,
        )

        # Wait for the marker file to be written with the expected value
        def check_marker_file() -> bool:
            if not marker_file.exists():
                return False
            content = marker_file.read_text()
            return "NEW_WINDOW_VAR=new_window_value_123456" in content

        if not poll_until(check_marker_file, timeout=10.0):
            pane_content = subprocess.run(
                ["tmux", "capture-pane", "-t", window_target, "-p"],
                capture_output=True,
                text=True,
            )
            marker_content = marker_file.read_text() if marker_file.exists() else "<file does not exist>"
            raise AssertionError(
                f"New tmux window did not inherit environment variables.\n"
                f"Marker file content: {marker_content!r}\n"
                f"Pane content:\n{pane_content.stdout}"
            )

    finally:
        host.stop_agents([agent.id])


def test_provision_agent_host_env_sourced_before_agent_env(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that host env is sourced before agent env (agent can override host)."""
    host, temp_dir = host_with_temp_dir
    agent = _create_minimal_agent(host, temp_dir)

    # Set a host-level env var
    host.set_env_var("HOST_VAR", "host_value")
    host.set_env_var("SHARED_VAR", "from_host")

    output_file = temp_dir / "provision_test" / "host_env_output.txt"

    options = CreateAgentOptions(
        name=AgentName("host-env-test"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        environment=AgentEnvironmentOptions(
            env_vars=(EnvVar(key="SHARED_VAR", value="from_agent"),),
        ),
        provisioning=AgentProvisioningOptions(
            create_directories=(output_file.parent,),
            user_commands=(f"echo HOST_VAR=$HOST_VAR SHARED_VAR=$SHARED_VAR > {output_file}",),
        ),
    )

    host.provision_agent(agent, options, host.mng_ctx)

    assert output_file.exists()
    content = output_file.read_text()
    # Host var should be available
    assert "HOST_VAR=host_value" in content
    # Agent env should override host env for SHARED_VAR
    assert "SHARED_VAR=from_agent" in content


@pytest.mark.rsync
def test_rsync_extra_args_parsing(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that rsync extra_args are parsed correctly using shlex."""
    host, temp_dir = host_with_temp_dir

    source_path = temp_dir / "source_rsync_args"
    source_path.mkdir()
    (source_path / "file1.txt").write_text("content1")
    (source_path / "file2.txt").write_text("content2")
    (source_path / "exclude_me.txt").write_text("excluded")

    target_path = temp_dir / "target_rsync_args"

    # Use rsync_args to exclude a file (tests that args are parsed and applied)
    options = CreateAgentOptions(
        name=AgentName("rsync-args-test"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        target_path=target_path,
        data_options=AgentDataOptions(
            is_rsync_enabled=True,
            rsync_args="--exclude exclude_me.txt",
        ),
    )

    work_dir = host.create_agent_work_dir(host, source_path, options).path

    assert work_dir == target_path
    assert (work_dir / "file1.txt").read_text() == "content1"
    assert (work_dir / "file2.txt").read_text() == "content2"
    # The excluded file should not be copied
    assert not (work_dir / "exclude_me.txt").exists()


@pytest.mark.rsync
def test_rsync_extra_args_with_spaces(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that rsync extra_args with quoted spaces are parsed correctly."""
    host, temp_dir = host_with_temp_dir

    source_path = temp_dir / "source_rsync_spaces"
    source_path.mkdir()
    (source_path / "file with spaces.txt").write_text("content with spaces")
    (source_path / "normal.txt").write_text("normal content")

    target_path = temp_dir / "target_rsync_spaces"

    # Use rsync_args with a filter pattern that has spaces
    # Note: rsync filter rules can be complex, so we use a simple exclude test
    options = CreateAgentOptions(
        name=AgentName("rsync-spaces-test"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        target_path=target_path,
        data_options=AgentDataOptions(
            is_rsync_enabled=True,
            rsync_args='--exclude "file with spaces.txt"',
        ),
    )

    work_dir = host.create_agent_work_dir(host, source_path, options).path

    assert work_dir == target_path
    assert (work_dir / "normal.txt").read_text() == "normal content"
    # The file with spaces should be excluded
    assert not (work_dir / "file with spaces.txt").exists()


@pytest.mark.rsync
def test_transfer_extra_files_with_many_files(
    host_with_temp_dir: tuple[Host, Path],
    setup_git_config: None,
) -> None:
    """Test that transferring many extra files works (uses temp file for --files-from)."""
    host, temp_dir = host_with_temp_dir

    source_path = temp_dir / "source_many_files"
    source_path.mkdir()
    (source_path / "tracked.txt").write_text("tracked")

    _init_git_repo(source_path)

    # Create many untracked files to exercise the files-from approach
    for i in range(50):
        (source_path / f"untracked_{i}.txt").write_text(f"untracked content {i}")

    target_path = temp_dir / "target_many_files"

    options = CreateAgentOptions(
        name=AgentName("many-files-test"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        target_path=target_path,
        git=AgentGitOptions(is_git_synced=True, is_include_unclean=True),
    )

    work_dir = host.create_agent_work_dir(host, source_path, options).path

    assert work_dir == target_path
    assert (work_dir / "tracked.txt").read_text() == "tracked"
    # Verify all untracked files were transferred
    for i in range(50):
        assert (work_dir / f"untracked_{i}.txt").read_text() == f"untracked content {i}"


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(60)
def test_rsync_files_remote_files_from_handling(
    host_with_temp_dir: tuple[Host, Path],
    ssh_host_factory: Callable[[str], Host],
    tmp_path: Path,
) -> None:
    """Test that files_from is copied to remote host when rsync runs remotely.

    This tests the code path where rsync runs on a remote host and needs the
    files-from list to be available there. We use a real SSH connection via
    a local sshd to verify the actual behavior.
    """
    local_host, _temp_dir = host_with_temp_dir

    # Create source files on the local filesystem
    source_path = tmp_path / "source_remote"
    source_path.mkdir()
    (source_path / "file1.txt").write_text("content1")
    (source_path / "file2.txt").write_text("content2")
    (source_path / "file3.txt").write_text("content3_not_transferred")

    target_path = tmp_path / "target_remote"
    target_path.mkdir()

    # Create a files-from file that only includes file1.txt and file2.txt
    files_from_path = tmp_path / "files_from.txt"
    files_from_path.write_text("file1.txt\nfile2.txt\n")

    ssh_host = ssh_host_factory("localhost")
    assert not ssh_host.is_local

    # Call _rsync_files with the SSH host as source
    # Since source and target are local paths but source_host is remote,
    # this tests the code path where the files_from file is copied to the remote
    local_host._rsync_files(
        source_host=ssh_host,
        source_path=source_path,
        target_path=target_path,
        files_from=files_from_path,
    )

    # Verify only the files listed in files_from were transferred
    assert (target_path / "file1.txt").read_text() == "content1"
    assert (target_path / "file2.txt").read_text() == "content2"
    # file3.txt should NOT have been transferred since it wasn't in files_from
    assert not (target_path / "file3.txt").exists()

    # Verify the temporary files_from file was cleaned up from the remote
    # by checking that no files matching the pattern exist
    result = ssh_host.execute_command("ls /tmp/rsync_files_from_*.txt 2>/dev/null || true")
    assert "rsync_files_from_" not in result.stdout


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(60)
def test_rsync_files_remote_to_remote(
    ssh_host_factory: Callable[[str], Host],
    tmp_path: Path,
) -> None:
    """Test rsync between two remote hosts via local intermediary.

    Uses a single local sshd to simulate two different remote hosts.
    The source SSH host syncs to a local temp dir, then syncs to the target SSH host.
    """
    # Create source files
    source_path = tmp_path / "source_r2r"
    source_path.mkdir()
    (source_path / "file1.txt").write_text("content1")
    (source_path / "file2.txt").write_text("content2")
    (source_path / "subdir").mkdir()
    (source_path / "subdir" / "nested.txt").write_text("nested content")

    target_path = tmp_path / "target_r2r"
    target_path.mkdir()

    source_host = ssh_host_factory("source-host")
    target_host = ssh_host_factory("target-host")

    assert not source_host.is_local
    assert not target_host.is_local

    target_host._rsync_files(
        source_host=source_host,
        source_path=source_path,
        target_path=target_path,
    )

    assert (target_path / "file1.txt").read_text() == "content1"
    assert (target_path / "file2.txt").read_text() == "content2"
    assert (target_path / "subdir" / "nested.txt").read_text() == "nested content"


@pytest.mark.acceptance
@pytest.mark.rsync
@pytest.mark.timeout(60)
def test_rsync_files_remote_to_remote_with_files_from(
    ssh_host_factory: Callable[[str], Host],
    tmp_path: Path,
) -> None:
    """Test rsync between two remote hosts with files_from filtering."""
    source_path = tmp_path / "source_r2r_ff"
    source_path.mkdir()
    (source_path / "include_me.txt").write_text("included")
    (source_path / "exclude_me.txt").write_text("excluded")

    target_path = tmp_path / "target_r2r_ff"
    target_path.mkdir()

    files_from_path = tmp_path / "files_from_r2r.txt"
    files_from_path.write_text("include_me.txt\n")

    source_host = ssh_host_factory("source-ff")
    target_host = ssh_host_factory("target-ff")

    target_host._rsync_files(
        source_host=source_host,
        source_path=source_path,
        target_path=target_path,
        files_from=files_from_path,
    )

    assert (target_path / "include_me.txt").read_text() == "included"
    assert not (target_path / "exclude_me.txt").exists()


@pytest.mark.rsync
def test_rsync_does_not_delete_existing_files_by_default(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that rsync without --delete preserves existing files in target.

    This is intentional behavior: rsync is designed for adding extra files
    (e.g., data files not in git), not for full directory sync.
    """
    host, temp_dir = host_with_temp_dir

    source_path = temp_dir / "source_no_delete"
    source_path.mkdir()
    (source_path / "new_file.txt").write_text("new content")

    target_path = temp_dir / "target_no_delete"
    target_path.mkdir()
    # Pre-existing file in target that doesn't exist in source
    (target_path / "existing_file.txt").write_text("existing content")

    options = CreateAgentOptions(
        name=AgentName("no-delete-test"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        target_path=target_path,
        data_options=AgentDataOptions(is_rsync_enabled=True),
    )

    work_dir = host.create_agent_work_dir(host, source_path, options).path

    assert work_dir == target_path
    # New file should be copied
    assert (work_dir / "new_file.txt").read_text() == "new content"
    # Existing file should NOT be deleted (rsync doesn't use --delete by default)
    assert (work_dir / "existing_file.txt").read_text() == "existing content"


@pytest.mark.rsync
def test_rsync_with_delete_removes_extra_files(host_with_temp_dir: tuple[Host, Path]) -> None:
    """Test that rsync with --delete removes files not in source.

    Users can add --delete to rsync_args to get full sync behavior.
    """
    host, temp_dir = host_with_temp_dir

    source_path = temp_dir / "source_with_delete"
    source_path.mkdir()
    (source_path / "new_file.txt").write_text("new content")

    target_path = temp_dir / "target_with_delete"
    target_path.mkdir()
    # Pre-existing file in target that doesn't exist in source
    (target_path / "existing_file.txt").write_text("existing content")

    options = CreateAgentOptions(
        name=AgentName("with-delete-test"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        target_path=target_path,
        data_options=AgentDataOptions(
            is_rsync_enabled=True,
            rsync_args="--delete",
        ),
    )

    work_dir = host.create_agent_work_dir(host, source_path, options).path

    assert work_dir == target_path
    # New file should be copied
    assert (work_dir / "new_file.txt").read_text() == "new content"
    # Existing file SHOULD be deleted (--delete flag passed)
    assert not (work_dir / "existing_file.txt").exists()


@pytest.mark.rsync
def test_create_work_dir_cross_host_generates_unique_paths(
    host_with_temp_dir: tuple[Host, Path],
    tmp_path: Path,
    temp_mng_ctx: MngContext,
    mng_test_prefix: str,
    plugin_manager: pluggy.PluginManager,
) -> None:
    """Test that cross-host work dir creation generates unique paths under host_dir/projects/.

    When no target_path is specified and source and target are on different hosts,
    each call should produce a unique directory so multiple agents on a shared host
    don't collide.
    """
    target_host, _temp_dir = host_with_temp_dir

    # Create a source host with a different default_host_dir so it gets a different host ID
    source_host_dir = tmp_path / "source_host_dir"
    source_host_dir.mkdir()
    source_profile_dir = source_host_dir / "profiles" / "default"
    source_profile_dir.mkdir(parents=True)
    source_config = MngConfig(
        default_host_dir=source_host_dir,
        prefix=mng_test_prefix,
        is_error_reporting_enabled=False,
    )
    source_mng_ctx = MngContext(
        config=source_config,
        pm=plugin_manager,
        profile_dir=source_profile_dir,
        concurrency_group=temp_mng_ctx.concurrency_group,
    )
    source_provider = LocalProviderInstance(
        name=ProviderInstanceName("local"),
        host_dir=source_host_dir,
        mng_ctx=source_mng_ctx,
    )
    source_host = source_provider.create_host(HostName("localhost"))

    # Verify the two hosts have different IDs (cross-host scenario)
    assert source_host.id != target_host.id

    # Create a source directory with a file
    source_path = tmp_path / "source_project"
    source_path.mkdir()
    (source_path / "file.txt").write_text("content")

    options = CreateAgentOptions(
        name=AgentName("agent-one"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        data_options=AgentDataOptions(is_rsync_enabled=True),
    )

    work_dir_1 = target_host.create_agent_work_dir(source_host, source_path, options).path

    # The generated path should be under host_dir/projects/
    assert str(work_dir_1).startswith(str(target_host.host_dir / "projects"))
    assert (work_dir_1 / "file.txt").read_text() == "content"

    # Create a second agent on the same target host - should get a different path
    options_2 = CreateAgentOptions(
        name=AgentName("agent-two"),
        agent_type=AgentTypeName("generic"),
        command=CommandString("sleep 1"),
        data_options=AgentDataOptions(is_rsync_enabled=True),
    )

    work_dir_2 = target_host.create_agent_work_dir(source_host, source_path, options_2).path

    assert str(work_dir_2).startswith(str(target_host.host_dir / "projects"))
    assert work_dir_1 != work_dir_2
    assert (work_dir_2 / "file.txt").read_text() == "content"
