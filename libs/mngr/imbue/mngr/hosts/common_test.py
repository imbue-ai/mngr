import os
import shlex
import subprocess
import types
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import ClassVar
from typing import cast

import pytest

from imbue.mngr.api.testing import FakeHost
from imbue.mngr.config.agent_class_registry import register_agent_class
from imbue.mngr.config.agent_class_registry import reset_agent_class_registry
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrConfig
from imbue.mngr.hosts.common import PROCESS_TABLE_SNAPSHOT_COMMAND
from imbue.mngr.hosts.common import TMUX_PANES_SNAPSHOT_COMMAND
from imbue.mngr.hosts.common import add_safe_directory_on_remote
from imbue.mngr.hosts.common import build_ssh_transport_command
from imbue.mngr.hosts.common import check_agent_type_known
from imbue.mngr.hosts.common import classify_waiting_reason
from imbue.mngr.hosts.common import compute_idle_seconds
from imbue.mngr.hosts.common import copy_on_host
from imbue.mngr.hosts.common import determine_lifecycle_probe_result
from imbue.mngr.hosts.common import find_agent_session_name
from imbue.mngr.hosts.common import get_descendant_pids
from imbue.mngr.hosts.common import get_descendant_process_names
from imbue.mngr.hosts.common import get_ssh_known_hosts_file
from imbue.mngr.hosts.common import parse_tmux_pane_listings
from imbue.mngr.hosts.common import resolve_expected_process_name
from imbue.mngr.hosts.common import symlink_on_host
from imbue.mngr.hosts.common import timestamp_to_datetime
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import CommandString
from imbue.mngr.primitives import LifecycleProbeResult
from imbue.mngr.primitives import WaitingReason
from imbue.mngr.utils.testing import write_executable_script


def test_timestamp_to_datetime_returns_none_for_none() -> None:
    assert timestamp_to_datetime(None) is None


def test_timestamp_to_datetime_converts_valid_timestamp() -> None:
    result = timestamp_to_datetime(1700000000)
    assert result is not None
    assert result.tzinfo == timezone.utc
    assert result.year == 2023


def test_timestamp_to_datetime_returns_none_for_invalid() -> None:
    result = timestamp_to_datetime(-99999999999999)
    assert result is None


def test_compute_idle_seconds_returns_none_when_all_none() -> None:
    assert compute_idle_seconds(None, None, None) is None


def test_compute_idle_seconds_uses_most_recent() -> None:
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=1)
    recent = now - timedelta(seconds=10)
    result = compute_idle_seconds(old, recent, None)
    assert result is not None
    assert 9 < result < 15


def test_compute_idle_seconds_with_single_activity() -> None:
    recent = datetime.now(timezone.utc) - timedelta(seconds=5)
    result = compute_idle_seconds(None, recent, None)
    assert result is not None
    assert 4 < result < 10


def test_lifecycle_stopped_when_no_tmux_info() -> None:
    assert (
        determine_lifecycle_probe_result(
            None,
            False,
            is_blocked_on_dialog=False,
            expected_process_name="claude",
            ps_output="",
        ).state
        == AgentLifecycleState.STOPPED
    )


def test_lifecycle_stopped_when_malformed_tmux_info() -> None:
    assert (
        determine_lifecycle_probe_result(
            "bad",
            False,
            is_blocked_on_dialog=False,
            expected_process_name="claude",
            ps_output="",
        ).state
        == AgentLifecycleState.STOPPED
    )


def test_lifecycle_done_when_pane_dead() -> None:
    assert (
        determine_lifecycle_probe_result(
            "1|bash|123",
            False,
            is_blocked_on_dialog=False,
            expected_process_name="claude",
            ps_output="",
        ).state
        == AgentLifecycleState.DONE
    )


def test_lifecycle_running_when_command_matches_and_active() -> None:
    assert (
        determine_lifecycle_probe_result(
            "0|claude|123",
            True,
            is_blocked_on_dialog=False,
            expected_process_name="claude",
            ps_output="",
        ).state
        == AgentLifecycleState.RUNNING
    )


