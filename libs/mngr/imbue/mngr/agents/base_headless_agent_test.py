from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.agents.base_headless_agent import BaseHeadlessAgent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import SendMessageError
from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName


class _ConcreteHeadlessAgent(BaseHeadlessAgent[AgentTypeConfig]):
    """Minimal concrete subclass for testing BaseHeadlessAgent."""

    def _get_stdout_path(self) -> Path:
        return self._get_agent_dir() / "stdout.log"

    def _get_stderr_path(self) -> Path:
        return self._get_agent_dir() / "stderr.log"

    def stream_output(self) -> Iterator[str]:
        raise NotImplementedError


class _AlwaysStopped(_ConcreteHeadlessAgent):
    """Test subclass that always reports STOPPED lifecycle state."""

    def get_lifecycle_state(self) -> AgentLifecycleState:
        return AgentLifecycleState.STOPPED


def _make_agent(
    host: Host,
    mngr_ctx: MngrContext,
    tmp_path: Path,
    is_always_stopped: bool = False,
) -> _ConcreteHeadlessAgent:
    """Create a concrete BaseHeadlessAgent for testing."""
    work_dir = tmp_path / f"work-{str(AgentId.generate().get_uuid())[:8]}"
    work_dir.mkdir()

    cls = _AlwaysStopped if is_always_stopped else _ConcreteHeadlessAgent
    return cls.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-headless"),
        agent_type=AgentTypeName("test_headless"),
        work_dir=work_dir,
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=mngr_ctx,
        agent_config=AgentTypeConfig(),
        host=host,
    )


# =============================================================================
# Tests for shared methods
# =============================================================================


def test_preflight_send_message_raises(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_agent(local_host, temp_mngr_ctx, tmp_path)
    with pytest.raises(SendMessageError, match="do not accept interactive messages"):
        agent._preflight_send_message("some-target")


def test_send_message_raises(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_agent(local_host, temp_mngr_ctx, tmp_path)
    with pytest.raises(SendMessageError, match="do not accept interactive messages"):
        agent.send_message("hello")


def test_uses_paste_detection_send_returns_false(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_agent(local_host, temp_mngr_ctx, tmp_path)
    assert agent.uses_paste_detection_send() is False


def test_get_tui_ready_indicator_returns_none(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_agent(local_host, temp_mngr_ctx, tmp_path)
    assert agent.get_tui_ready_indicator() is None


def test_is_agent_finished_when_stopped(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_agent(local_host, temp_mngr_ctx, tmp_path, is_always_stopped=True)
    assert agent._is_agent_finished() is True


def test_file_exists_on_host_returns_false_for_missing(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_agent(local_host, temp_mngr_ctx, tmp_path)
    assert agent._file_exists_on_host(tmp_path / "nonexistent") is False


def test_file_exists_on_host_returns_true_for_existing(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_agent(local_host, temp_mngr_ctx, tmp_path)
    existing = tmp_path / "exists.txt"
    existing.write_text("data")
    assert agent._file_exists_on_host(existing) is True


def test_get_stderr_error_message_returns_none_when_missing(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_agent(local_host, temp_mngr_ctx, tmp_path)
    assert agent._get_stderr_error_message() is None


def test_get_stderr_error_message_returns_content(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_agent(local_host, temp_mngr_ctx, tmp_path)
    agent_dir = agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "stderr.log").write_text("some error\n")
    assert agent._get_stderr_error_message() == "some error"


def test_get_stderr_error_message_returns_none_when_empty(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_agent(local_host, temp_mngr_ctx, tmp_path)
    agent_dir = agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "stderr.log").write_text("")
    assert agent._get_stderr_error_message() is None
