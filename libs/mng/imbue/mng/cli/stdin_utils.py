import sys
from typing import TextIO

from imbue.mng.errors import UserInputError

STDIN_PLACEHOLDER = "-"


def _read_identifiers_from_stream(stream: TextIO) -> list[str]:
    """Read identifiers from a text stream, one per line.

    Strips whitespace from each line and skips empty lines.
    """
    identifiers: list[str] = []
    for line in stream:
        stripped = line.strip()
        if stripped:
            identifiers.append(stripped)
    return identifiers


def expand_stdin_placeholder(identifiers: tuple[str, ...], stdin: TextIO | None = None) -> list[str]:
    """Expand the '-' stdin placeholder in a sequence of identifiers.

    If '-' appears in identifiers, it is replaced with newline-separated
    values read from stdin. Other identifiers are preserved as-is.

    Raises UserInputError if '-' appears more than once, or if '-' is
    specified but stdin is a TTY (no piped input).
    """
    dash_count = identifiers.count(STDIN_PLACEHOLDER)
    if dash_count == 0:
        return list(identifiers)
    if dash_count > 1:
        raise UserInputError("'-' can only be specified once (stdin can only be consumed once)")
    stream = stdin if stdin is not None else sys.stdin
    if stream.isatty():
        raise UserInputError("'-' requires piped input (stdin is a TTY)")

    stdin_values = _read_identifiers_from_stream(stream)
    result: list[str] = []
    for identifier in identifiers:
        if identifier == STDIN_PLACEHOLDER:
            result.extend(stdin_values)
        else:
            result.append(identifier)
    return result