def test_lifecycle_waiting_when_command_matches_and_not_active() -> None:
    assert (
        determine_lifecycle_probe_result(
            "0|claude|123",
            False,
            is_blocked_on_dialog=False,
            expected_process_name="claude",
            ps_output="",
        ).state
        == AgentLifecycleState.WAITING
    )


def test_lifecycle_running_when_descendant_matches() -> None:
    ps_output = "100 1 init\n200 123 bash\n300 200 claude\n"
    assert (
        determine_lifecycle_probe_result(
            "0|bash|123",
            True,
            is_blocked_on_dialog=False,
            expected_process_name="claude",
            ps_output=ps_output,
        ).state
        == AgentLifecycleState.RUNNING
    )


def test_lifecycle_done_when_non_shell_descendant_but_foreground_is_shell_known_type() -> None:
    """A known-type agent whose process is gone but whose pane has dropped to a shell
    prompt is DONE, even with a non-shell descendant still running.

    Every real claude agent keeps mngr's own in-pane helpers (transcript streamers,
    background-task script -- each a `sleep` loop) running as descendants. After the
    agent process is killed (ctrl-c / crash / OOM shed) those linger, so a non-shell
    descendant must NOT be read as "replaced by another program" when the foreground
    is a shell. Only a non-shell *foreground* means a real replacement.
    """
    ps_output = "200 123 python3\n"
    assert (
        determine_lifecycle_probe_result(
            "0|bash|123",
            True,
            is_blocked_on_dialog=False,
            expected_process_name="claude",
            ps_output=ps_output,
        ).state
        == AgentLifecycleState.DONE
    )


def test_lifecycle_done_when_shell_only() -> None:
    assert (
        determine_lifecycle_probe_result(
            "0|bash|123",
            True,
            is_blocked_on_dialog=False,
            expected_process_name="claude",
            ps_output="",
        ).state
        == AgentLifecycleState.DONE
    )


def test_lifecycle_replaced_when_unknown_command_and_pane_not_shell() -> None:
    """REPLACED when pane_current_command is unknown and pane PID's own process is not a shell."""
    ps_output = "123 1 python3\n"
    assert (
        determine_lifecycle_probe_result(
            "0|python3|123",
            True,
            is_blocked_on_dialog=False,
            expected_process_name="claude",
            ps_output=ps_output,
        ).state
        == AgentLifecycleState.REPLACED
    )


def test_lifecycle_replaced_when_unknown_command_and_pane_pid_not_in_ps() -> None:
    """REPLACED when pane_current_command is unknown and pane PID is not found in ps."""
    assert (
        determine_lifecycle_probe_result(
            "0|python3|123",
            True,
            is_blocked_on_dialog=False,
            expected_process_name="claude",
            ps_output="",
        ).state
        == AgentLifecycleState.REPLACED
    )


def test_lifecycle_done_when_modified_process_title_and_pane_is_shell() -> None:
    """DONE when tmux reports a modified title (e.g. version string) but pane PID is a shell.

    Claude Code sets its process title to its version (e.g. "2.1.73"), which tmux
    reports as pane_current_command. After claude exits, the shell prompt returns
    but tmux may briefly still report the stale title. The pane PID's own comm
    from ps ("bash") is the authoritative source.
    """
    ps_output = "123 1 bash\n"
    assert (
        determine_lifecycle_probe_result(
            "0|2.1.73|123",
            False,
            is_blocked_on_dialog=False,
            expected_process_name="claude",
            ps_output=ps_output,
        ).state
        == AgentLifecycleState.DONE
    )


def test_lifecycle_running_unknown_when_non_shell_descendant_and_unknown_type() -> None:
    ps_output = "200 123 python3\n"
    assert (
        determine_lifecycle_probe_result(
            "0|bash|123",
            True,
            is_blocked_on_dialog=False,
            expected_process_name="claude",
            ps_output=ps_output,
            is_agent_type_known=False,
        ).state
        == AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE
    )


def test_lifecycle_running_unknown_when_pane_not_shell_and_unknown_type() -> None:
    ps_output = "123 1 python3\n"
    assert (
        determine_lifecycle_probe_result(
            "0|python3|123",
            True,
            is_blocked_on_dialog=False,
            expected_process_name="claude",
            ps_output=ps_output,
            is_agent_type_known=False,
        ).state
        == AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE
    )


