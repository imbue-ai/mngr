from __future__ import annotations

import shlex
import time
from collections.abc import Iterable
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Never

from loguru import logger
from pydantic import Field

from imbue.imbue_common.pure import pure
from imbue.mngr.agents.base_headless_agent import BaseHeadlessAgent
from imbue.mngr.agents.base_headless_agent import render_file_diagnostic
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.errors import MngrError
from imbue.mngr.errors import NoCommandDefinedError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.live_output import LiveOutputReader
from imbue.mngr.primitives import CommandString
from imbue.mngr_claude import hookimpl
from imbue.mngr_claude.plugin import ClaudeAgentConfig
from imbue.mngr_claude.plugin import ClaudeCoreAgent
from imbue.mngr_claude.stream_json import assistant_message_id
from imbue.mngr_claude.stream_json import assistant_text
from imbue.mngr_claude.stream_json import classify_stream_event
from imbue.mngr_claude.stream_json import decode_stream_line
from imbue.mngr_claude.stream_json import validate_stream_event

# Grace period before trusting lifecycle state. Claude can take several seconds
# to start (especially on first run or via nvm), during which the tmux pane shows
# bash as the current command, making the agent look DONE/STOPPED.
_STARTUP_GRACE_SECONDS: float = 10.0


@pure
def _result_error_from_parsed(parsed: dict[str, Any]) -> str | None:
    """Extract error text from an already-parsed stream-json result event.

    Returns the error message when `parsed` is a `result` event with
    `is_error=true`, None otherwise (including for non-`result` events).
    Falls back to "unknown error" when the `result` field is missing or
    not a string, so the declared `str | None` return type is honored
    even if claude emits a non-string `result` payload.
    """
    if parsed.get("type") == "result" and parsed.get("is_error"):
        result_value = parsed.get("result")
        if isinstance(result_value, str):
            return result_value
        return "unknown error"
    return None


@pure
def _extract_result_error(line: str) -> str | None:
    """Extract error text from a stream-json result event with is_error=true.

    Returns the error message if this is an error result, None otherwise.
    """
    parsed = decode_stream_line(line)
    if parsed is None:
        return None
    return _result_error_from_parsed(parsed)


