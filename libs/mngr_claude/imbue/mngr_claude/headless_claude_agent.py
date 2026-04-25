from __future__ import annotations

import json
from collections.abc import Iterable
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Callable

from loguru import logger
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.mngr.agents.base_headless_agent import BaseHeadlessAgent
from imbue.mngr.agents.base_headless_agent import TAIL_POLL_INTERVAL
from imbue.mngr.agents.base_headless_agent import TAIL_POLL_TIMEOUT
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import NoCommandDefinedError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.agent import NoPermissionsAgentMixin
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import CommandString
from imbue.mngr.utils.polling import poll_until
from imbue.mngr_claude import hookimpl
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_claude.plugin import ClaudeAgentConfig

# Grace period before trusting lifecycle state. Claude can take several seconds
# to start (especially on first run or via nvm), during which the tmux pane shows
# bash as the current command, making the agent look DONE/STOPPED.
_STARTUP_GRACE_SECONDS: float = 10.0


@pure
def _parse_stream_line(line: str) -> dict[str, Any] | None:
    """Decode a single stream-json line into a dict.

    Returns the parsed dict on success, or None if the line is not valid
    JSON or does not decode to a JSON object. Non-JSON lines (blank lines,
    debug output that claude sometimes leaks to stdout) are expected and
    silently skipped.
    """
    try:
        parsed = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


@pure
def _extract_text_delta_from_parsed(parsed: dict[str, Any]) -> str | None:
    """Extract text from an already-parsed stream-json content_block_delta event."""
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
def extract_text_delta(line: str) -> str | None:
    """Extract text from a stream-json content_block_delta event.

    Returns the delta text if the line is a content_block_delta with a text_delta,
    or None otherwise. This handles the partial-message envelope emitted by
    `claude --output-format stream-json --include-partial-messages`.
    """
    parsed = _parse_stream_line(line)
    if parsed is None:
        return None
    return _extract_text_delta_from_parsed(parsed)


@pure
def _extract_assistant_text_from_parsed(parsed: dict[str, Any]) -> str | None:
    """Extract concatenated text from an already-parsed top-level `assistant` event."""
    if parsed.get("type") != "assistant":
        return None

    message = parsed.get("message")
    if not isinstance(message, dict):
        return None

    content_blocks = message.get("content")
    if not isinstance(content_blocks, list):
        return None

    text_parts: list[str] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            text_parts.append(text)

    if not text_parts:
        return None
    return "".join(text_parts)


@pure
def extract_assistant_text(line: str) -> str | None:
    """Extract concatenated text from a top-level `assistant` event's content blocks.

    Without `--include-partial-messages`, `claude --output-format stream-json`
    emits one `{"type":"assistant","message":{"content":[{"type":"text","text":...},...]}}`
    line per assistant turn. Returns the concatenation of all text blocks, or
    None if the line is not an `assistant` event with at least one text block.
    """
    parsed = _parse_stream_line(line)
    if parsed is None:
        return None
    return _extract_assistant_text_from_parsed(parsed)


@pure
def _extract_assistant_message_id_from_parsed(parsed: dict[str, Any]) -> str | None:
    """Extract `message.id` from an already-parsed top-level `assistant` event."""
    if parsed.get("type") != "assistant":
        return None
    message = parsed.get("message")
    if not isinstance(message, dict):
        return None
    message_id = message.get("id")
    if isinstance(message_id, str):
        return message_id
    return None


@pure
def extract_assistant_message_id(line: str) -> str | None:
    """Extract `message.id` from a top-level `assistant` event, if present."""
    parsed = _parse_stream_line(line)
    if parsed is None:
        return None
    return _extract_assistant_message_id_from_parsed(parsed)


@pure
def _extract_message_start_id_from_parsed(parsed: dict[str, Any]) -> str | None:
    """Extract `message.id` from an already-parsed partial-stream `message_start` event."""
    if parsed.get("type") != "stream_event":
        return None
    event = parsed.get("event")
    if not isinstance(event, dict):
        return None
    if event.get("type") != "message_start":
        return None
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    message_id = message.get("id")
    if isinstance(message_id, str):
        return message_id
    return None


@pure
def extract_message_start_id(line: str) -> str | None:
    """Extract `message.id` from a partial-stream `message_start` event, if present.

    With `--include-partial-messages`, claude emits a
    `{"type":"stream_event","event":{"type":"message_start","message":{"id":"...",...}}}`
    line at the start of each assistant message. The id lets us correlate
    subsequent text deltas with a later top-level `assistant` summary that
    carries the same id.
    """
    parsed = _parse_stream_line(line)
    if parsed is None:
        return None
    return _extract_message_start_id_from_parsed(parsed)