def test_lifecycle_running_unknown_when_pane_pid_not_in_ps_and_unknown_type() -> None:
    assert (
        determine_lifecycle_probe_result(
            "0|python3|123",
            True,
            is_blocked_on_dialog=False,
            expected_process_name="claude",
            ps_output="",
            is_agent_type_known=False,
        ).state
        == AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE
    )


def test_lifecycle_replaced_when_non_shell_foreground_and_known_type() -> None:
    """A known-type agent with a non-shell process in the *foreground* (pane comm is
    not a shell) is genuinely REPLACED -- distinct from a non-shell *descendant* under
    a shell prompt, which is DONE (see the test above)."""
    ps_output = "123 1 python3\n"
    assert (
        determine_lifecycle_probe_result(
            "0|python3|123",
            True,
            is_blocked_on_dialog=False,
            expected_process_name="claude",
            ps_output=ps_output,
            is_agent_type_known=True,
        ).state
        == AgentLifecycleState.REPLACED
    )


def test_lifecycle_replaced_when_non_shell_foreground_child_and_known_type() -> None:
    """A known-type agent whose pane shell has a non-shell *foreground* child is
    REPLACED, not DONE.

    This is the common takeover shape: the pane's root process is still the shell
    (so its ps comm is a shell), but another program runs in the foreground -- e.g.
    a window deliberately held by `sleep infinity && claude`, or a user who ctrl-c'd
    the agent and launched something else. tmux reports the foreground child as
    pane_current_command, which is what distinguishes this from a pane sitting at a
    bare prompt with only *background* helpers left (DONE, see above). Messaging
    treats DONE as revivable and tears the session down, so misreading this as DONE
    would destroy the occupying program.
    """
    ps_output = "123 1 bash\n200 123 sleep\n"
    assert (
        determine_lifecycle_probe_result(
            "0|sleep|123",
            True,
            is_blocked_on_dialog=False,
            expected_process_name="claude",
            ps_output=ps_output,
            is_agent_type_known=True,
        ).state
        == AgentLifecycleState.REPLACED
    )


def test_lifecycle_done_when_shell_and_unknown_type() -> None:
    """Unknown type does not affect DONE state (shell in pane means agent exited)."""
    assert (
        determine_lifecycle_probe_result(
            "0|bash|123",
            True,
            is_blocked_on_dialog=False,
            expected_process_name="claude",
            ps_output="",
            is_agent_type_known=False,
        ).state
        == AgentLifecycleState.DONE
    )


def test_lifecycle_waiting_when_modified_title_and_expected_in_descendants() -> None:
    """WAITING when tmux reports version string but claude is running as descendant."""
    ps_output = "123 1 bash\n456 123 claude\n"
    assert (
        determine_lifecycle_probe_result(
            "0|2.1.73|123",
            False,
            is_blocked_on_dialog=False,
            expected_process_name="claude",
            ps_output=ps_output,
        ).state
        == AgentLifecycleState.WAITING
    )


def test_pid_is_claude_descendant_when_running() -> None:
    """The returned PID is the descendant whose comm matches the expected process."""
    ps_output = "100 1 init\n200 123 bash\n300 200 claude\n"
    probe = determine_lifecycle_probe_result(
        "0|bash|123",
        True,
        is_blocked_on_dialog=False,
        expected_process_name="claude",
        ps_output=ps_output,
    )
    assert probe.state == AgentLifecycleState.RUNNING
    assert probe.pid == 300


def test_pid_is_pane_pid_when_process_runs_directly_in_pane() -> None:
    """When claude is the pane's own top process (not under a shell), its PID is the pane PID."""
    ps_output = "123 1 claude\n"
    probe = determine_lifecycle_probe_result(
        "0|claude|123",
        True,
        is_blocked_on_dialog=False,
        expected_process_name="claude",
        ps_output=ps_output,
    )
    assert probe.state == AgentLifecycleState.RUNNING
    assert probe.pid == 123


def test_pid_found_when_title_modified_and_claude_is_descendant() -> None:
    """A modified tmux title still yields the claude PID from the ps tree (WAITING here)."""
    ps_output = "123 1 bash\n456 123 claude\n"
    probe = determine_lifecycle_probe_result(
        "0|2.1.73|123",
        False,
        is_blocked_on_dialog=False,
        expected_process_name="claude",
        ps_output=ps_output,
    )
    assert probe.state == AgentLifecycleState.WAITING
    assert probe.pid == 456


