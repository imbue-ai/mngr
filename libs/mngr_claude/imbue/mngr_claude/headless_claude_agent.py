from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Callable
from typing import Never

from loguru import logger
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import NoCommandDefinedError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import NoPermissionsAgentMixin
from imbue.mngr.interfaces.agent import StreamingHeadlessAgentMixin
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.primitives import CommandString
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_claude import hookimpl
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_claude.plugin import ClaudeAgentConfig

_TAIL_POLL_INTERVAL: float = 0.05
_TAIL_POLL_TIMEOUT: float = 300.0


@pure
def extract_text_delta(line: str) -> str | None:
    """Extract text from a stream-json content_block_delta event.

    Returns the delta text if the line is a content_block_delta with a text_delta,
    or None otherwise.
    """
    try:
        parsed = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        # Expected: the stream contains non-JSON lines (blank lines, debug
        # output that claude sometimes leaks to stdout). Skip them silently.
        return None

    if parsed.get("type") != "stream_event":
        return None

    event = parsed.get("event")
    if not isinstance(event, dict):
        return None

    if event.get("type") != "content_block_delta":
        return None

    delta = event.get("delta")
    if not isinstance(delta, dict):
        return None

    if delta.get("type") != "text_delta":
        return None

    text = delta.get("text")
    if isinstance(text, str):
        return text

    return None


@pure
def _is_result_event(line: str) -> bool:
    """Check if a stream-json line is a result event (signals completion)."""
    try:
        parsed = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        # Expected: non-JSON lines in the stream (see extract_text_delta).
        return False
    return parsed.get("type") == "result"


@pure
def _extract_result_error(line: str) -> str | None:
    """Extract error text from a stream-json result event with is_error=true.

    Returns the error message if this is an error result, None otherwise.
    """
    try:
        parsed = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        # Expected: non-JSON lines in the stream (see extract_text_delta).
        return None
    if parsed.get("type") == "result" and parsed.get("is_error"):
        return parsed.get("result", "unknown error")
    return None


def _yield_text_deltas_from_lines(lines: list[str]) -> Iterator[str]:
    """Yield text deltas parsed from stream-json lines, skipping blanks and non-delta events."""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        text = extract_text_delta(stripped)
        if text is not None:
            yield text


class _StreamTailState(MutableModel):
    """Encapsulates mutable state for tailing a file via the host interface.

    Separated from HeadlessClaude to avoid lambda-in-loop closure issues (B023)
    when polling for file changes.
    """

    stdout_path: Path
    host: OnlineHostInterface
    is_finished: Callable[[], bool]
    last_mtime: datetime | None = None
    chars_consumed: int = 0
    line_buffer: str = ""
    result_error: str | None = None

    def _has_new_data_or_finished(self) -> bool:
        current_mtime = self.host.get_file_mtime(self.stdout_path)
        if current_mtime is not None and current_mtime != self.last_mtime:
            return True
        return self.is_finished()

    def tail_until_done(self) -> Iterator[str]:
        got_result = False
        while not got_result and not self.is_finished():
            poll_until(
                self._has_new_data_or_finished,
                timeout=_TAIL_POLL_TIMEOUT,
                poll_interval=_TAIL_POLL_INTERVAL,
            )
            self.last_mtime = self.host.get_file_mtime(self.stdout_path)

            try:
                content = self.host.read_text_file(self.stdout_path)
            except FileNotFoundError:
                continue

            raw = content[self.chars_consumed :]
            self.chars_consumed = len(content)

            if raw:
                combined = self.line_buffer + raw
                self.line_buffer = ""

                lines = combined.split("\n")
                if not combined.endswith("\n"):
                    self.line_buffer = lines.pop()

                for line in lines:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if _is_result_event(stripped):
                        self.result_error = _extract_result_error(stripped)
                        got_result = True
                        break
                    text = extract_text_delta(stripped)
                    if text is not None:
                        yield text

        if not got_result:
            # Final drain after agent exits
            try:
                content = self.host.read_text_file(self.stdout_path)
            except FileNotFoundError:
                return
            remaining = self.line_buffer + content[self.chars_consumed :]
            if remaining:
                for line in remaining.split("\n"):
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if _is_result_event(stripped):
                        self.result_error = _extract_result_error(stripped)
                        break
                    text = extract_text_delta(stripped)
                    if text is not None:
                        yield text


