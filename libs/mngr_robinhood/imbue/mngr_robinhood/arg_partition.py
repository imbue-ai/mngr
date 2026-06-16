from collections.abc import Mapping
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.mngr.primitives import TmuxHeight
from imbue.mngr.primitives import TmuxWidth
from imbue.mngr.primitives import TmuxWindowSize
from imbue.mngr_robinhood.data_types import ArgPartition
from imbue.mngr_robinhood.data_types import DEFAULT_ROBINHOOD_TMUX_HEIGHT
from imbue.mngr_robinhood.data_types import DEFAULT_ROBINHOOD_TMUX_WIDTH
from imbue.mngr_robinhood.data_types import DEFAULT_ROBINHOOD_TMUX_WINDOW_SIZE
from imbue.mngr_robinhood.data_types import InputFormat
from imbue.mngr_robinhood.data_types import OutputFormat
from imbue.mngr_robinhood.errors import UnsupportedClaudeFlagError

# Flags that look like top-level claude flags but in v1 we explicitly do not
# support. Value is the user-facing reason shown when one is encountered.
REJECTED_FLAGS: Final[Mapping[str, str]] = {
    "--fallback-model": "--fallback-model is not supported by mngr robinhood in v1",
    "--max-budget-usd": "--max-budget-usd is not supported by mngr robinhood in v1",
    "--no-session-persistence": "--no-session-persistence is not supported by mngr robinhood in v1",
    "--include-hook-events": "--include-hook-events is not supported by mngr robinhood in v1",
    "-c": "-c / --continue is not supported by mngr robinhood in v1",
    "--continue": "-c / --continue is not supported by mngr robinhood in v1",
    "-r": "-r / --resume is not supported by mngr robinhood in v1",
    "--resume": "-r / --resume is not supported by mngr robinhood in v1",
    "--session-id": "--session-id is not supported by mngr robinhood in v1",
}

# Simulated flags that take a separate value token (``--flag value`` form).
_SIMULATED_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "--input-format",
        "--output-format",
        # tmux window sizing for the spawned agent (consumed by the wrapper, not
        # forwarded to claude). Defaults are large + pinned so streamed text is not
        # hard-wrapped at a narrow pane width.
        "--tmux-width",
        "--tmux-height",
        "--tmux-window-size",
    }
)

# Simulated boolean flags (no value).
_SIMULATED_BOOL_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "-p",
        "--print",
        "--replay-user-messages",
        # Opt in to claude-native partial-message (text_delta) events sourced from
        # the agent's stream_buffer. Requires --output-format=stream-json.
        "--include-partial-messages",
        # Stream the assistant's text to stdout incrementally (text output mode).
        "--stream-plain-text",
    }
)

# Pass-through claude flags that consume the next argv token as their value.
# Used so that a positional prompt placed AFTER one of these (e.g.
# ``--model opus "hello"``) is correctly identified, rather than mistaking
# ``opus`` for the prompt. Flags not listed here are treated as bare booleans
# for the purpose of positional-prompt detection -- if a user wants to pass
# an unknown value flag, they should use the ``--flag=value`` form or ``--``.
_CLAUDE_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {
        "--add-dir",
        "--agent",
        "--agents",
        "--allowed-tools",
        "--allowedTools",
        "--append-system-prompt",
        "--append-system-prompt-file",
        "--betas",
        "--debug-file",
        "--disallowed-tools",
        "--disallowedTools",
        "--effort",
        "--file",
        "--json-schema",
        "--max-turns",
        "--mcp-config",
        "--model",
        "-n",
        "--name",
        "--permission-mode",
        "--permission-prompt-tool",
        "--plugin-dir",
        "--plugin-url",
        "--remote-control-session-name-prefix",
        "--setting-sources",
        "--settings",
        "--system-prompt",
        "--system-prompt-file",
        "--teammate-mode",
        "--tools",
    }
)

_INPUT_FORMAT_BY_TOKEN: Final[Mapping[str, InputFormat]] = {
    "text": InputFormat.TEXT,
    "stream-json": InputFormat.STREAM_JSON,
}

_OUTPUT_FORMAT_BY_TOKEN: Final[Mapping[str, OutputFormat]] = {
    "text": OutputFormat.TEXT,
    "json": OutputFormat.JSON,
    "stream-json": OutputFormat.STREAM_JSON,
}

_TMUX_WINDOW_SIZE_BY_TOKEN: Final[Mapping[str, TmuxWindowSize]] = {
    "manual": TmuxWindowSize.MANUAL,
    "latest": TmuxWindowSize.LATEST,
    "largest": TmuxWindowSize.LARGEST,
    "smallest": TmuxWindowSize.SMALLEST,
}


@pure
def _split_equals(token: str) -> tuple[str, str | None]:
    """Split a ``--flag=value`` token into (flag, value); returns (token, None) otherwise."""
    if token.startswith("--") and "=" in token:
        flag, value = token.split("=", 1)
        return flag, value
    return token, None


def _resolve_input_format(value: str) -> InputFormat:
    resolved = _INPUT_FORMAT_BY_TOKEN.get(value)
    if resolved is None:
        raise UnsupportedClaudeFlagError(f"--input-format must be 'text' or 'stream-json' (got {value!r})")
    return resolved


def _resolve_output_format(value: str) -> OutputFormat:
    resolved = _OUTPUT_FORMAT_BY_TOKEN.get(value)
    if resolved is None:
        raise UnsupportedClaudeFlagError(f"--output-format must be 'text', 'json', or 'stream-json' (got {value!r})")
    return resolved