# Under gVisor, ps lists a process that is exiting mid-read with pid 0. Here that is the
# agent's short-lived `wc`: taken as a real pid it makes pid 1 -- an ancestor of the
# agent's own pane -- look like a descendant of the pane.
_PS_OUTPUT_WITH_EXITING_PROCESS_AT_PID_ZERO = (
    "1 0 sh\n69 1 tmux:\n123 69 bash\n456 123 claude\n789 456 bash\n0 789 wc\n900 0 python3\n"
)


def test_lifecycle_probe_ignores_an_exiting_process_listed_with_pid_zero() -> None:
    """A process reachable from the pane only through a pid-0 line is not the agent's: the probe is DONE."""
    probe = determine_lifecycle_probe_result(
        "0|bash|123",
        False,
        is_blocked_on_dialog=False,
        expected_process_name="python3",
        ps_output=_PS_OUTPUT_WITH_EXITING_PROCESS_AT_PID_ZERO,
    )
    assert probe.state == AgentLifecycleState.DONE
    assert probe.pid is None


def test_lifecycle_probe_terminates_when_the_parent_links_form_a_cycle_through_the_pane() -> None:
    probe = determine_lifecycle_probe_result(
        "0|bash|123",
        False,
        is_blocked_on_dialog=False,
        expected_process_name="claude",
        ps_output="123 300 bash\n200 123 sleep\n300 200 sleep\n",
    )
    assert probe.state == AgentLifecycleState.DONE
    assert probe.pid is None


def test_pid_none_when_stopped() -> None:
    probe = determine_lifecycle_probe_result(
        None,
        False,
        is_blocked_on_dialog=False,
        expected_process_name="claude",
        ps_output="",
    )
    assert probe.state == AgentLifecycleState.STOPPED
    assert probe.pid is None


def test_pid_none_when_done() -> None:
    probe = determine_lifecycle_probe_result(
        "1|bash|123",
        False,
        is_blocked_on_dialog=False,
        expected_process_name="claude",
        ps_output="",
    )
    assert probe.state == AgentLifecycleState.DONE
    assert probe.pid is None


def test_pid_none_when_replaced() -> None:
    """A non-shell foreground is REPLACED and carries no main PID."""
    ps_output = "123 1 python3\n"
    probe = determine_lifecycle_probe_result(
        "0|python3|123",
        True,
        is_blocked_on_dialog=False,
        expected_process_name="claude",
        ps_output=ps_output,
    )
    assert probe.state == AgentLifecycleState.REPLACED
    assert probe.pid is None


def test_descendant_names_returns_empty_for_no_children() -> None:
    ps_output = "100 1 init\n200 1 sshd\n"
    result = get_descendant_process_names("999", ps_output)
    assert result == []


def test_descendant_names_finds_direct_children() -> None:
    ps_output = "100 1 init\n200 100 bash\n300 100 sshd\n"
    result = get_descendant_process_names("100", ps_output)
    assert set(result) == {"bash", "sshd"}


def test_descendant_names_finds_nested_children() -> None:
    ps_output = "100 1 init\n200 100 bash\n300 200 claude\n400 300 node\n"
    result = get_descendant_process_names("100", ps_output)
    assert result == ["bash", "claude", "node"]


def test_descendant_names_ignores_an_exiting_process_listed_with_pid_zero() -> None:
    result = get_descendant_process_names("123", _PS_OUTPUT_WITH_EXITING_PROCESS_AT_PID_ZERO)
    assert result == ["claude", "bash"]


def test_descendant_names_visits_each_process_once_when_the_parent_links_form_a_cycle() -> None:
    ps_output = "100 300 init\n200 100 bash\n300 200 sleep\n"
    result = get_descendant_process_names("100", ps_output)
    assert result == ["bash", "sleep"]


def test_descendant_pids_returns_empty_for_no_children() -> None:
    assert get_descendant_pids("999", "100 1 init\n200 1 sshd\n") == []


