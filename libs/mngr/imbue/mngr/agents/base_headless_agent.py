from __future__ import annotations

from abc import abstractmethod
from pathlib import Path
from typing import Never

from loguru import logger

from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.errors import HostError
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.interfaces.agent import AgentConfigT
from imbue.mngr.interfaces.agent import StreamingHeadlessAgentMixin
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.utils.polling import poll_until

TAIL_POLL_INTERVAL: float = 0.05
TAIL_POLL_TIMEOUT: float = 300.0
# Default startup grace period before trusting lifecycle state. During startup
# the tmux pane may show the shell as the current command, making the agent
# look DONE/STOPPED before the real process has started. Subclasses can
# override _startup_grace_seconds to increase this (e.g. Claude needs longer
# due to nvm resolution and node startup).
STARTUP_GRACE_SECONDS: float = 2.0


class BaseHeadlessAgent(BaseAgent[AgentConfigT], StreamingHeadlessAgentMixin):
    """Base class for headless agents that capture output to files.

    Provides shared infrastructure for agents that redirect stdout/stderr
    to files and expose output programmatically. Subclasses must implement
    _get_stdout_path, _get_stderr_path, and stream_output.

    Subclasses can customize behavior by overriding:
    - _no_output_error_subject: the subject for "X exited without producing output" messages
    - _get_extra_error_sources(): additional error sources beyond stderr (e.g. stdout JSON errors)
    - _startup_grace_seconds: how long to wait for the process to start before trusting lifecycle state
    """

    _no_output_error_subject: str = "Command"
    _startup_grace_seconds: float = STARTUP_GRACE_SECONDS

    @abstractmethod
    def _get_stdout_path(self) -> Path:
        """Return the path to the stdout output file for this agent."""
        ...

    @abstractmethod
    def _get_stderr_path(self) -> Path:
        """Return the path to the stderr output file for this agent."""
        ...

    def _preflight_send_message(self, tmux_target: str) -> None:
        """Headless agents do not accept interactive messages."""
        raise SendMessageError(
            str(self.name),
            "Headless agents do not accept interactive messages.",
        )

    def uses_paste_detection_send(self) -> bool:
        return False

    def get_tui_ready_indicator(self) -> str | None:
        return None

    def _is_agent_finished(self) -> bool:
        """Check if the agent process has exited (tmux lifecycle) or is no longer running."""
        state = self.get_lifecycle_state()
        return state in (AgentLifecycleState.STOPPED, AgentLifecycleState.DONE)

    def _file_exists_on_host(self, path: Path) -> bool:
        """Check if a file exists on the agent's host (works for both local and remote)."""
        return self.host.get_file_mtime(path) is not None

    def _wait_for_stdout_file(self, stdout_path: Path) -> bool:
        """Wait for the stdout file to be created or the agent to exit.

        Returns True if the file exists, False if the agent exited without creating it.

        Two phases:
        1. Startup grace period -- wait for the file only, ignoring lifecycle
           state. During startup the tmux pane shows the shell as the current
           command, making lifecycle detection incorrectly report DONE.
        2. After the grace period, also check lifecycle state so we don't wait
           forever if the process genuinely failed to start.
        """
        # Phase 1: wait for stdout file, ignoring lifecycle state
        if poll_until(
            lambda: self._file_exists_on_host(stdout_path),
            timeout=self._startup_grace_seconds,
            poll_interval=TAIL_POLL_INTERVAL,
        ):
            return True
        # Phase 2: file didn't appear during grace period, now also check lifecycle
        poll_until(
            lambda: self._file_exists_on_host(stdout_path) or self._is_agent_finished(),
            timeout=max(0.0, TAIL_POLL_TIMEOUT - self._startup_grace_seconds),
            poll_interval=TAIL_POLL_INTERVAL,
        )
        return self._file_exists_on_host(stdout_path)

    def output(self) -> str:
        """Wait for the agent to finish and return its complete output."""
        return "".join(self.stream_output())

    def _get_stderr_error_message(self) -> str | None:
        """Read stderr file for error output."""
        stderr_path = self._get_stderr_path()
        try:
            content = self.host.read_text_file(stderr_path)
        except FileNotFoundError:
            return None
        stripped = content.strip()
        return stripped if stripped else None

    def _get_pane_error_message(self) -> str | None:
        """Capture the tmux pane content as a last-resort error source."""
        content = self.capture_pane_content()
        if content is None:
            return None
        stripped = content.strip()
        return stripped if stripped else None

    def _get_extra_error_sources(self) -> list[str]:
        """Return additional error details beyond stderr.

        Subclasses can override to check additional error sources (e.g.
        stdout JSON error results). Called by _raise_no_output_error after
        stderr is checked and before the pane capture fallback.
        """
        return []

    def _get_state_dir_diagnostic(self) -> str:
        """Return a short inventory of the stdout/stderr files' existence and size.

        Useful when stderr is empty and the tmux pane only shows the original
        command (e.g. claude exited silently). Confirms whether the redirect
        files were ever created, how big they are, and includes the tail of
        each so release-test post-mortems aren't stuck at "exited without
        producing output". Best-effort: filesystem / remote-host errors are
        trace-logged and folded into the rendered line so they neither mask
        the caller's primary error nor disappear silently.

        Always returns a non-empty string -- the iterated (stdout, stderr)
        tuple is hard-coded, so at least one rendered line is guaranteed.
        """
        stdout_path = self._get_stdout_path()
        stderr_path = self._get_stderr_path()

        lines: list[str] = []
        for label, path in (("stdout", stdout_path), ("stderr", stderr_path)):
            try:
                mtime = self.host.get_file_mtime(path)
            except (OSError, HostError) as e:
                logger.trace("get_file_mtime({}) failed: {}", path, e)
                mtime = None
            if mtime is None:
                lines.append(f"{label}: {path} -- does not exist")
                continue
            try:
                content = self.host.read_text_file(path)
            except FileNotFoundError:
                # Raced with deletion between mtime probe and read.
                lines.append(f"{label}: {path} -- does not exist")
                continue
            except (OSError, HostError) as e:
                logger.trace("read_text_file({}) failed: {}", path, e)
                lines.append(f"{label}: {path} -- exists, read failed: {e}")
                continue
            # `content` is a decoded str, so len() counts characters, not
            # bytes; label accordingly so triage isn't misled on non-ASCII
            # output. The 1024-char tail cap is intentionally character-
            # based (we're slicing decoded text, not raw bytes).
            char_count = len(content)
            tail = content[-1024:] if char_count > 1024 else content
            lines.append(f"{label}: {path} -- {char_count} chars, tail:\n{tail}".rstrip())
        # `lines` is guaranteed non-empty: the loop iterates over a
        # hard-coded 2-tuple and every branch unconditionally appends.
        assert lines

        # Include the current lifecycle state so we can distinguish
        # "agent never started" from "agent exited without output" in
        # post-mortems. DONE/STOPPED with 0-char stdout/stderr means the
        # command ran and returned without ever producing output (e.g.
        # claude CLI silently exiting on auth failure or TTY prompt).
        try:
            lifecycle = self.get_lifecycle_state()
            lines.append(f"lifecycle: {lifecycle.value}")
        except (OSError, HostError) as e:
            logger.trace("get_lifecycle_state failed: {}", e)

        return "\n".join(lines)

    def _raise_no_output_error(self) -> Never:
        """Raise MngrError collecting all available error detail.

        Checks stderr, then subclass-specific extra sources, then falls
        back to tmux pane capture as a last resort. The pane capture is
        always attempted when no other details are found, regardless of
        whether redirect files exist -- shell redirects create empty files
        even when the process fails immediately.
        """
        parts: list[str] = []

        stderr_error = self._get_stderr_error_message()
        if stderr_error:
            parts.append(stderr_error)

        parts.extend(self._get_extra_error_sources())

        if not parts:
            pane_error = self._get_pane_error_message()
            if pane_error:
                parts.append(f"[tmux pane]\n{pane_error}")

        # Always add the state-dir diagnostic so release-test post-mortems
        # have the stdout/stderr sizes + tails even when everything else is
        # empty. See test_ask_simple_query "claude exited without producing
        # output" debugging notes. The diagnostic string is always non-empty,
        # so `parts` is guaranteed to have at least one element here.
        parts.append(f"[state-dir]\n{self._get_state_dir_diagnostic()}")

        subject = self._no_output_error_subject
        detail = "\n".join(parts)
        raise MngrError(f"{subject} exited without producing output:\n{detail}")
