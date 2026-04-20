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
from imbue.mngr.interfaces.host import OnlineHostInterface
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


def render_file_diagnostic(
    host: OnlineHostInterface,
    path: Path,
    label: str,
    *,
    tail_chars: int | None = None,
    show_path: bool = True,
) -> str:
    """Render a single-file diagnostic line (for silent-exit post-mortems).

    Probes existence and size of ``path`` via ``host.get_file_mtime`` +
    ``host.read_text_file``, and returns one rendered string summarising
    what was found. Best-effort: filesystem / remote-host errors and
    decode errors are trace-logged and folded into the rendered line so
    they neither mask the caller's primary error nor disappear silently.

    ``label`` is used verbatim as the line prefix (the caller chooses
    their own label / indent style). When ``show_path`` is True, the
    rendered path is included after the label (format ``{label}: {path}
    -- ...``); when False, the path is omitted (format ``{label}: ...``)
    for callers that already report the directory separately. When
    ``tail_chars`` is not None, up to that many trailing characters of
    the decoded file are appended after a ``, tail:\\n`` separator; when
    None, only the char count is reported.

    The returned string never contains a trailing newline.
    """
    prefix = f"{label}: {path} -- " if show_path else f"{label}: "
    mtime_error: str | None = None
    try:
        mtime = host.get_file_mtime(path)
    except (OSError, HostError) as e:
        logger.trace("get_file_mtime({}) failed: {}", path, e)
        mtime = None
        mtime_error = str(e)
    if mtime is None:
        # Distinguish a genuinely-missing file ("does not exist") from a
        # probe failure so triage isn't misled by a transient filesystem /
        # remote-host error that just happens to look like a missing file.
        if mtime_error is not None:
            return f"{prefix}mtime probe failed: {mtime_error}"
        return f"{prefix}does not exist"
    try:
        content = host.read_text_file(path)
    except FileNotFoundError:
        # Raced with deletion between mtime probe and read.
        return f"{prefix}does not exist"
    except (OSError, HostError, UnicodeDecodeError) as e:
        # UnicodeDecodeError lives here (not OSError) because read_text_file
        # decodes the file as UTF-8 and subprocess output isn't guaranteed
        # to be valid UTF-8. Treating it as a read failure honours the
        # best-effort contract documented above.
        logger.trace("read_text_file({}) failed: {}", path, e)
        return f"{prefix}exists, read failed: {e}"
    # `content` is a decoded str, so len() counts characters, not bytes;
    # label accordingly so triage isn't misled on non-ASCII output. Any
    # tail slice is intentionally character-based (we're slicing decoded
    # text, not raw bytes).
    char_count = len(content)
    # Skip the ", tail:\n..." suffix when there is nothing to tail. Emitting
    # a dangling `tail:` with an empty body (the case for redirect files
    # that were created but never written to) is visual noise and suggests
    # output follows when there is none; match the no-tail-chars format so
    # callers always see `N chars` when N is 0.
    if tail_chars is None or char_count == 0:
        return f"{prefix}{char_count} chars"
    tail = content[-tail_chars:] if char_count > tail_chars else content
    return f"{prefix}{char_count} chars, tail:\n{tail}".rstrip()


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
        """Return an inventory of the stdout/stderr files plus the agent's lifecycle state.

        Each stdout/stderr line reports existence, size, and tail; a trailing
        ``lifecycle: ...`` line reports the current lifecycle state (or folds
        in the probe error if the state cannot be read).

        Called by :meth:`_raise_no_output_error` and unconditionally included
        in every no-output error message, alongside stderr / extra sources /
        tmux pane. Particularly useful when those other sources are empty
        (e.g. claude exited silently), because it confirms whether the
        redirect files were ever created, how big they are, and includes the
        tail of each so release-test post-mortems aren't stuck at "exited
        without producing output". Delegates per-file rendering to
        :func:`render_file_diagnostic` so the format stays in lockstep with
        other silent-exit diagnostics (e.g. HeadlessClaude's work-dir
        inventory).

        Always returns a non-empty string -- the iterated (stdout, stderr)
        tuple is hard-coded and every rendered line is unconditionally
        appended, so at least one line is guaranteed.
        """
        lines: list[str] = [
            render_file_diagnostic(self.host, self._get_stdout_path(), "stdout", tail_chars=1024),
            render_file_diagnostic(self.host, self._get_stderr_path(), "stderr", tail_chars=1024),
        ]

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
            # Fold the error into the rendered output rather than dropping
            # it -- trace-level logging alone would effectively hide this
            # information, defeating the purpose of the diagnostic.
            lines.append(f"lifecycle: probe failed: {e}")

        return "\n".join(lines)

    def _raise_no_output_error(self) -> Never:
        """Raise MngrError collecting all available error detail.

        Gathers stderr, subclass-specific extra sources, the tmux pane
        content, and the state-dir inventory. All sources are *always*
        captured -- silent-exit post-mortems (e.g. test_ask_simple_query
        with 0-char stdout/stderr) need every signal we can get. Shell
        errors like "cat: .mngr-prompt: No such file" only appear in the
        tmux pane because the redirect captures the claude process's
        stdout/stderr, not the shell's own.
        """
        parts: list[str] = []

        stderr_error = self._get_stderr_error_message()
        if stderr_error:
            parts.append(stderr_error)

        parts.extend(self._get_extra_error_sources())

        pane_error = self._get_pane_error_message()
        if pane_error:
            parts.append(f"[tmux pane]\n{pane_error}")

        # The state-dir diagnostic string is always non-empty, so `parts`
        # is guaranteed to have at least one element at the raise below.
        parts.append(f"[state-dir]\n{self._get_state_dir_diagnostic()}")

        subject = self._no_output_error_subject
        detail = "\n".join(parts)
        raise MngrError(f"{subject} exited without producing output:\n{detail}")