class NoPermissionsClaudeAgent(ClaudeAgent, NoPermissionsAgentMixin):
    """ClaudeAgent with no permissions granted (no tools, no trust needed).

    Skips trust validation and dialog dismissal during provisioning since
    the agent cannot perform any actions that require permissions. All other
    provisioning (config dir setup, installation, hooks) runs normally.
    """

    def on_before_provisioning(
        self,
        host: OnlineHostInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """No-op: skip trust/dialog validation for no-permissions agents."""

    def _ensure_no_blocking_dialogs(self, source_path: Path | None, mngr_ctx: MngrContext) -> None:
        """No-op: no permissions means no dialogs to check."""


class HeadlessClaudeAgentConfig(ClaudeAgentConfig):
    """Config for the headless_claude agent type.

    Disables sync_home_settings because headless agents are ephemeral and
    should not inherit user-level hooks (e.g. Stop hooks) from
    ~/.claude/settings.json.
    """

    sync_home_settings: bool = Field(
        default=False,
        description="Headless agents do not sync user settings from ~/.claude/ "
        "to avoid inheriting hooks (e.g. Stop hooks) that interfere with ephemeral operation.",
    )


class HeadlessClaude(NoPermissionsClaudeAgent, StreamingHeadlessAgentMixin):
    """Agent type for non-interactive (headless) Claude usage.

    Runs `claude --print` with stdout redirected to a file so callers can
    read output programmatically via stream_output(). Does not support
    interactive messages, paste detection, or TUI readiness checking.
    """

    def _preflight_send_message(self, tmux_target: str) -> None:
        """Headless agents do not accept interactive messages."""
        raise SendMessageError(
            str(self.name),
            "Headless claude agents do not accept interactive messages.",
        )

    def uses_paste_detection_send(self) -> bool:
        return False

    def get_tui_ready_indicator(self) -> str | None:
        return None

    def wait_for_ready_signal(
        self, is_creating: bool, start_action: Callable[[], None], timeout: float | None = None
    ) -> None:
        raise NotImplementedError(
            "HeadlessClaude agents do not support wait_for_ready_signal. "
            "The prompt is passed as a CLI arg, not via send_message."
        )

    def assemble_command(
        self,
        host: OnlineHostInterface,
        agent_args: tuple[str, ...],
        command_override: CommandString | None,
    ) -> CommandString:
        """Build a simplified command for headless operation.

        Always includes --print, no session resumption, no background activity
        tracking. Redirects stdout to $MNGR_AGENT_STATE_DIR/stdout.jsonl and
        stderr to $MNGR_AGENT_STATE_DIR/stderr.log.
        """
        if command_override is not None:
            base = str(command_override)
        elif self.agent_config.command is not None:
            base = str(self.agent_config.command)
        else:
            raise NoCommandDefinedError(f"No command defined for agent type '{self.agent_type}'")

        parts = [base, "--print"]

        all_extra_args = self.agent_config.cli_args + agent_args
        if all_extra_args:
            parts.extend(all_extra_args)

        cmd_str = " ".join(parts)
        return CommandString(f'{cmd_str} > "$MNGR_AGENT_STATE_DIR/stdout.jsonl" 2> "$MNGR_AGENT_STATE_DIR/stderr.log"')

    def _get_stdout_path(self) -> Path:
        """Return the path to the stdout.jsonl file for this agent."""
        return self._get_agent_dir() / "stdout.jsonl"

    def _get_stderr_path(self) -> Path:
        """Return the path to the stderr.log file for this agent."""
        return self._get_agent_dir() / "stderr.log"

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
        """
        poll_until(
            lambda: self._file_exists_on_host(stdout_path) or self._is_agent_finished(),
            timeout=_TAIL_POLL_TIMEOUT,
            poll_interval=_TAIL_POLL_INTERVAL,
        )
        return self._file_exists_on_host(stdout_path)

    def output(self) -> str:
        """Wait for the agent to finish and return its complete output."""
        return "".join(self.stream_output())

    def _raise_no_output_error(self) -> Never:
        """Raise MngrError collecting all available error detail.

        Collects errors from all file-based sources (stderr.log and stdout.jsonl)
        since they can contain complementary information (e.g. stderr has a stack
        trace while stdout has the user-facing error message). Falls back to tmux
        pane capture only when neither redirect file exists (the shell never ran).

        Error sources:

        - stderr.log: claude CLI crashes (unhandled promise rejections) write
          stack traces here. Claude's stderr behavior is unreliable (some errors
          leak to stdout instead), so this rarely fires in practice.

        - stdout.jsonl stream-json error result: the primary error channel.
          With --output-format stream-json --verbose, auth failures and API
          errors appear as result events with is_error=true.

        - tmux pane capture: last resort when neither redirect file exists,
          meaning the shell itself failed before running the command. Extremely
          unlikely since the shell redirect creates both files before executing.
        """
        parts: list[str] = []
        # stderr: crashes, stack traces, tool errors
        stderr_error = self._get_stderr_error_message()
        if stderr_error:
            parts.append(stderr_error)
        # stdout: stream-json result events with is_error=true
        stdout_error = self._get_stdout_stream_json_error()
        if stdout_error:
            parts.append(stdout_error)
        # pane capture: only if the shell never created the redirect files
        if not parts:
            stderr_exists = self._file_exists_on_host(self._get_stderr_path())
            stdout_exists = self._file_exists_on_host(self._get_stdout_path())
            if not stderr_exists and not stdout_exists:
                pane_error = self._get_pane_error_message()
                if pane_error:
                    parts.append(pane_error)
        if parts:
            detail = "\n".join(parts)
            raise MngrError(f"claude exited without producing output:\n{detail}")
        raise MngrError("claude exited without producing output (no details available)")

    def _get_stderr_error_message(self) -> str | None:
        """Read stderr.log for error output from the claude process.

        Relevant when: claude crashes with an unhandled exception (the Node.js
        runtime writes the stack trace to stderr), or other tools in the
        shell pipeline write errors to stderr.
        """
        stderr_path = self._get_stderr_path()
        try:
            content = self.host.read_text_file(stderr_path)
        except FileNotFoundError:
            return None
        stripped = content.strip()
        return stripped if stripped else None

    def _get_stdout_stream_json_error(self) -> str | None:
        """Extract error message from a stream-json result event in stdout.jsonl.

        Relevant when: claude starts successfully but encounters an error
        (auth failure, API error, rate limit). With --output-format stream-json
        --verbose, these appear as {"type": "result", "is_error": true,
        "result": "Not logged in"} events in the stdout stream. This is the
        primary error channel for headless claude.
        """
        stdout_path = self._get_stdout_path()
        try:
            content = self.host.read_text_file(stdout_path)
        except FileNotFoundError:
            return None
        for line in content.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if parsed.get("type") == "result" and parsed.get("is_error"):
                    return parsed.get("result", "unknown error")
            except (json.JSONDecodeError, ValueError):
                # Non-JSON in stdout.jsonl after an error -- could indicate
                # debug output leaking to stdout (known claude CLI bug).
                logger.debug("Non-JSON line in stdout.jsonl during error recovery: {}", line)
                continue
        return None

    def _get_pane_error_message(self) -> str | None:
        """Capture the tmux pane content to extract an error message.

        Relevant when: the shell command never ran far enough to create the
        redirect files (stderr.log, stdout.jsonl). This would happen if tmux
        send-keys failed to reach a shell prompt, or the shell itself crashed
        before executing the redirect. Extremely unlikely in practice --
        mostly defense-in-depth.
        """
        content = self.capture_pane_content()
        if content is None:
            return None
        stripped = content.strip()
        return stripped if stripped else None

    def stream_output(self) -> Iterator[str]:
        """Stream text output as it becomes available.

        Tails $MNGR_AGENT_STATE_DIR/stdout.jsonl via the host interface so it
        works for both local and remote hosts. Uses mtime checks via
        host.get_file_mtime() (through poll_until) to detect new data instead
        of busy-polling with time.sleep.

        Yields text delta chunks parsed from stream-json events. Completes when
        the agent process exits and the file is fully consumed.

        Raises MngrError if the stream-json result event has is_error=true
        (even if some text was yielded before the error), or if the agent exits
        without producing any output (startup failure, auth error, etc.).
        """
        stdout_path = self._get_stdout_path()

        if not self._wait_for_stdout_file(stdout_path):
            self._raise_no_output_error()

        state = _StreamTailState(
            stdout_path=stdout_path,
            host=self.host,
            is_finished=self._is_agent_finished,
        )
        yielded_any = False
        for chunk in state.tail_until_done():
            yielded_any = True
            yield chunk

        # After streaming completes, check for errors. result_error is
        # captured from the stream-json result event during tailing (covers
        # both "error with no output" and "error after partial output").
        # Also check stderr since it may have complementary info (e.g. a
        # stack trace alongside the user-facing error in the result event).
        if state.result_error:
            parts = [state.result_error]
            stderr_error = self._get_stderr_error_message()
            if stderr_error:
                parts.append(stderr_error)
            detail = "\n".join(parts)
            raise MngrError(f"claude returned an error:\n{detail}")
        # If nothing was yielded and no result error was captured, fall back
        # to the full error chain (re-reads stdout for errors, checks pane).
        if not yielded_any:
            self._raise_no_output_error()


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the headless_claude agent type."""
    return ("headless_claude", HeadlessClaude, HeadlessClaudeAgentConfig)