class StreamJsonReader(LiveOutputReader):
    """Extracts assistant text deltas from claude ``--print`` stream-json output.

    The stdout file is append-only NDJSON. :meth:`feed` consumes newly-appended
    complete lines (holding any partial trailing line until more arrives) and
    returns the text they carry; :meth:`finalize` flushes a trailing line left
    at EOF without a newline. A stream-json ``result`` event ends the stream:
    it sets :attr:`stream_error` (when ``is_error``) and marks the reader
    :attr:`is_complete`, after which no further lines are consumed.
    """

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

    @property
    def is_complete(self) -> bool:
        return self.got_result

    @property
    def stream_error(self) -> str | None:
        return self.result_error

    def feed(self, content: str) -> list[str]:
        raw = content[self.chars_consumed :]
        self.chars_consumed = len(content)
        if not raw:
            return []
        combined = self.line_buffer + raw
        self.line_buffer = ""
        lines = combined.split("\n")
        if not combined.endswith("\n"):
            self.line_buffer = lines.pop()
        return list(self._yield_text_from_lines(lines))

    def finalize(self) -> list[str]:
        # A result event already ended the stream; the trailing partial line (if
        # any) is past it and not ours to emit, matching the original drain skip.
        if self.got_result:
            return []
        remaining = self.line_buffer
        self.line_buffer = ""
        if not remaining:
            return []
        return list(self._yield_text_from_lines([remaining]))

    def _reset_turn_state(self) -> None:
        self.streaming_message_id = None
        self.yielded_text_chunks = []

    def _yield_text_for_parsed(self, parsed: dict[str, Any]) -> Iterator[str]:
        # Dispatch an already-parsed stream-json line on its (CLI-level) `type`, then
        # delegate to the shared typed boundary in `stream_json` for the envelope
        # vocabulary. `stream_event` / `assistant` are claude-CLI wrappers; their inner
        # payloads are the Anthropic-API shapes that `stream_json` models.
        match parsed.get("type"):
            case "stream_event":
                yield from self._handle_stream_event(parsed)
            case "assistant":
                yield from self._handle_assistant_event(parsed)
            case other_event_type:
                # Other event types (system, user, ping, future event types,
                # etc.) carry no text to surface here and are intentionally
                # skipped. Trace-log for debugging when something looks off.
                logger.trace("Skipped stream-json event of type {!r} (no text to surface)", other_event_type)

    def _handle_stream_event(self, parsed: dict[str, Any]) -> Iterator[str]:
        event = validate_stream_event(parsed.get("event"))
        if event is None:
            # The inner payload was not a JSON object, or it matched no event variant this
            # `anthropic` package models (e.g. a CLI running ahead of our pinned package).
            # Nothing to surface; the static exhaustiveness check in `classify_stream_event`
            # is what flags new variants on a package bump.
            logger.trace("Skipped stream_event with no modeled inner event to surface")
            return
        info = classify_stream_event(event)

        # message_start (partial stream): begin a new turn. Any deltas for
        # the previous turn whose summary never arrived have already been
        # yielded directly, so dropping the buffer here is safe.
        if info.message_start_id is not None:
            self._reset_turn_state()
            self.streaming_message_id = info.message_start_id
            return

        # text_delta (partial stream): yield the delta and record it in the
        # per-turn buffer so we can subtract it from the matching summary.
        if info.delta_text is not None:
            self.yielded_text_chunks.append(info.delta_text)
            yield info.delta_text
            return

        # Other inner event types (content_block_start/stop, message_delta/stop) carry no
        # text to surface and are intentionally skipped.
        logger.trace("Skipped stream_event inner type {!r} (no text to surface)", event.type)

    def _handle_assistant_event(self, parsed: dict[str, Any]) -> Iterator[str]:
        # Top-level assistant event: reconcile against the per-turn buffer.
        # An assistant event always ends the current turn (it is the message
        # summary), so the per-turn state is reset unconditionally on exit --
        # even when the message has no text (e.g. tool_use-only, or the rare
        # case of a single empty text block) and no reconciliation is needed.
        # The truthiness check skips the empty-text case for free, matching
        # the `if trailing_text:` guard one branch deeper that prevents
        # yielding an empty string.
        raw_message = parsed.get("message")
        message = raw_message if isinstance(raw_message, dict) else None
        summary_text = assistant_text(message)
        if summary_text:
            assistant_id = assistant_message_id(message)
            is_definitely_different_message = (
                self.streaming_message_id is not None
                and assistant_id is not None
                and assistant_id != self.streaming_message_id
            )

            if is_definitely_different_message:
                # The streamed deltas belonged to a previous message whose summary
                # never arrived. Yield the full summary for this new message; the
                # per-turn buffer is irrelevant here so we don't bother joining it.
                yield summary_text
            else:
                # Materialize the per-turn buffer once, here, instead of after every
                # delta -- this turns an O(N*M) per-turn cost into O(M).
                yielded_so_far = "".join(self.yielded_text_chunks)
                if summary_text.startswith(yielded_so_far):
                    # Summary continues / matches what we already yielded; emit only
                    # the trailing extra text (empty string when they match exactly).
                    trailing_text = summary_text[len(yielded_so_far) :]
                    if trailing_text:
                        yield trailing_text
                else:
                    # Buffer is not a prefix of the summary. Either deltas drifted from
                    # the summary or this is a different message we cannot disambiguate
                    # by id. Yield the full summary; better a possible partial double-
                    # emit than dropping the assistant message entirely.
                    yield summary_text

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
            parsed = decode_stream_line(stripped)
            if parsed is None:
                # Non-JSON output that claude leaked to stdout (debug, banners,
                # warnings) or, more rarely, valid JSON that isn't an object.
                # Truncate so a runaway line cannot blow up the log.
                logger.trace("Skipped stream-json line that did not decode to a JSON object: {!r}", stripped[:200])
                continue
            if parsed.get("type") == "result":
                self.result_error = _result_error_from_parsed(parsed)
                self.got_result = True
                return
            yield from self._yield_text_for_parsed(parsed)


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


