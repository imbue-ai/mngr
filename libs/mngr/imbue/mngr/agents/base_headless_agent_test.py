from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.mngr.agents.base_headless_agent import BaseHeadlessAgent
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
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


class _StoppedWithPaneContent(_AlwaysStopped):
    """Test subclass that always reports STOPPED and returns fixed pane content."""

    _pane_content: str | None = None

    def capture_pane_content(self, include_scrollback: bool = False) -> str | None:
        return self._pane_content


_UNSET = object()


def _make_agent(
    host: Host,
    mngr_ctx: MngrContext,
    tmp_path: Path,
    is_always_stopped: bool = False,
    pane_content: str | None | object = _UNSET,
) -> _ConcreteHeadlessAgent:
    """Create a concrete BaseHeadlessAgent for testing.

    Pass pane_content (including None) to create a _StoppedWithPaneContent
    agent that returns that value from capture_pane_content without invoking
    tmux. Omit pane_content entirely to use the default agent.
    """
    work_dir = tmp_path / f"work-{str(AgentId.generate().get_uuid())[:8]}"
    work_dir.mkdir()

    if pane_content is not _UNSET:
        cls: type[_ConcreteHeadlessAgent] = _StoppedWithPaneContent
    elif is_always_stopped:
        cls = _AlwaysStopped
    else:
        cls = _ConcreteHeadlessAgent

    agent = cls.model_construct(
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
    if isinstance(agent, _StoppedWithPaneContent) and pane_content is not _UNSET:
        assert isinstance(pane_content, str) or pane_content is None
        agent._pane_content = pane_content
    return agent


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


# =============================================================================
# Tests for _get_pane_error_message
# =============================================================================


def test_get_pane_error_message_returns_content(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_agent(local_host, temp_mngr_ctx, tmp_path, pane_content="error in pane\n")
    assert agent._get_pane_error_message() == "error in pane"


def test_get_pane_error_message_returns_none_when_no_pane(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_agent(local_host, temp_mngr_ctx, tmp_path, pane_content=None)
    assert agent._get_pane_error_message() is None


def test_get_pane_error_message_returns_none_when_empty(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    agent = _make_agent(local_host, temp_mngr_ctx, tmp_path, pane_content="  \n  ")
    assert agent._get_pane_error_message() is None


# =============================================================================
# Tests for _raise_no_output_error
# =============================================================================


def test_raise_no_output_error_surfaces_pane_content_when_no_files(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """When neither stdout nor stderr files exist, pane content is surfaced."""
    agent = _make_agent(local_host, temp_mngr_ctx, tmp_path, pane_content="pane-err-content")
    with pytest.raises(MngrError, match="pane-err-content"):
        agent._raise_no_output_error()


def test_raise_no_output_error_surfaces_pane_content_when_files_exist_but_empty(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """When redirect files exist but are empty, pane content is still surfaced.

    Shell redirects (> stdout 2> stderr) create empty files even when the
    process fails immediately. The pane capture should be used as a fallback
    regardless of whether the redirect files exist.
    """
    agent = _make_agent(local_host, temp_mngr_ctx, tmp_path, pane_content="startup-crash-output")
    agent_dir = agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "stdout.log").write_text("")
    (agent_dir / "stderr.log").write_text("")
    with pytest.raises(MngrError, match="startup-crash-output"):
        agent._raise_no_output_error()


def test_raise_no_output_error_state_dir_reports_missing_files(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """When stdout/stderr files do not exist, the [state-dir] section says so.

    This guards the diagnostic branch used by release-test post-mortems when
    the subprocess exited before any redirect file was even created.
    """
    agent = _make_agent(local_host, temp_mngr_ctx, tmp_path, pane_content="pane")
    with pytest.raises(MngrError) as excinfo:
        agent._raise_no_output_error()
    message = str(excinfo.value)
    assert "[state-dir]" in message
    assert "stdout:" in message
    assert "stderr:" in message
    assert "does not exist" in message


def test_raise_no_output_error_state_dir_reports_char_counts_and_tails(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """When files are present with small content, char counts and tails are reported."""
    agent = _make_agent(local_host, temp_mngr_ctx, tmp_path, pane_content="pane")
    agent_dir = agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    # Use distinct lengths so the per-file char-count assertions are
    # independent: "12 chars" vs "20 chars" each match only their own line.
    stdout_content = "hello-stdout"
    stderr_content = "stderr-content-extra"
    assert len(stdout_content) != len(stderr_content), "test data must have distinct lengths"
    (agent_dir / "stdout.log").write_text(stdout_content)
    (agent_dir / "stderr.log").write_text(stderr_content)
    with pytest.raises(MngrError) as excinfo:
        agent._raise_no_output_error()
    message = str(excinfo.value)
    assert "[state-dir]" in message
    assert f"{len(stdout_content)} chars" in message
    assert f"{len(stderr_content)} chars" in message
    assert stdout_content in message
    assert stderr_content in message


def test_raise_no_output_error_state_dir_truncates_long_content_to_tail(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """Content longer than 1024 chars should be truncated to the last 1024 chars.

    Uses a distinct prefix and suffix so we can verify that the tail (not the
    head) was kept, and that the reported char count matches the full length.
    """
    agent = _make_agent(local_host, temp_mngr_ctx, tmp_path, pane_content="pane")
    agent_dir = agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    head_marker = "HEAD-SHOULD-BE-TRUNCATED"
    # 2000 chars of filler keeps the head well outside the 1024-char tail
    # window; tail_marker sits at the very end so it must appear in output.
    filler = "x" * 2000
    tail_marker = "TAIL-MUST-SURVIVE"
    long_content = head_marker + filler + tail_marker
    (agent_dir / "stdout.log").write_text(long_content)
    (agent_dir / "stderr.log").write_text("")
    with pytest.raises(MngrError) as excinfo:
        agent._raise_no_output_error()
    message = str(excinfo.value)
    assert f"{len(long_content)} chars" in message
    assert tail_marker in message
    assert head_marker not in message


def test_raise_no_output_error_state_dir_handles_non_utf8_bytes(
    local_host: Host,
    temp_mngr_ctx: MngrContext,
    tmp_path: Path,
) -> None:
    """Non-UTF-8 bytes in stdout/stderr should fold into 'read failed', not raise.

    The diagnostic is documented as best-effort: read failures (including
    decode errors, since read_text_file decodes as UTF-8) must not mask
    the caller's primary "exited without producing output" error.
    """
    agent = _make_agent(local_host, temp_mngr_ctx, tmp_path, pane_content="pane")
    agent_dir = agent._get_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    # 0xff is never valid as a UTF-8 leading byte, so decoding will raise
    # UnicodeDecodeError. Writing via write_bytes() bypasses any encoding
    # step that would otherwise silently fix up the content.
    (agent_dir / "stdout.log").write_bytes(b"\xff\xfe garbage \xff")
    (agent_dir / "stderr.log").write_text("")
    with pytest.raises(MngrError) as excinfo:
        agent._raise_no_output_error()
    message = str(excinfo.value)
    # The diagnostic contract: the stdout line must render as a read
    # failure (not propagate UnicodeDecodeError, not claim "does not
    # exist", not go missing). The caller's subject must still be present.
    assert "[state-dir]" in message
    assert "exists, read failed" in message
    assert "exited without producing output" in message
