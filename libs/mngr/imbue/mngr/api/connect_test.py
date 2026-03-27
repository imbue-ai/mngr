"""Unit tests for the connect API module."""

import os
import shlex
import subprocess
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pluggy
import pytest
from pyinfra.api import Host as PyinfraHost
from pyinfra.api import State as PyinfraState
from pyinfra.api.inventory import Inventory

from imbue.imbue_common.model_update import to_update
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.api.connect import SIGNAL_EXIT_CODE_DESTROY
from imbue.mngr.api.connect import SIGNAL_EXIT_CODE_STOP
from imbue.mngr.api.connect import _build_ssh_activity_wrapper_script
from imbue.mngr.api.connect import _build_ssh_args
from imbue.mngr.api.connect import _determine_post_disconnect_action
from imbue.mngr.api.connect import connect_to_agent
from imbue.mngr.api.connect import resolve_connect_command
from imbue.mngr.api.connect import run_connect_command
from imbue.mngr.api.data_types import ConnectionOptions
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import NestedTmuxError
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.data_types import PyinfraConnector
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostId
from imbue.mngr.providers.local.instance import LocalProviderInstance


def test_build_ssh_activity_wrapper_script_creates_activity_directory() -> None:
    """Test that the wrapper script creates the activity directory."""
    script = _build_ssh_activity_wrapper_script("mngr-test-session", Path("/home/user/.mngr"), "claude")

    assert "mkdir -p '/home/user/.mngr/activity'" in script


def test_build_ssh_activity_wrapper_script_writes_to_activity_file() -> None:
    """Test that the wrapper script writes to the activity/ssh file."""
    script = _build_ssh_activity_wrapper_script("mngr-test-session", Path("/home/user/.mngr"), "claude")

    assert "'/home/user/.mngr/activity/ssh'" in script


def test_build_ssh_activity_wrapper_script_attaches_to_tmux_session() -> None:
    """Test that the wrapper script attaches to the correct tmux session."""
    script = _build_ssh_activity_wrapper_script("mngr-my-agent", Path("/home/user/.mngr"), "claude")

    assert "tmux attach -t 'mngr-my-agent'" in script


def test_build_ssh_activity_wrapper_script_kills_activity_tracker_on_exit() -> None:
    """Test that the wrapper script kills the activity tracker when tmux exits."""
    script = _build_ssh_activity_wrapper_script("mngr-test", Path("/tmp/.mngr"), "claude")

    assert "kill $MNGR_ACTIVITY_PID" in script


def test_build_ssh_activity_wrapper_script_writes_json_with_time_and_pid() -> None:
    """Test that the activity file contains JSON with time and ssh_pid."""
    script = _build_ssh_activity_wrapper_script("mngr-test", Path("/tmp/.mngr"), "claude")

    # The script should write JSON with time and ssh_pid fields
    assert "time" in script
    assert "ssh_pid" in script
    assert "TIME_MS" in script


def test_build_ssh_activity_wrapper_script_handles_paths_with_spaces() -> None:
    """Test that the wrapper script handles paths with spaces correctly."""
    script = _build_ssh_activity_wrapper_script("mngr-test", Path("/home/user/my dir/.mngr"), "claude")

    # Paths should be quoted to handle spaces
    assert "'/home/user/my dir/.mngr/activity'" in script
    assert "'/home/user/my dir/.mngr/activity/ssh'" in script


def test_build_ssh_activity_wrapper_script_checks_for_signal_file() -> None:
    """Test that the wrapper script checks for the session-specific signal file."""
    script = _build_ssh_activity_wrapper_script("mngr-my-agent", Path("/home/user/.mngr"), "claude")

    assert "'/home/user/.mngr/signals/mngr-my-agent'" in script
    assert "SIGNAL_FILE=" in script