def test_descendant_pids_finds_the_whole_tree_and_nothing_outside_it() -> None:
    ps_output = "100 1 bash\n200 100 claude\n300 200 node\n400 100 tee\n500 1 sshd\n600 500 bash\n"
    assert get_descendant_pids("100", ps_output) == ["200", "400", "300"]


def test_descendant_pids_keeps_a_comm_with_spaces() -> None:
    assert get_descendant_pids("100", "100 1 bash\n200 100 tmux: server\n") == ["200"]


def test_descendant_pids_ignores_an_exiting_process_listed_with_pid_zero() -> None:
    assert get_descendant_pids("100", "100 1 bash\n0 100 sleep\n200 100 claude\n") == ["200"]


def test_descendant_pids_visits_each_process_once_when_the_parent_links_form_a_cycle() -> None:
    assert get_descendant_pids("100", "100 300 init\n200 100 bash\n300 200 sleep\n") == ["200", "300"]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/usr/bin/my-agent --flag", "my-agent"),
        ("sleep", "sleep"),
        ("( /usr/bin/script.sh session ) &", "script.sh"),
    ],
    ids=["path-with-args", "bare-command", "subshell-wrapped"],
)
def test_resolve_expected_process_name_falls_back_to_the_command_basename(command: str, expected: str) -> None:
    """Without a fixed name on the class, the listing expects what a loaded agent of the type expects."""
    config = MngrConfig.model_construct(agent_types={})
    assert resolve_expected_process_name("custom", CommandString(command), config) == expected


class _FixedProcessNameAgent(AgentInterface):
    """An agent class that always runs as one process, whatever its launch command."""

    fixed_process_name: ClassVar[str | None] = "codex"


@pytest.mark.parametrize(
    ("agent_type", "agent_types"),
    [
        ("codex", {}),
        ("my-codex", {AgentTypeName("my-codex"): AgentTypeConfig.model_construct(parent_type=AgentTypeName("codex"))}),
    ],
    ids=["registered-type", "custom-type-with-that-parent"],
)
def test_resolve_expected_process_name_reads_the_registered_class_fixed_name(
    agent_type: str, agent_types: dict[AgentTypeName, AgentTypeConfig]
) -> None:
    """A listing that knows only the type gets the name a loaded agent of the class reports, not the
    launch command's first word (a codex agent's command starts with a backgrounded subshell)."""
    try:
        register_agent_class("codex", _FixedProcessNameAgent)
        config = MngrConfig.model_construct(agent_types=agent_types)
        result = resolve_expected_process_name(
            agent_type, CommandString("( bash codex_background_tasks.sh ) & codex --remote"), config
        )
    finally:
        reset_agent_class_registry()

    assert result == "codex"


def test_check_agent_type_known_for_registered_type() -> None:
    try:
        register_agent_class("claude", type("FakeClaudeAgent", (), {}))
        config = MngrConfig.model_construct(agent_types={})
        assert check_agent_type_known("claude", config) is True
    finally:
        reset_agent_class_registry()


def test_check_agent_type_known_for_unregistered_type() -> None:
    config = MngrConfig.model_construct(agent_types={})
    assert check_agent_type_known("totally-unknown-type-xyz", config) is False


def test_check_agent_type_known_for_custom_type_with_registered_parent() -> None:
    try:
        register_agent_class("claude", type("FakeClaudeAgent", (), {}))
        custom_config = AgentTypeConfig.model_construct(parent_type=AgentTypeName("claude"))
        config = MngrConfig.model_construct(agent_types={AgentTypeName("my-claude"): custom_config})
        assert check_agent_type_known("my-claude", config) is True
    finally:
        reset_agent_class_registry()


def test_check_agent_type_known_for_custom_type_with_unregistered_parent() -> None:
    custom_config = AgentTypeConfig.model_construct(parent_type=AgentTypeName("totally-unknown-parent-xyz"))
    config = MngrConfig.model_construct(agent_types={AgentTypeName("my-custom"): custom_config})
    assert check_agent_type_known("my-custom", config) is False