_MNGR_PROMPT_FILE: str = ".mngr-prompt"
# Canonical form of the "read the staged prompt" arg. Written by
# stage_initial_message under $MNGR_AGENT_STATE_DIR so it is cleaned up
# when the agent is destroyed.
_MNGR_PROMPT_CAT_ARG: str = f'"$(cat "$MNGR_AGENT_STATE_DIR/{_MNGR_PROMPT_FILE}")"'


class HeadlessClaude(ClaudeCoreAgent, BaseHeadlessAgent[ClaudeAgentConfig]):
    """Agent type for non-interactive (headless) Claude usage.

    Runs `claude --print` with stdout redirected to a file so callers can
    read output programmatically via stream_output(). Does not support
    interactive messages, paste detection, or TUI readiness checking.
    """

    _no_output_error_subject: str = "claude"
    _startup_grace_seconds: float = _STARTUP_GRACE_SECONDS

    def is_unattended_enabled(self) -> bool:
        # Diamond resolution (HeadlessClaude(ClaudeCoreAgent, BaseHeadlessAgent)): both bases
        # define this -- ClaudeCoreAgent config-driven (auto_allow_permissions), BaseHeadlessAgent
        # always True. Keep ClaudeCoreAgent's config-driven behavior so the auto-allow hook is
        # gated exactly as before the split. The MRO already resolves here; the explicit override
        # makes the choice deliberate (see test_headless_claude_resolves_all_shared_method_conflicts).
        return ClaudeCoreAgent.is_unattended_enabled(self)

    def stage_initial_message(self, initial_message: str) -> None:
        """Persist ``initial_message`` to ``.mngr-prompt`` inside the agent's state dir.

        The command assembled by ``assemble_command`` reads this file via
        ``cat`` so we can pass very long prompts without hitting tmux /
        shell arg length limits. Writing to the state dir (rather than the
        work dir) means the file is cleaned up when the agent is destroyed
        and does not leak into an in-place source directory.
        """
        prompt_path = self._get_agent_dir() / _MNGR_PROMPT_FILE
        self.host.write_text_file(prompt_path, initial_message)

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
        initial_message: str | None = None,
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

        # cli_args reach here already shell-safe: string-form configs go through split_cli_args_string
        # (non-POSIX shlex that preserves quote chars in tokens). agent_args, by contrast, are raw
        # argv strings passed through Click as click.UNPROCESSED -- the OS shell stripped quote chars
        # when it built argv at invocation time, so we must re-quote each element before splicing it
        # into a shell command string.
        quoted_agent_args = tuple(shlex.quote(arg) for arg in agent_args)
        all_extra_args = self.agent_config.cli_args + quoted_agent_args
        if all_extra_args:
            parts.extend(all_extra_args)

        # When the caller supplied --message (or --message-file),
        # stage_initial_message writes the prompt to
        # $MNGR_AGENT_STATE_DIR/.mngr-prompt. Append a cat reference so
        # claude reads it on startup.
        #
        # ``initial_message`` is passed in by ``Host.create_agent_state``
        # from ``CreateAgentOptions.initial_message``. We deliberately do
        # NOT read ``self.get_initial_message()`` here: ``assemble_command``
        # runs inside ``create_agent_state`` *before* ``data.json`` is
        # written, so the persisted initial_message is not yet visible via
        # ``_read_data``.
        #
        # The "already referenced" check is an exact-equality membership
        # test against the *unquoted* inputs (``agent_args`` and
        # ``cli_args``), not a substring scan of the joined args: a
        # substring scan would falsely match any arg containing
        # `.mngr-prompt` (e.g. an unrelated path) and silently drop the
        # prompt. We cannot test against ``all_extra_args`` because each
        # element of ``quoted_agent_args`` has been wrapped by
        # ``shlex.quote`` and so will not compare equal to the canonical
        # ``_MNGR_PROMPT_CAT_ARG`` literal.
        already_referenced = _MNGR_PROMPT_CAT_ARG in agent_args or _MNGR_PROMPT_CAT_ARG in self.agent_config.cli_args
        if initial_message is not None and not already_referenced:
            parts.append(_MNGR_PROMPT_CAT_ARG)

        cmd_str = " ".join(parts)
        return CommandString(f'{cmd_str} > "$MNGR_AGENT_STATE_DIR/stdout.jsonl" 2> "$MNGR_AGENT_STATE_DIR/stderr.log"')

    def _get_stdout_path(self) -> Path:
        """Return the path to the stdout.jsonl file for this agent."""
        return self._get_agent_dir() / "stdout.jsonl"

    def _get_stderr_path(self) -> Path:
        """Return the path to the stderr.log file for this agent."""
        return self._get_agent_dir() / "stderr.log"

    def _get_extra_error_sources(self) -> list[str]:
        """Return the stream-json stdout error (if any) and the work-dir diagnostic.

        The work-dir diagnostic is always appended -- it's cheap to compute
        and most valuable for silent-exit post-mortems (e.g. the
        test_ask_simple_query failure mode, where stdout/stderr are both
        empty because the stream-json error check can't find a result event).
        Listing the .mngr-prompt / .mngr-system-prompt files that the command
        substitution reads helps distinguish "claude never ran because its
        prompt inputs were empty/missing" from "claude ran but produced no
        output." When a stream-json error *is* present, the work-dir
        diagnostic still provides useful triage context alongside it.
        """
        sources: list[str] = []
        stdout_error = self._get_stdout_stream_json_error()
        if stdout_error:
            sources.append(stdout_error)
        sources.append(f"[work-dir]\n{self._get_work_dir_diagnostic()}")
        return sources

    def _get_work_dir_diagnostic(self) -> str:
        """Summarize the agent's work dir for silent-exit post-mortems.

        Lists the .mngr-prompt and .mngr-system-prompt files by existence +
        char count. Delegates per-file rendering to
        :func:`render_file_diagnostic` so the format stays in lockstep with
        BaseHeadlessAgent's state-dir diagnostic.
        """
        work_dir = self.work_dir
        lines: list[str] = [f"work_dir: {work_dir}"]
        for name in (".mngr-prompt", ".mngr-system-prompt"):
            # show_path=False: the `work_dir:` line already reports the
            # directory, so per-file lines only need the filename label.
            lines.append(render_file_diagnostic(self.host, work_dir / name, f"  {name}", show_path=False))
        return "\n".join(lines)

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

    def make_live_output_reader(self) -> LiveOutputReader:
        """Parse the captured stream-json stdout into assistant text deltas."""
        return StreamJsonReader()

    def _make_live_output_finished_predicate(self) -> Callable[[], bool]:
        """The lifecycle check, gated by a startup grace period.

        Claude can take several seconds to start (first run, nvm resolution),
        during which the tmux pane shows the shell as the current command and
        lifecycle reads DONE/STOPPED. Until the grace deadline, treat the agent
        as finished only once it has *also* produced some stdout; otherwise the
        tail loop would exit and raise "no output" before claude's output lands.
        """
        startup_deadline = time.monotonic() + self._startup_grace_seconds
        return lambda: self._is_finished_after_grace(startup_deadline)

    def _is_finished_after_grace(self, startup_deadline: float) -> bool:
        if time.monotonic() < startup_deadline:
            try:
                return self._is_agent_finished() and self.host.read_text_file(self._get_stdout_path()) != ""
            except FileNotFoundError:
                return False
        return self._is_agent_finished()

    def _raise_stream_error(self, error: str) -> Never:
        """Surface a stream-json result error, appending stderr context when present."""
        parts = [error]
        stderr_error = self._get_stderr_error_message()
        if stderr_error:
            parts.append(stderr_error)
        detail = "\n".join(parts)
        raise MngrError(f"claude returned an error:\n{detail}")


@hookimpl
def register_agent_type() -> tuple[str, type[AgentInterface] | None, type[AgentTypeConfig]]:
    """Register the headless_claude agent type."""
    return ("headless_claude", HeadlessClaude, HeadlessClaudeAgentConfig)