def test_build_ssh_activity_wrapper_script_exits_with_destroy_code_on_destroy_signal() -> None:
    """Test that the wrapper script exits with SIGNAL_EXIT_CODE_DESTROY when signal is 'destroy'."""
    script = _build_ssh_activity_wrapper_script("mngr-test", Path("/tmp/.mngr"), "claude")

    assert f"exit {SIGNAL_EXIT_CODE_DESTROY}" in script
    assert '"destroy"' in script


def test_build_ssh_activity_wrapper_script_exits_with_stop_code_on_stop_signal() -> None:
    """Test that the wrapper script exits with SIGNAL_EXIT_CODE_STOP when signal is 'stop'."""
    script = _build_ssh_activity_wrapper_script("mngr-test", Path("/tmp/.mngr"), "claude")

    assert f"exit {SIGNAL_EXIT_CODE_STOP}" in script
    assert '"stop"' in script


def test_build_ssh_activity_wrapper_script_removes_signal_file_after_reading() -> None:
    """Test that the wrapper script removes the signal file after reading it."""
    script = _build_ssh_activity_wrapper_script("mngr-test", Path("/tmp/.mngr"), "claude")

    assert 'rm -f "$SIGNAL_FILE"' in script


def test_build_ssh_activity_wrapper_script_signal_file_uses_session_name() -> None:
    """Test that the signal file path includes the session name for per-session signals."""
    script = _build_ssh_activity_wrapper_script("mngr-unique-session", Path("/data/.mngr"), "claude")

    assert "'/data/.mngr/signals/mngr-unique-session'" in script


# =========================================================================
# Tests for _build_ssh_args
# =========================================================================


def _create_pyinfra_ssh_host(
    hostname: str,
    data: dict[str, Any],
) -> PyinfraHost:
    """Create a real pyinfra Host with the given SSH connection data."""
    names_data = ([(hostname, data)], {})
    inventory = Inventory(names_data)
    state = PyinfraState(inventory=inventory)
    pyinfra_host = inventory.get_host(hostname)
    pyinfra_host.init(state)
    return pyinfra_host


def _make_ssh_host(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    hostname: str = "example.com",
    ssh_user: str | None = "ubuntu",
    ssh_port: int | None = 22,
    ssh_key: str | None = "/home/user/.ssh/id_rsa",
    ssh_known_hosts_file: str | None = None,
) -> Host:
    """Create a real Host with an SSH pyinfra connector for testing."""
    host_data: dict[str, Any] = {}
    if ssh_user is not None:
        host_data["ssh_user"] = ssh_user
    if ssh_port is not None:
        host_data["ssh_port"] = ssh_port
    if ssh_key is not None:
        host_data["ssh_key"] = ssh_key
    if ssh_known_hosts_file is not None:
        host_data["ssh_known_hosts_file"] = ssh_known_hosts_file

    pyinfra_host = _create_pyinfra_ssh_host(hostname, host_data)
    connector = PyinfraConnector(pyinfra_host)

    return Host(
        id=HostId(f"host-{uuid4().hex}"),
        connector=connector,
        provider_instance=local_provider,
        mngr_ctx=temp_mngr_ctx,
    )


class _TestAgent(BaseAgent):
    """Test agent that avoids SSH access for get_expected_process_name.

    BaseAgent.get_expected_process_name reads data.json via the host connector,
    which fails for SSH hosts in tests since no SSH server is running. This
    subclass returns a fixed process name to avoid that code path.
    """

    def get_expected_process_name(self) -> str:
        return "test-process"


def _make_remote_agent(
    host: Host,
    temp_mngr_ctx: MngrContext,
    agent_name: str = "test-agent",
) -> _TestAgent:
    """Create a test agent on a remote host for testing connect_to_agent."""
    return _TestAgent(
        id=AgentId(f"agent-{uuid4().hex}"),
        name=AgentName(agent_name),
        agent_type=AgentTypeName("test"),
        work_dir=Path("/tmp/work"),
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=temp_mngr_ctx,
        agent_config=AgentTypeConfig(),
        host=host,
    )