@pure
def _result_error_from_parsed(parsed: dict[str, Any]) -> str | None:
    """Extract error text from an already-parsed stream-json result event.

    Returns the error message when `parsed` is a `result` event with
    `is_error=true`, None otherwise (including for non-`result` events).
    """
    if parsed.get("type") == "result" and parsed.get("is_error"):
        return parsed.get("result", "unknown error")
    return None


@pure
def _extract_result_error(line: str) -> str | None:
    """Extract error text from a stream-json result event with is_error=true.

    Returns the error message if this is an error result, None otherwise.
    """
    parsed = _parse_stream_line(line)
    if parsed is None:
        return None
    return _result_error_from_parsed(parsed)


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
    # Set to True once a stream-json `result` event has been seen. Once set,
    # the tail loop stops; further lines (typically there are none) are not
    # consumed.
    got_result: bool = False
    # Id of the assistant message currently being streamed via partial deltas
    # (from `--include-partial-messages`'s `message_start` event), if any.
    # Used to correlate deltas with the later top-level `assistant` summary
    # that carries the same id. None when no partial-stream context is active.
    streaming_message_id: str | None = None
    # Chunks of text already yielded for the in-progress turn, in order. Used
    # to compute the trailing diff when the `assistant` summary arrives, so
    # that text present in the summary but not in the deltas is still emitted
    # without re-emitting text already streamed. Stored as a list (and joined
    # lazily on summary arrival) to avoid O(N*M) repeated concatenation when
    # a turn contains many small deltas.
    yielded_text_chunks: list[str] = Field(default_factory=list)

    def _has_new_data_or_finished(self) -> bool:
        current_mtime = self.host.get_file_mtime(self.stdout_path)
        if current_mtime is not None and current_mtime != self.last_mtime:
            return True
        return self.is_finished()

    def _reset_turn_state(self) -> None:
        self.streaming_message_id = None
        self.yielded_text_chunks = []

    def _yield_text_for_parsed(self, parsed: dict[str, Any]) -> Iterator[str]:
        # Dispatch an already-parsed stream-json line on its `type`, then
        # delegate to the module-level extract helpers (their dict-accepting
        # variants) so dict-walking logic lives in one place. The string-
        # accepting public helpers are thin wrappers around the same dict logic.
        match parsed.get("type"):
            case "stream_event":
                yield from self._handle_stream_event(parsed)
            case "assistant":
                yield from self._handle_assistant_event(parsed)
            case other_event_type:
                # Other event types (system, user, ping, future event types,
                # etc.) carry no text to surface here and are intentionally
                # skipped. Trace-log for debugging when something looks off.
                logger.trace("Skipped stream-json event of unhandled type {!r}", other_event_type)

    def _handle_stream_event(self, parsed: dict[str, Any]) -> Iterator[str]:
        # message_start (partial stream): begin a new turn. Any deltas for
        # the previous turn whose summary never arrived have already been
        # yielded directly, so dropping the buffer here is safe.
        start_id = _extract_message_start_id_from_parsed(parsed)
        if start_id is not None:
            self._reset_turn_state()
            self.streaming_message_id = start_id
            return

        # text_delta (partial stream): yield the delta and record it in the
        # per-turn buffer so we can subtract it from the matching summary.
        delta_text = _extract_text_delta_from_parsed(parsed)
        if delta_text is not None:
            self.yielded_text_chunks.append(delta_text)
            yield delta_text

    def _handle_assistant_event(self, parsed: dict[str, Any]) -> Iterator[str]:
        # Top-level assistant event: reconcile against the per-turn buffer.
        # An assistant event always ends the current turn (it is the message
        # summary), so the per-turn state is reset unconditionally on exit --
        # even when the message has no text (e.g. tool_use-only, or the rare
        # case of a single empty text block) and no reconciliation is needed.
        # The truthiness check skips the empty-text case for free, matching
        # the `if trailing_text:` guard one branch deeper that prevents
        # yielding an empty string.
        assistant_text = _extract_assistant_text_from_parsed(parsed)
        if assistant_text:
            assistant_id = _extract_assistant_message_id_from_parsed(parsed)
            is_definitely_different_message = (
                self.streaming_message_id is not None
                and assistant_id is not None
                and assistant_id != self.streaming_message_id
            )

            if is_definitely_different_message:
                # The streamed deltas belonged to a previous message whose summary
                # never arrived. Yield the full summary for this new message; the
                # per-turn buffer is irrelevant here so we don't bother joining it.
                yield assistant_text
            else:
                # Materialize the per-turn buffer once, here, instead of after every
                # delta -- this turns an O(N*M) per-turn cost into O(M).
                yielded_so_far = "".join(self.yielded_text_chunks)
                if assistant_text.startswith(yielded_so_far):
                    # Summary continues / matches what we already yielded; emit only
                    # the trailing extra text (empty string when they match exactly).
                    trailing_text = assistant_text[len(yielded_so_far) :]
                    if trailing_text:
                        yield trailing_text
                else:
                    # Buffer is not a prefix of the summary. Either deltas drifted from
                    # the summary or this is a different message we cannot disambiguate
                    # by id. Yield the full summary; better a possible partial double-
                    # emit than dropping the assistant message entirely.
                    yield assistant_text

        self._reset_turn_state()

    def _yield_text_from_lines(self, lines: Iterable[str]) -> Iterator[str]:
        """Process already-split stream-json lines, yielding text deltas.

        Skips blank/non-JSON lines, records `result_error` and sets
        `got_result` when a `result` event is seen (then stops iterating;
        any lines after a result event are not consumed). Other events are
        dispatched through `_yield_text_for_parsed` which yields any text.
        """
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            parsed = _parse_stream_line(stripped)
            if parsed is None:
                continue
            if parsed.get("type") == "result":
                self.result_error = _result_error_from_parsed(parsed)
                self.got_result = True
                return
            yield from self._yield_text_for_parsed(parsed)

    def tail_until_done(self) -> Iterator[str]:
        while not self.got_result and not self.is_finished():
            poll_until(
                self._has_new_data_or_finished,
                timeout=TAIL_POLL_TIMEOUT,
                poll_interval=TAIL_POLL_INTERVAL,
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

                yield from self._yield_text_from_lines(lines)

        if not self.got_result:
            # Final drain after agent exits
            try:
                content = self.host.read_text_file(self.stdout_path)
            except FileNotFoundError:
                return
            remaining = self.line_buffer + content[self.chars_consumed :]
            if remaining:
                yield from self._yield_text_from_lines(remaining.split("\n"))


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

    def interactively_dismiss_claude_dialogs(self, source_path: Path | None, mngr_ctx: MngrContext) -> None:
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


class HeadlessClaude(NoPermissionsClaudeAgent, BaseHeadlessAgent[ClaudeAgentConfig]):
    """Agent type for non-interactive (headless) Claude usage.

    Runs `claude --print` with stdout redirected to a file so callers can
    read output programmatically via stream_output(). Does not support
    interactive messages, paste detection, or TUI readiness checking.
    """

    _no_output_error_subject: str = "claude"
    _startup_grace_seconds: float = _STARTUP_GRACE_SECONDS

    def _preflight_send_message(self, tmux_target: str) -> None:
        """Headless agents do not accept interactive messages.

        Must be defined here because ClaudeAgent overrides BaseAgent's no-op
        _preflight_send_message with dialog-checking logic. Without this
        explicit override, the MRO resolves to ClaudeAgent's implementation
        instead of BaseHeadlessAgent's, since ClaudeAgent appears earlier
        in HeadlessClaude's MRO.
        """
        BaseHeadlessAgent._preflight_send_message(self, tmux_target)

    def uses_paste_detection_send(self) -> bool:
        return BaseHeadlessAgent.uses_paste_detection_send(self)

    def get_tui_ready_indicator(self) -> str | None:
        return BaseHeadlessAgent.get_tui_ready_indicator(self)

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

    def _get_extra_error_sources(self) -> list[str]:
        """Check stdout.jsonl for stream-json error results."""
        stdout_error = self._get_stdout_stream_json_error()
        return [stdout_error] if stdout_error else []

    def _get_stdout_stream_json_error(self) -> str | None:
        """Extract error message from a stream-json result event in stdout.jsonl."""
        stdout_path = self._get_stdout_path()
        try:
            content = self.host.read_text_file(stdout_path)
        except FileNotFoundError:
            return None
        for line in content.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            error = _extract_result_error(stripped)
            if error is not None:
                return error
        return None

    def stream_output(self) -> Iterator[str]:
        """Stream text output as it becomes available.

        Tails $MNGR_AGENT_STATE_DIR/stdout.jsonl via the host interface so it
        works for both local and remote hosts. Yields text delta chunks parsed
        from stream-json events.

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
        is_any_output_yielded = False
        for chunk in state.tail_until_done():
            is_any_output_yielded = True
            yield chunk

        # After streaming completes, check for errors
        if state.result_error:
            parts = [state.result_error]
            stderr_error = self._get_stderr_error_message()
            if stderr_error:
                parts.append(stderr_error)
            detail = "\n".join(parts)
            raise MngrError(f"claude returned an error:\n{detail}")
        if not is_any_output_yielded:
            self._raise_no_output_error()


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the headless_claude agent type."""
    return ("headless_claude", HeadlessClaude, HeadlessClaudeAgentConfig)
