from pathlib import Path

import pytest

from imbue.mngr.errors import MngrError
from imbue.mngr.errors import UserInputError
from imbue.mngr.primitives import AgentId
from imbue.mngr_file.cli.target import ResolveFileTargetResult
from imbue.mngr_file.cli.target import _compute_agent_base_path
from imbue.mngr_file.cli.target import _is_volume_accessible_path
from imbue.mngr_file.cli.target import compute_volume_path
from imbue.mngr_file.cli.target import resolve_full_path
from imbue.mngr_file.data_types import PathRelativeTo

# --- resolve_full_path ---


def test_resolve_full_path_with_relative_path() -> None:
    assert resolve_full_path(Path("/home/user/work"), "config.toml") == Path("/home/user/work/config.toml")


def test_resolve_full_path_with_nested_relative_path() -> None:
    assert resolve_full_path(Path("/home/user/work"), "subdir/file.txt") == Path("/home/user/work/subdir/file.txt")


def test_resolve_full_path_with_absolute_path_ignores_base() -> None:
    assert resolve_full_path(Path("/home/user/work"), "/etc/hostname") == Path("/etc/hostname")


def test_resolve_full_path_with_dot_relative_path() -> None:
    assert resolve_full_path(Path("/home/user/work"), "./local/file.txt") == Path("/home/user/work/local/file.txt")


# --- _compute_agent_base_path ---


def test_compute_agent_base_path_work() -> None:
    work_dir = Path("/agent/work")
    result = _compute_agent_base_path(PathRelativeTo.WORK, work_dir, Path("/home/.mngr"), AgentId.generate())
    assert result == work_dir


def test_compute_agent_base_path_state() -> None:
    host_dir = Path("/home/user/.mngr")
    agent_id = AgentId.generate()
    result = _compute_agent_base_path(PathRelativeTo.STATE, Path("/work"), host_dir, agent_id)
    assert result == host_dir / "agents" / str(agent_id)


def test_compute_agent_base_path_host() -> None:
    host_dir = Path("/home/user/.mngr")
    result = _compute_agent_base_path(PathRelativeTo.HOST, Path("/work"), host_dir, AgentId.generate())
    assert result == host_dir


# --- _is_volume_accessible_path ---


def test_is_volume_accessible_path_work_returns_false() -> None:
    assert _is_volume_accessible_path(PathRelativeTo.WORK) is False


def test_is_volume_accessible_path_state_returns_true() -> None:
    assert _is_volume_accessible_path(PathRelativeTo.STATE) is True


def test_is_volume_accessible_path_host_returns_true() -> None:
    assert _is_volume_accessible_path(PathRelativeTo.HOST) is True


# --- compute_volume_path ---


def test_compute_volume_path_host_with_user_path() -> None:
    assert (
        compute_volume_path(PathRelativeTo.HOST, agent_id=None, user_path="events/logs/events.jsonl")
        == "events/logs/events.jsonl"
    )


def test_compute_volume_path_host_without_user_path() -> None:
    assert compute_volume_path(PathRelativeTo.HOST, agent_id=None, user_path=None) == "."


def test_compute_volume_path_state_with_user_path() -> None:
    agent_id = AgentId.generate()
    assert (
        compute_volume_path(PathRelativeTo.STATE, agent_id=agent_id, user_path="file.txt")
        == f"agents/{agent_id}/file.txt"
    )


def test_compute_volume_path_state_without_user_path() -> None:
    agent_id = AgentId.generate()
    assert compute_volume_path(PathRelativeTo.STATE, agent_id=agent_id, user_path=None) == f"agents/{agent_id}"


def test_compute_volume_path_state_without_agent_id_raises() -> None:
    with pytest.raises(UserInputError, match="requires an agent target"):
        compute_volume_path(PathRelativeTo.STATE, agent_id=None, user_path="file.txt")


def test_compute_volume_path_work_raises() -> None:
    with pytest.raises(UserInputError, match="offline"):
        compute_volume_path(PathRelativeTo.WORK, agent_id=AgentId.generate(), user_path="file.txt")


# --- ResolveFileTargetResult ---


def test_resolve_file_target_result_host_raises_when_offline() -> None:
    result = ResolveFileTargetResult(
        online_host=None,
        volume=None,
        base_path=Path("/test"),
        is_agent=False,
        agent_id=None,
        relative_to=PathRelativeTo.HOST,
    )
    with pytest.raises(MngrError, match="offline"):
        _ = result.host


def test_resolve_file_target_result_is_online_false_when_no_host() -> None:
    result = ResolveFileTargetResult(
        online_host=None,
        volume=None,
        base_path=Path("/test"),
        is_agent=False,
        agent_id=None,
        relative_to=PathRelativeTo.HOST,
    )
    assert result.is_online is False