def test_build_ssh_args_with_known_hosts_file(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that _build_ssh_args uses StrictHostKeyChecking=yes with a known_hosts file."""
    host = _make_ssh_host(local_provider, temp_mngr_ctx, ssh_known_hosts_file="/tmp/known_hosts")
    opts = ConnectionOptions(is_unknown_host_allowed=False)

    args = _build_ssh_args(host, opts)

    assert "-i" in args
    assert "/home/user/.ssh/id_rsa" in args
    assert "-p" in args
    assert "22" in args
    assert "UserKnownHostsFile=/tmp/known_hosts" in " ".join(args)
    assert "StrictHostKeyChecking=yes" in " ".join(args)
    assert "ubuntu@example.com" in args


def test_build_ssh_args_with_allow_unknown_host(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that _build_ssh_args disables host key checking when allowed."""
    host = _make_ssh_host(local_provider, temp_mngr_ctx, ssh_known_hosts_file=None)
    opts = ConnectionOptions(is_unknown_host_allowed=True)

    args = _build_ssh_args(host, opts)

    assert "StrictHostKeyChecking=no" in " ".join(args)
    assert "UserKnownHostsFile=/dev/null" in " ".join(args)


def test_build_ssh_args_raises_without_known_hosts_or_allow_unknown(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that _build_ssh_args raises MngrError when no known_hosts and not allowing unknown."""
    host = _make_ssh_host(local_provider, temp_mngr_ctx, ssh_known_hosts_file=None)
    opts = ConnectionOptions(is_unknown_host_allowed=False)

    with pytest.raises(MngrError, match="known_hosts"):
        _build_ssh_args(host, opts)


def test_build_ssh_args_without_user(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that _build_ssh_args omits user@ when ssh_user is None."""
    host = _make_ssh_host(local_provider, temp_mngr_ctx, ssh_user=None, ssh_known_hosts_file="/tmp/known_hosts")
    opts = ConnectionOptions(is_unknown_host_allowed=False)

    args = _build_ssh_args(host, opts)

    # Should have bare hostname, not user@hostname
    assert "example.com" in args
    assert not any("@" in arg for arg in args)


def test_build_ssh_args_without_port(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that _build_ssh_args omits -p when ssh_port is None."""
    host = _make_ssh_host(local_provider, temp_mngr_ctx, ssh_port=None, ssh_known_hosts_file="/tmp/known_hosts")
    opts = ConnectionOptions(is_unknown_host_allowed=False)

    args = _build_ssh_args(host, opts)

    assert "-p" not in args


def test_build_ssh_args_without_key(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that _build_ssh_args omits -i when ssh_key is None."""
    host = _make_ssh_host(local_provider, temp_mngr_ctx, ssh_key=None, ssh_known_hosts_file="/tmp/known_hosts")
    opts = ConnectionOptions(is_unknown_host_allowed=False)

    args = _build_ssh_args(host, opts)

    assert "-i" not in args


def test_build_ssh_args_known_hosts_dev_null_treated_as_missing(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
) -> None:
    """Test that /dev/null known_hosts is treated as no known_hosts file."""
    host = _make_ssh_host(local_provider, temp_mngr_ctx, ssh_known_hosts_file="/dev/null")
    opts = ConnectionOptions(is_unknown_host_allowed=True)

    args = _build_ssh_args(host, opts)

    # Should fall through to the allow_unknown_host branch
    assert "StrictHostKeyChecking=no" in " ".join(args)


# =========================================================================
# Tests for _determine_post_disconnect_action
# =========================================================================


def test_determine_post_disconnect_action_destroy_signal() -> None:
    """Test that SIGNAL_EXIT_CODE_DESTROY maps to a mngr destroy action."""
    action = _determine_post_disconnect_action(SIGNAL_EXIT_CODE_DESTROY, "mngr-test-agent")

    assert action is not None
    executable, argv = action
    assert executable == "mngr"
    assert argv == ["mngr", "destroy", "--session", "mngr-test-agent", "-f"]


def test_determine_post_disconnect_action_stop_signal() -> None:
    """Test that SIGNAL_EXIT_CODE_STOP maps to a mngr stop action."""
    action = _determine_post_disconnect_action(SIGNAL_EXIT_CODE_STOP, "mngr-test-agent")

    assert action is not None
    executable, argv = action
    assert executable == "mngr"
    assert argv == ["mngr", "stop", "--session", "mngr-test-agent"]


def test_determine_post_disconnect_action_normal_exit_returns_none() -> None:
    """Test that a normal exit (code 0) returns no action."""
    action = _determine_post_disconnect_action(0, "mngr-test-agent")

    assert action is None


def test_determine_post_disconnect_action_unknown_exit_code_returns_none() -> None:
    """Test that an unexpected exit code returns no action."""
    action = _determine_post_disconnect_action(255, "mngr-test-agent")

    assert action is None


def test_determine_post_disconnect_action_uses_session_name_in_args() -> None:
    """Test that the session name is correctly embedded in the action args."""
    action = _determine_post_disconnect_action(SIGNAL_EXIT_CODE_DESTROY, "custom-my-agent")

    assert action is not None
    _, argv = action
    assert argv == ["mngr", "destroy", "--session", "custom-my-agent", "-f"]


# =========================================================================
# Tests for connect_to_agent remote exit code handling
# =========================================================================


class _ConnectTestResult:
    """Captures the results of a connect_to_agent call with intercepted system calls."""

    def __init__(self) -> None:
        self.execvp_calls: list[tuple[str, list[str]]] = []
        self.subprocess_call_args: list[list[str]] = []


def _run_connect_to_agent(
    local_provider: LocalProviderInstance,
    mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
    ssh_exit_code: int,
    agent_name: str = "test-agent",
) -> _ConnectTestResult:
    """Set up and run connect_to_agent with intercepted system calls."""
    host = _make_ssh_host(local_provider, mngr_ctx, ssh_known_hosts_file="/tmp/known_hosts")
    agent = _make_remote_agent(host, mngr_ctx, agent_name=agent_name)
    opts = ConnectionOptions(is_unknown_host_allowed=False)

    result = _ConnectTestResult()

    def fake_run_interactive(args, **kwargs):
        result.subprocess_call_args.append(list(args))
        return subprocess.CompletedProcess(args=args, returncode=ssh_exit_code)

    monkeypatch.setattr(
        "imbue.mngr.api.connect.run_interactive_subprocess",
        fake_run_interactive,
    )
    monkeypatch.setattr(
        "imbue.mngr.api.connect.os.execvp",
        lambda cmd, args: result.execvp_calls.append((cmd, list(args))),
    )

    connect_to_agent(agent, host, mngr_ctx, opts)

    return result


def test_connect_to_agent_remote_destroy_signal(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that connect_to_agent exec's into mngr destroy when SSH exits with SIGNAL_EXIT_CODE_DESTROY."""
    result = _run_connect_to_agent(local_provider, temp_mngr_ctx, monkeypatch, SIGNAL_EXIT_CODE_DESTROY)

    expected_session = f"{temp_mngr_ctx.config.prefix}test-agent"
    assert len(result.execvp_calls) == 1
    assert result.execvp_calls[0] == ("mngr", ["mngr", "destroy", "--session", expected_session, "-f"])


def test_connect_to_agent_remote_stop_signal(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that connect_to_agent exec's into mngr stop when SSH exits with SIGNAL_EXIT_CODE_STOP."""
    result = _run_connect_to_agent(local_provider, temp_mngr_ctx, monkeypatch, SIGNAL_EXIT_CODE_STOP)

    expected_session = f"{temp_mngr_ctx.config.prefix}test-agent"
    assert len(result.execvp_calls) == 1
    assert result.execvp_calls[0] == ("mngr", ["mngr", "stop", "--session", expected_session])


def test_connect_to_agent_remote_normal_exit_no_action(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that connect_to_agent does not exec into anything on normal SSH exit (code 0)."""
    result = _run_connect_to_agent(local_provider, temp_mngr_ctx, monkeypatch, ssh_exit_code=0)

    assert len(result.execvp_calls) == 0


def test_connect_to_agent_remote_unknown_exit_code_no_action(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that connect_to_agent does not exec into anything on unexpected SSH exit codes."""
    result = _run_connect_to_agent(local_provider, temp_mngr_ctx, monkeypatch, ssh_exit_code=255)

    assert len(result.execvp_calls) == 0


def test_connect_to_agent_remote_uses_correct_session_name(
    local_provider: LocalProviderInstance,
    temp_host_dir: Path,
    temp_profile_dir: Path,
    plugin_manager: pluggy.PluginManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that connect_to_agent constructs the session name from prefix + agent name."""
    # Use a custom prefix (different from the default fixture prefix) to verify the code
    # reads the prefix from the context rather than using a hardcoded value
    custom_config = MngrConfig(default_host_dir=temp_host_dir, prefix="custom-")
    custom_ctx = MngrContext(config=custom_config, pm=plugin_manager, profile_dir=temp_profile_dir)

    result = _run_connect_to_agent(
        local_provider, custom_ctx, monkeypatch, SIGNAL_EXIT_CODE_DESTROY, agent_name="my-agent"
    )

    assert len(result.execvp_calls) == 1
    assert result.execvp_calls[0] == ("mngr", ["mngr", "destroy", "--session", "custom-my-agent", "-f"])


def test_ssh_wrapper_script_is_correctly_quoted_for_bash_c() -> None:
    """Verify the wrapper script survives shell parsing as a single bash -c argument.

    SSH concatenates remote command arguments with spaces, so the wrapper must
    be shell-quoted into a single 'bash -c <quoted_script>' string. Otherwise
    bash -c only receives the first word (e.g. 'mkdir'), causing errors like
    'mkdir: missing operand'.
    """
    wrapper_script = _build_ssh_activity_wrapper_script("mngr-test", Path("/mngr"), "claude")
    remote_command = "bash -c " + shlex.quote(wrapper_script)

    # When the remote shell parses this command, bash should receive
    # the full wrapper script as a single -c argument
    parsed = shlex.split(remote_command)
    assert parsed == ["bash", "-c", wrapper_script]


def test_build_ssh_activity_wrapper_script_quotes_agent_command_with_metacharacters() -> None:
    """Test that agent_command is shell-quoted to prevent syntax errors.

    When agent_command contains shell metacharacters (e.g. '(' from a command
    like '( script.sh ... ) &'), it must be quoted so that pkill -f receives
    it as a literal pattern rather than as shell syntax.
    """
    script = _build_ssh_activity_wrapper_script("mngr-test", Path("/mngr"), "(")

    # The '(' should be quoted (e.g. as '(') so bash doesn't interpret it as subshell syntax
    assert "pkill -SIGWINCH -f '('" in script


def test_build_ssh_activity_wrapper_script_quotes_normal_agent_command() -> None:
    """Test that even normal agent_command values are properly quoted."""
    script = _build_ssh_activity_wrapper_script("mngr-test", Path("/mngr"), "claude")

    assert "pkill -SIGWINCH -f claude" in script


# =========================================================================
# Tests for nested tmux detection in connect_to_agent (local host)
# =========================================================================


def _make_local_host_and_agent(
    local_provider: LocalProviderInstance,
    mngr_ctx: MngrContext,
    agent_name: str = "test-agent",
) -> tuple[Host, _TestAgent]:
    """Create a local host and agent for testing connect_to_agent."""
    host = Host(
        id=HostId(f"host-{uuid4().hex}"),
        connector=PyinfraConnector(local_provider._create_local_pyinfra_host()),
        provider_instance=local_provider,
        mngr_ctx=mngr_ctx,
    )
    agent = _TestAgent(
        id=AgentId(f"agent-{uuid4().hex}"),
        name=AgentName(agent_name),
        agent_type=AgentTypeName("test"),
        work_dir=Path("/tmp/work"),
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=mngr_ctx,
        agent_config=AgentTypeConfig(),
        host=host,
    )
    return host, agent


def test_connect_to_agent_local_raises_nested_tmux_error_when_tmux_is_set(
    local_provider: LocalProviderInstance,
    temp_mngr_ctx: MngrContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test that connect_to_agent raises NestedTmuxError when $TMUX is set and is_nested_tmux_allowed is False."""
    host, agent = _make_local_host_and_agent(local_provider, temp_mngr_ctx)
    opts = ConnectionOptions(is_unknown_host_allowed=False)

    # Simulate being inside a tmux session
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,12345,0")

    with pytest.raises(NestedTmuxError) as exc_info:
        connect_to_agent(agent, host, temp_mngr_ctx, opts)

    expected_session = f"{temp_mngr_ctx.config.prefix}test-agent"
    assert exc_info.value.session_name == expected_session
    assert expected_session in str(exc_info.value)
    assert exc_info.value.user_help_text is not None
    assert "is_nested_tmux_allowed" in exc_info.value.user_help_text


# =========================================================================
# Tests for run_connect_command
# =========================================================================


def test_run_connect_command_sets_env_vars_and_execs(tmp_path: Path) -> None:
    """Test that run_connect_command sets env vars and execs via sh.

    Since run_connect_command replaces the process via os.execvpe, we fork and run
    it in the child. The command writes env vars to a temp file so the parent can verify.
    """
    output_file = tmp_path / "connect_env_output.txt"
    command = f'echo "$MNGR_AGENT_NAME $MNGR_SESSION_NAME $MNGR_HOST_IS_LOCAL" > {output_file}'

    pid = os.fork()
    if pid == 0:
        run_connect_command(command, "my-agent", "mngr-my-agent", is_local=True)
        os._exit(1)
    else:
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 0

        content = output_file.read_text().strip()
        assert content == "my-agent mngr-my-agent true"


def test_run_connect_command_sets_host_is_local_false_for_remote(tmp_path: Path) -> None:
    """Test that MNGR_HOST_IS_LOCAL is 'false' when is_local=False."""
    output_file = tmp_path / "connect_env_output_remote.txt"
    command = f'echo "$MNGR_HOST_IS_LOCAL" > {output_file}'

    pid = os.fork()
    if pid == 0:
        run_connect_command(command, "remote-agent", "mngr-remote-agent", is_local=False)
        os._exit(1)
    else:
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status)
        assert os.WEXITSTATUS(status) == 0

        content = output_file.read_text().strip()
        assert content == "false"


# =============================================================================
# resolve_connect_command tests
# =============================================================================


def test_resolve_connect_command_prefers_cli_option(temp_mngr_ctx: MngrContext) -> None:
    """resolve_connect_command should prefer the CLI option over config."""
    result = resolve_connect_command("cli-command", temp_mngr_ctx)
    assert result == "cli-command"


def test_resolve_connect_command_falls_back_to_config(temp_mngr_ctx: MngrContext) -> None:
    """resolve_connect_command should fall back to config.connect_command when CLI is None."""
    config_with_cmd = temp_mngr_ctx.config.model_copy_update(
        to_update(temp_mngr_ctx.config.field_ref().connect_command, "config-command"),
    )
    ctx = temp_mngr_ctx.model_copy_update(
        to_update(temp_mngr_ctx.field_ref().config, config_with_cmd),
    )
    result = resolve_connect_command(None, ctx)
    assert result == "config-command"


def test_resolve_connect_command_returns_none_when_neither_set(temp_mngr_ctx: MngrContext) -> None:
    """resolve_connect_command should return None when neither CLI nor config is set."""
    result = resolve_connect_command(None, temp_mngr_ctx)
    assert result is None
