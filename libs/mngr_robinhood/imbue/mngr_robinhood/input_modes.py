import json
from collections.abc import Iterator
from typing import Any
from typing import IO

from imbue.imbue_common.pure import pure
from imbue.mngr_robinhood.data_types import InputFormat
from imbue.mngr_robinhood.errors import InvalidStreamJsonInputError
from imbue.mngr_robinhood.errors import MissingPromptError


def iter_user_prompts(
    input_format: InputFormat,
    positional: str | None,
    stdin: IO[str],
    is_stdin_a_tty: bool,
) -> Iterator[str]:
    """Yield one user prompt at a time, depending on input format.

    For :attr:`InputFormat.TEXT`: yields exactly one prompt drawn from the
    positional arg (when given), falling back to all of stdin (when not a
    tty). Raises :class:`MissingPromptError` if neither is available.

    For :attr:`InputFormat.STREAM_JSON`: yields one prompt per stdin line.
    Each line must be a JSON object of the form
    ``{"type": "user", "message": {"role": "user", "content": <string>}}``;
    any other shape (content blocks, control_request, malformed JSON, etc.)
    raises :class:`InvalidStreamJsonInputError`. A non-empty positional
    prompt is rejected in this mode because every user turn must come from
    stdin as a JSON line; silently dropping it would hide a user error.
    """
    match input_format:
        case InputFormat.TEXT:
            yield _resolve_text_prompt(positional, stdin, is_stdin_a_tty)
        case InputFormat.STREAM_JSON:
            if positional is not None and positional != "":
                raise InvalidStreamJsonInputError(
                    "a positional prompt cannot be combined with --input-format=stream-json; "
                    "all user turns must be supplied as JSON lines on stdin"
                )
            yield from _iter_stream_json_prompts(stdin)


def _resolve_text_prompt(positional: str | None, stdin: IO[str], is_stdin_a_tty: bool) -> str:
    if positional is not None and positional != "":
        return positional
    if not is_stdin_a_tty:
        piped = stdin.read()
        if piped != "":
            return piped
    raise MissingPromptError("no prompt provided")


def _iter_stream_json_prompts(stdin: IO[str]) -> Iterator[str]:
    for raw_line in stdin:
        stripped = raw_line.rstrip("\n")
        if stripped == "":
            continue
        yield _parse_stream_json_user_line(stripped)


@pure
def _parse_stream_json_user_line(line: str) -> str:
    """Extract the user-text content from a single stream-json input line.

    Accepts only the simple shape:

        {"type": "user", "message": {"role": "user", "content": "<string>"}}

    Anything else (content blocks, control_request, malformed JSON) is rejected.
    """
    try:
        parsed: object = json.loads(line)
    except json.JSONDecodeError as exc:
        raise InvalidStreamJsonInputError(f"stream-json input line is not valid JSON: {line!r} ({exc.msg})") from exc

    if not isinstance(parsed, dict):
        raise InvalidStreamJsonInputError(
            f"stream-json input line must be a JSON object, got {type(parsed).__name__}: {line!r}"
        )

    parsed_dict: dict[str, Any] = {str(key): value for key, value in parsed.items()}
    return _extract_user_text_content(parsed_dict, line)


def _extract_user_text_content(parsed: dict[str, Any], line: str) -> str:
    type_value: Any = parsed.get("type")
    if type_value != "user":
        raise InvalidStreamJsonInputError(
            f'only {{"type": "user"}} stream-json input is supported in v1 (got type={type_value!r}): {line!r}'
        )

    message: Any = parsed.get("message")
    if not isinstance(message, dict):
        raise InvalidStreamJsonInputError(f"stream-json user line must have a 'message' object: {line!r}")

    message_dict: dict[str, Any] = {str(key): value for key, value in message.items()}
    role: Any = message_dict.get("role")
    if role != "user":
        raise InvalidStreamJsonInputError(
            f"stream-json user line must have message.role == 'user' (got {role!r}): {line!r}"
        )

    content: Any = message_dict.get("content")
    if not isinstance(content, str):
        raise InvalidStreamJsonInputError(
            "stream-json user line content must be a string in v1 "
            f"(content blocks / images / tool_result are not supported): {line!r}"
        )

    return content