def _resolve_tmux_width(value: str) -> TmuxWidth:
    try:
        return TmuxWidth(int(value))
    except ValueError:
        raise UnsupportedClaudeFlagError(f"--tmux-width must be a positive integer (got {value!r})") from None


def _resolve_tmux_height(value: str) -> TmuxHeight:
    try:
        return TmuxHeight(int(value))
    except ValueError:
        raise UnsupportedClaudeFlagError(f"--tmux-height must be a positive integer (got {value!r})") from None


def _resolve_tmux_window_size(value: str) -> TmuxWindowSize:
    resolved = _TMUX_WINDOW_SIZE_BY_TOKEN.get(value)
    if resolved is None:
        raise UnsupportedClaudeFlagError(
            f"--tmux-window-size must be 'manual', 'latest', 'largest', or 'smallest' (got {value!r})"
        )
    return resolved


def partition_args(argv: tuple[str, ...]) -> ArgPartition:
    """Split argv into our simulated flags, the positional prompt, and the pass-through tail.

    Walks ``argv`` left-to-right with a small state machine. Tokens whose flag
    matches one of ours (in either ``--flag=value`` or ``--flag value`` form)
    are consumed. Rejected flags raise :class:`UnsupportedClaudeFlagError` with
    the reason from :data:`REJECTED_FLAGS`. The first bare token (not starting
    with ``-`` and not the value of a recognized flag) becomes the positional
    prompt; everything else is passed through verbatim as agent args.
    """
    input_format = InputFormat.TEXT
    output_format = OutputFormat.TEXT
    replay_user_messages = False
    include_partial_messages = False
    stream_plain_text = False
    tmux_width = DEFAULT_ROBINHOOD_TMUX_WIDTH
    tmux_height = DEFAULT_ROBINHOOD_TMUX_HEIGHT
    tmux_window_size = DEFAULT_ROBINHOOD_TMUX_WINDOW_SIZE
    pass_through: list[str] = []
    positional_prompt: str | None = None

    index = 0
    while index < len(argv):
        token = argv[index]
        flag, inline_value = _split_equals(token)

        if flag in REJECTED_FLAGS:
            raise UnsupportedClaudeFlagError(REJECTED_FLAGS[flag])

        if flag in _SIMULATED_BOOL_FLAGS:
            if inline_value is not None:
                raise UnsupportedClaudeFlagError(f"{flag} does not take a value (got {inline_value!r})")
            if flag in {"-p", "--print"}:
                pass
            elif flag == "--replay-user-messages":
                replay_user_messages = True
            elif flag == "--include-partial-messages":
                include_partial_messages = True
            elif flag == "--stream-plain-text":
                stream_plain_text = True
            else:
                raise UnsupportedClaudeFlagError(f"unexpected simulated flag: {flag}")
            index += 1
            continue

        if flag in _SIMULATED_VALUE_FLAGS:
            if inline_value is not None:
                value = inline_value
                index += 1
            elif index + 1 < len(argv):
                value = argv[index + 1]
                index += 2
            else:
                raise UnsupportedClaudeFlagError(f"{flag} requires a value")
            if flag == "--input-format":
                input_format = _resolve_input_format(value)
            elif flag == "--output-format":
                output_format = _resolve_output_format(value)
            elif flag == "--tmux-width":
                tmux_width = _resolve_tmux_width(value)
            elif flag == "--tmux-height":
                tmux_height = _resolve_tmux_height(value)
            elif flag == "--tmux-window-size":
                tmux_window_size = _resolve_tmux_window_size(value)
            else:
                raise UnsupportedClaudeFlagError(f"unexpected simulated flag: {flag}")
            continue

        if token == "--":
            pass_through.extend(argv[index + 1 :])
            break

        if flag in _CLAUDE_VALUE_FLAGS:
            pass_through.append(token)
            if inline_value is None and index + 1 < len(argv):
                pass_through.append(argv[index + 1])
                index += 2
            else:
                index += 1
            continue

        if not token.startswith("-") and positional_prompt is None:
            positional_prompt = token
            index += 1
            continue

        pass_through.append(token)
        index += 1

    _validate_replay_user_messages(replay_user_messages, input_format, output_format)
    _validate_streaming_flags(include_partial_messages, stream_plain_text, output_format)

    return ArgPartition(
        input_format=input_format,
        output_format=output_format,
        replay_user_messages=replay_user_messages,
        include_partial_messages=include_partial_messages,
        stream_plain_text=stream_plain_text,
        tmux_width=tmux_width,
        tmux_height=tmux_height,
        tmux_window_size=tmux_window_size,
        pass_through_agent_args=tuple(pass_through),
        positional_prompt=positional_prompt,
    )


def _validate_replay_user_messages(
    replay_user_messages: bool,
    input_format: InputFormat,
    output_format: OutputFormat,
) -> None:
    if not replay_user_messages:
        return
    if input_format != InputFormat.STREAM_JSON or output_format != OutputFormat.STREAM_JSON:
        raise UnsupportedClaudeFlagError(
            "--replay-user-messages requires both --input-format=stream-json and --output-format=stream-json"
        )


def _validate_streaming_flags(
    include_partial_messages: bool,
    stream_plain_text: bool,
    output_format: OutputFormat,
) -> None:
    if include_partial_messages and output_format != OutputFormat.STREAM_JSON:
        raise UnsupportedClaudeFlagError("--include-partial-messages requires --output-format=stream-json")
    if stream_plain_text and output_format != OutputFormat.TEXT:
        raise UnsupportedClaudeFlagError("--stream-plain-text requires --output-format=text (the default)")
