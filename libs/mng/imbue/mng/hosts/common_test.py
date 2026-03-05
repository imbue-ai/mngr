import subprocess
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import cast

from imbue.mng.api.testing import FakeHost
from imbue.mng.config.data_types import AgentTypeConfig
from imbue.mng.config.data_types import MngConfig
from imbue.mng.hosts.common import add_safe_directory_on_remote
from imbue.mng.hosts.common import compute_idle_seconds
from imbue.mng.hosts.common import determine_lifecycle_state
from imbue.mng.hosts.common import get_descendant_process_names
from imbue.mng.hosts.common import resolve_expected_process_name
from imbue.mng.hosts.common import timestamp_to_datetime
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.primitives import AgentLifecycleState
from imbue.mng.primitives import AgentTypeName
from imbue.mng.primitives import CommandString

# =========================================================================
# timestamp_to_datetime tests
# =========================================================================


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


# =========================================================================
# compute_idle_seconds tests
# =========================================================================


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


# =========================================================================
# determine_lifecycle_state tests
# =========================================================================


def test_lifecycle_stopped_when_no_tmux_info() -> None:
    assert determine_lifecycle_state(None, False, "claude", "") == AgentLifecycleState.STOPPED


def test_lifecycle_stopped_when_malformed_tmux_info() -> None:
    assert determine_lifecycle_state("bad", False, "claude", "") == AgentLifecycleState.STOPPED


def test_lifecycle_done_when_pane_dead() -> None:
    assert determine_lifecycle_state("1|bash|123", False, "claude", "") == AgentLifecycleState.DONE


def test_lifecycle_running_when_command_matches_and_active() -> None:
    assert determine_lifecycle_state("0|claude|123", True, "claude", "") == AgentLifecycleState.RUNNING


def test_lifecycle_waiting_when_command_matches_and_not_active() -> None:
    assert determine_lifecycle_state("0|claude|123", False, "claude", "") == AgentLifecycleState.WAITING


def test_lifecycle_running_when_descendant_matches() -> None:
    ps_output = "100 1 init\n200 123 bash\n300 200 claude\n"
    assert determine_lifecycle_state("0|bash|123", True, "claude", ps_output) == AgentLifecycleState.RUNNING


def test_lifecycle_replaced_when_non_shell_descendant() -> None:
    ps_output = "200 123 python3\n"
    assert determine_lifecycle_state("0|bash|123", True, "claude", ps_output) == AgentLifecycleState.REPLACED


def test_lifecycle_done_when_shell_only() -> None:
    assert determine_lifecycle_state("0|bash|123", True, "claude", "") == AgentLifecycleState.DONE


def test_lifecycle_replaced_when_unknown_command() -> None:
    assert determine_lifecycle_state("0|python3|123", True, "claude", "") == AgentLifecycleState.REPLACED


# =========================================================================
# get_descendant_process_names tests
# =========================================================================


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


# =========================================================================
# resolve_expected_process_name tests
# =========================================================================


def test_resolve_expected_process_name_for_claude() -> None:
    config = MngConfig.model_construct(agent_types={})
    result = resolve_expected_process_name("claude", CommandString("complex wrapper command"), config)
    assert result == "claude"


def test_resolve_expected_process_name_for_simple_command() -> None:
    config = MngConfig.model_construct(agent_types={})
    result = resolve_expected_process_name("custom", CommandString("/usr/bin/my-agent --flag"), config)
    assert result == "my-agent"


def test_resolve_expected_process_name_for_custom_type_with_claude_parent() -> None:
    custom_config = AgentTypeConfig.model_construct(parent_type=AgentTypeName("claude"))
    config = MngConfig.model_construct(agent_types={AgentTypeName("my-claude"): custom_config})
    result = resolve_expected_process_name("my-claude", CommandString("complex wrapper"), config)
    assert result == "claude"


def test_resolve_expected_process_name_for_bare_command() -> None:
    config = MngConfig.model_construct(agent_types={})
    result = resolve_expected_process_name("unknown", CommandString("sleep"), config)
    assert result == "sleep"


# =========================================================================
# add_safe_directory_on_remote tests
# =========================================================================


def _get_safe_directories(tmp_path: Path) -> list[str]:
    """Read safe.directory entries from the gitconfig in the test HOME."""
    gitconfig = tmp_path / ".gitconfig"
    if not gitconfig.exists():
        return []
    result = subprocess.run(
        ["git", "config", "--global", "--get-all", "safe.directory"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return result.stdout.strip().splitlines()


def test_add_safe_directory_on_remote_adds_entry_for_non_local_host(tmp_path: Path) -> None:
    """Test that add_safe_directory_on_remote writes to gitconfig for non-local hosts."""
    host = cast(OnlineHostInterface, FakeHost(is_local=False))
    target_path = Path("/some/agent/workdir")

    add_safe_directory_on_remote(host, target_path)

    safe_dirs = _get_safe_directories(tmp_path)
    assert str(target_path) in safe_dirs


def test_add_safe_directory_on_remote_is_noop_for_local_host(tmp_path: Path) -> None:
    """Test that add_safe_directory_on_remote does nothing for local hosts."""
    host = cast(OnlineHostInterface, FakeHost(is_local=True))
    target_path = Path("/some/agent/workdir")

    add_safe_directory_on_remote(host, target_path)

    safe_dirs = _get_safe_directories(tmp_path)
    assert str(target_path) not in safe_dirs