def _get_safe_directories() -> list[str]:
    """Read safe.directory entries from the global gitconfig.

    Reads from ``~/.gitconfig`` in the fake HOME (created by the
    ``setup_git_config`` fixture via ``isolate_git``).
    """
    result = subprocess.run(
        ["git", "config", "--global", "--get-all", "safe.directory"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return result.stdout.strip().splitlines()


def test_add_safe_directory_on_remote_adds_entry_for_non_local_host(setup_git_config: None) -> None:
    """Test that add_safe_directory_on_remote writes to gitconfig for non-local hosts."""
    host = cast(OnlineHostInterface, FakeHost(is_local=False))
    target_path = Path("/some/agent/workdir")

    add_safe_directory_on_remote(host, target_path)

    safe_dirs = _get_safe_directories()
    assert str(target_path) in safe_dirs


def test_add_safe_directory_on_remote_is_noop_for_local_host(setup_git_config: None) -> None:
    """Test that add_safe_directory_on_remote does nothing for local hosts."""
    host = cast(OnlineHostInterface, FakeHost(is_local=True))
    target_path = Path("/some/agent/workdir")

    add_safe_directory_on_remote(host, target_path)

    safe_dirs = _get_safe_directories()
    assert str(target_path) not in safe_dirs


def test_symlink_on_host_symlinks_even_when_source_absent(local_host: OnlineHostInterface, tmp_path: Path) -> None:
    """symlink_on_host creates a (dangling) symlink to a not-yet-existing source, and a write goes through it."""
    source = tmp_path / "real" / "tok"
    dest = tmp_path / "agent" / "tok"

    symlink_on_host(local_host, source, dest, ensure_source_parent=True)

    assert dest.is_symlink()
    assert Path(os.readlink(dest)) == source
    # Dangling: source not created yet; ensure_source_parent created the shared parent dir.
    assert not dest.exists()
    assert source.parent.is_dir()
    # A write through the dangling symlink creates the source (the write-through property).
    dest.write_text("tok-data")
    assert source.read_text() == "tok-data"


def test_copy_on_host_copies_existing_source(local_host: OnlineHostInterface, tmp_path: Path) -> None:
    source = tmp_path / "real" / "tok"
    source.parent.mkdir(parents=True)
    source.write_text("secret")
    dest = tmp_path / "agent" / "tok"

    result = copy_on_host(local_host, source, dest)

    assert result is True
    assert not dest.is_symlink()
    assert dest.read_text() == "secret"
    assert (dest.stat().st_mode & 0o777) == 0o600


def test_copy_on_host_skips_when_source_absent(local_host: OnlineHostInterface, tmp_path: Path) -> None:
    source = tmp_path / "real" / "missing"
    dest = tmp_path / "agent" / "tok"

    result = copy_on_host(local_host, source, dest)

    assert result is False
    assert not dest.exists()


def test_build_ssh_transport_command_with_known_hosts_uses_strict_checking() -> None:
    result = build_ssh_transport_command(
        key_path=Path("/tmp/test_key"),
        port=2222,
        known_hosts_file=Path("/tmp/known_hosts"),
    )
    assert "ssh" in result
    assert "-i /tmp/test_key" in result
    assert "-p 2222" in result
    # Double-quoted for ssh: UserKnownHostsFile is a whitespace-separated list.
    assert "-o UserKnownHostsFile='\"/tmp/known_hosts\"'" in result
    assert "-o StrictHostKeyChecking=yes" in result
    assert "-o IdentitiesOnly=yes" in result
    assert "-o IdentityAgent=none" in result


def test_build_ssh_transport_command_without_known_hosts_uses_strict_checking() -> None:
    result = build_ssh_transport_command(
        key_path=Path("/tmp/test_key"),
        port=22,
        known_hosts_file=None,
    )
    assert "-o StrictHostKeyChecking=yes" in result
    assert "-o IdentitiesOnly=yes" in result
    assert "-o IdentityAgent=none" in result
    assert "UserKnownHostsFile" not in result


def test_build_ssh_transport_command_quotes_key_path_with_spaces() -> None:
    result = build_ssh_transport_command(
        key_path=Path("/path with spaces/key"),
        port=22,
        known_hosts_file=None,
    )
    assert "'/path with spaces/key'" in result


def test_build_ssh_transport_command_quotes_known_hosts_path_with_spaces() -> None:
    result = build_ssh_transport_command(
        key_path=Path("/tmp/key"),
        port=22,
        known_hosts_file=Path("/path with spaces/known_hosts"),
    )
    # The shell is the first parser: the option must survive its split as one
    # argument.
    parsed = shlex.split(result)
    option = next(arg for arg in parsed if arg.startswith("UserKnownHostsFile="))

    # ssh is the second, and splits on whitespace itself, so the value it sees
    # has to carry its own quotes.
    assert option == 'UserKnownHostsFile="/path with spaces/known_hosts"', option


def _make_host_with_known_hosts(known_hosts_file: str | None) -> OnlineHostInterface:
    """Create a minimal host-like object with the connector data needed for get_ssh_known_hosts_file."""
    data: dict[str, str] = {}
    if known_hosts_file is not None:
        data["ssh_known_hosts_file"] = known_hosts_file
    pyinfra_host = types.SimpleNamespace(data=data)
    connector = types.SimpleNamespace(host=pyinfra_host)
    return cast(OnlineHostInterface, types.SimpleNamespace(connector=connector))


def test_get_ssh_known_hosts_file_returns_path_when_configured() -> None:
    host = _make_host_with_known_hosts("/tmp/known_hosts")
    result = get_ssh_known_hosts_file(host)
    assert result == Path("/tmp/known_hosts")


def test_get_ssh_known_hosts_file_returns_none_when_not_configured() -> None:
    host = _make_host_with_known_hosts(None)
    result = get_ssh_known_hosts_file(host)
    assert result is None


def test_get_ssh_known_hosts_file_returns_none_for_dev_null() -> None:
    host = _make_host_with_known_hosts("/dev/null")
    result = get_ssh_known_hosts_file(host)
    assert result is None


# classify_waiting_reason tests


@pytest.mark.parametrize(
    "is_active, is_blocked, expected",
    [
        # Idle (turn over): END_OF_TURN regardless of a stranded permission marker.
        (False, False, WaitingReason.END_OF_TURN),
        (False, True, WaitingReason.END_OF_TURN),
        # In a turn: PERMISSIONS only while genuinely blocked, else actively running.
        (True, True, WaitingReason.PERMISSIONS),
        (True, False, None),
    ],
)
def test_classify_waiting_reason(is_active: bool, is_blocked: bool, expected: WaitingReason | None) -> None:
    """The shared gating rule behind every agent plugin's waiting_reason field
    generator: PERMISSIONS is gated on is_active, so a stranded permission marker
    (active absent) never yields PERMISSIONS."""
    assert classify_waiting_reason(is_active, is_blocked) == expected


@pytest.mark.parametrize(
    ("is_active", "is_blocked", "expected"),
    [
        # An agent in a turn is RUNNING only while it is making progress.
        (True, False, AgentLifecycleState.RUNNING),
        (True, True, AgentLifecycleState.WAITING),
        # Out of a turn it is WAITING either way; a stranded block marker cannot change that.
        (False, False, AgentLifecycleState.WAITING),
        (False, True, AgentLifecycleState.WAITING),
    ],
)
def test_blocked_on_dialog_reports_waiting(is_active: bool, is_blocked: bool, expected: AgentLifecycleState) -> None:
    probe = determine_lifecycle_probe_result(
        "0|claude|123",
        is_active,
        is_blocked_on_dialog=is_blocked,
        expected_process_name="claude",
        ps_output="",
    )
    assert probe.state == expected


def test_blocked_on_dialog_keeps_the_pid() -> None:
    """The pid is populated for RUNNING and WAITING alike, so a block must not drop it."""
    ps_output = "123 1 bash\n456 123 claude\n"
    probe = determine_lifecycle_probe_result(
        "0|claude|123",
        True,
        is_blocked_on_dialog=True,
        expected_process_name="claude",
        ps_output=ps_output,
    )
    assert probe.state == AgentLifecycleState.WAITING
    assert probe.pid == 456


def test_blocked_on_dialog_has_no_default() -> None:
    """The signal is required, so a caller cannot silently omit it."""
    with pytest.raises(TypeError):
        determine_lifecycle_probe_result(  # ty: ignore[missing-argument]
            "0|claude|123",
            True,
            expected_process_name="claude",
            ps_output="",
        )


@pytest.mark.parametrize(
    ("tmux_info", "expected"),
    [
        # No session, and a pane whose process has exited, need no process table to settle.
        (None, LifecycleProbeResult(state=AgentLifecycleState.STOPPED)),
        ("1|bash|4242", LifecycleProbeResult(state=AgentLifecycleState.DONE)),
        # tmux's own report of the foreground settles a live pane, though without a PID.
        ("0|claude|4242", LifecycleProbeResult(state=AgentLifecycleState.WAITING)),
        # A shell foreground may hide the agent below it, which only the process table could show.
        ("0|bash|4242", LifecycleProbeResult(state=AgentLifecycleState.UNKNOWN)),
    ],
)
def test_lifecycle_without_a_process_table_settles_only_what_tmux_shows(
    tmux_info: str | None, expected: LifecycleProbeResult
) -> None:
    probe = determine_lifecycle_probe_result(
        tmux_info,
        is_active=False,
        is_blocked_on_dialog=False,
        expected_process_name="claude",
        ps_output=None,
    )
    assert probe == expected


def test_lifecycle_snapshot_finds_an_agents_session_by_its_id_stamp_before_its_name() -> None:
    """A renamed agent's session carries its id under the new name, while a stale session may hold the old name."""
    agent_id = AgentId.generate()
    panes = parse_tmux_pane_listings(
        f"mngr-renamed::MNGR::{agent_id}::MNGR::agent::MNGR::0|claude|4242\n"
        "mngr-original::MNGR::::MNGR::agent::MNGR::0|bash|4343\n"
    )

    assert find_agent_session_name(panes, agent_id, "mngr-original") == "mngr-renamed"
    assert find_agent_session_name(panes, AgentId.generate(), "mngr-original") == "mngr-original"
    assert find_agent_session_name(panes, AgentId.generate(), "mngr-gone") is None


def _write_fake_program(bin_dir: Path, name: str, body: str) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    write_executable_script(bin_dir / name, f"#!/bin/sh\n{body}\n")


def _run_snapshot_command(command: str, path_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", "-c", command],
        env={"PATH": str(path_dir)},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_process_table_snapshot_reads_a_host_without_ps_but_not_one_whose_ps_fails(tmp_path: Path) -> None:
    """Minimal images ship no ps, so they list no processes; a ps that is there but fails says nothing."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    without_ps = _run_snapshot_command(PROCESS_TABLE_SNAPSHOT_COMMAND, bin_dir)

    assert without_ps.returncode == 0, without_ps.stderr
    assert without_ps.stdout == ""

    _write_fake_program(bin_dir, "ps", "exit 1")

    assert _run_snapshot_command(PROCESS_TABLE_SNAPSHOT_COMMAND, bin_dir).returncode != 0


@pytest.mark.parametrize(
    ("tmux_stderr", "is_readable"),
    [
        ("no server running on /tmp/tmux-501/default", True),
        ("error connecting to /tmp/tmux-501/default (No such file or directory)", True),
        ("lost server", False),
    ],
)
def test_tmux_panes_snapshot_reads_a_host_without_a_tmux_server_but_not_one_whose_tmux_fails(
    tmux_stderr: str, is_readable: bool, tmp_path: Path
) -> None:
    """No tmux server means no sessions, so its agents read STOPPED; any other tmux failure says nothing."""
    bin_dir = tmp_path / "bin"
    _write_fake_program(bin_dir, "tmux", f"echo {shlex.quote(tmux_stderr)} >&2\nexit 1")

    run = _run_snapshot_command(TMUX_PANES_SNAPSHOT_COMMAND, bin_dir)

    assert (run.returncode == 0) == is_readable, run.stderr
    if is_readable:
        assert parse_tmux_pane_listings(run.stdout) == ()


def test_tmux_panes_snapshot_reads_a_host_without_tmux_as_having_no_panes(tmp_path: Path) -> None:
    """A host with no tmux binary has no tmux sessions, so its agents read STOPPED rather than unreadable."""
    empty_bin_dir = tmp_path / "bin"
    empty_bin_dir.mkdir()

    run = _run_snapshot_command(TMUX_PANES_SNAPSHOT_COMMAND, empty_bin_dir)

    assert run.returncode == 0, run.stderr
    assert parse_tmux_pane_listings(run.stdout) == ()
