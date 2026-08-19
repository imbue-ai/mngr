"""Lazy, ``anthropic``-free facade for the Claude stream-json typed boundary.

Importing this module does NOT import the ``anthropic`` SDK. Registering the claude /
headless_claude agent types (and robinhood's stream emitters) imports this module
transitively at ``mngr`` startup, but the ~900-module ``anthropic`` SDK is only pulled once a
Claude stream is actually produced or consumed. The real, fully-typed implementation -- which
imports ``anthropic`` at top level and keeps the static exhaustiveness tripwire over the
stream-event union -- lives in :mod:`imbue.mngr_claude.stream_json_impl` and is imported lazily
on first call. Every function below forwards to it with identical runtime behavior. See MIND-179.

The four consume-side functions that return or accept ``anthropic`` types
(:func:`validate_stream_event`, :func:`classify_stream_event`, :func:`parse_stream_event`,
:func:`parse_assistant_message`) are annotated ``Any`` here, because naming an ``anthropic``
type in a signature would require importing the SDK at module load. Import them from
:mod:`imbue.mngr_claude.stream_json_impl` directly when the precise static types are wanted.
"""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final

# Mirror of stream_json_impl._DEFAULT_BLOCK_INDEX. Duplicated (not imported) so the forwarded
# signatures keep their default without importing the impl -- and thus anthropic -- at load time.
_DEFAULT_BLOCK_INDEX: Final[int] = 0


def _impl() -> Any:
    """Import and return the anthropic-backed implementation module (cached by ``sys.modules``)."""
    from imbue.mngr_claude import stream_json_impl

    return stream_json_impl


# ---------------------------------------------------------------------------
# Emit side -- construct via anthropic models, dump to the wire dict.
# ---------------------------------------------------------------------------


def text_delta_event(text: str, index: int = _DEFAULT_BLOCK_INDEX) -> dict[str, Any]:
    """Build a ``content_block_delta`` carrying a ``text_delta`` (the hot-path token event)."""
    return _impl().text_delta_event(text, index)


def message_start_event(message_id: str, model: str) -> dict[str, Any]:
    """Build the opening ``message_start`` event for a synthesized assistant message."""
    return _impl().message_start_event(message_id, model)


def content_block_start_event(index: int = _DEFAULT_BLOCK_INDEX) -> dict[str, Any]:
    """Build a ``content_block_start`` opening an (initially empty) text block."""
    return _impl().content_block_start_event(index)


def content_block_stop_event(index: int = _DEFAULT_BLOCK_INDEX) -> dict[str, Any]:
    """Build a ``content_block_stop`` closing a text block."""
    return _impl().content_block_stop_event(index)


def message_delta_event(stop_reason: str) -> dict[str, Any]:
    """Build a ``message_delta`` carrying the terminal ``stop_reason`` (zeroed usage stub)."""
    return _impl().message_delta_event(stop_reason)


def message_stop_event() -> dict[str, Any]:
    """Build a ``message_stop`` event."""
    return _impl().message_stop_event()


def wrap_stream_event(event: Mapping[str, Any], session_id: str) -> dict[str, Any]:
    """Wrap a raw inner ``event`` payload in the CLI's ``stream_event`` envelope."""
    return _impl().wrap_stream_event(event, session_id)


def build_assistant_message(
    *,
    message_id: str,
    model: str,
    content: Sequence[Mapping[str, Any]],
    stop_reason: str | None,
    usage: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the inner ``message`` of an ``assistant`` line, validated against ``anthropic.types.Message``."""
    return _impl().build_assistant_message(
        message_id=message_id, model=model, content=content, stop_reason=stop_reason, usage=usage
    )


# ---------------------------------------------------------------------------
# Consume side -- validate into the union, dispatch exhaustively.
# ---------------------------------------------------------------------------


def decode_stream_line(line: str) -> dict[str, Any] | None:
    """Decode a single stream-json line into a dict, or ``None`` if it is not a JSON object."""
    return _impl().decode_stream_line(line)


def validate_stream_event(payload: object) -> Any:
    """Validate a raw inner ``event`` payload against the anthropic stream-event union, or ``None``.

    Returns an ``anthropic.types.RawMessageStreamEvent`` (typed ``Any`` here); see
    :func:`imbue.mngr_claude.stream_json_impl.validate_stream_event` for the precise type.
    """
    return _impl().validate_stream_event(payload)


def classify_stream_event(event: Any) -> Any:
    """Dispatch a typed stream event to the text / message-start id mngr cares about.

    Returns a ``StreamEventText``; see
    :func:`imbue.mngr_claude.stream_json_impl.classify_stream_event`.
    """
    return _impl().classify_stream_event(event)


def parse_stream_event(line: str) -> Any:
    """Decode a full ``stream_event`` line and validate its inner event into the typed union, or ``None``.

    Returns an ``anthropic.types.RawMessageStreamEvent`` (typed ``Any`` here); see
    :func:`imbue.mngr_claude.stream_json_impl.parse_stream_event`.
    """
    return _impl().parse_stream_event(line)


def extract_text_delta(line: str) -> str | None:
    """Extract delta text from a ``stream_event`` / ``content_block_delta`` / ``text_delta`` line."""
    return _impl().extract_text_delta(line)


def extract_message_start_id(line: str) -> str | None:
    """Extract ``message.id`` from a ``stream_event`` / ``message_start`` line, if present."""
    return _impl().extract_message_start_id(line)


def parse_assistant_message(message: dict[str, Any] | None) -> Any:
    """Validate an ``assistant`` line's inner ``message`` against ``anthropic.types.Message``, or ``None``.

    Returns an ``anthropic.types.Message`` (typed ``Any`` here); see
    :func:`imbue.mngr_claude.stream_json_impl.parse_assistant_message`.
    """
    return _impl().parse_assistant_message(message)


def assistant_text(message: dict[str, Any] | None) -> str | None:
    """Concatenate the text of every text block in an ``assistant`` line's inner ``message``."""
    return _impl().assistant_text(message)


def assistant_message_id(message: dict[str, Any] | None) -> str | None:
    """Extract ``id`` from an ``assistant`` line's inner ``message`` (typed, with lenient fallback)."""
    return _impl().assistant_message_id(message)


def extract_assistant_text(line: str) -> str | None:
    """Extract concatenated text from a top-level ``assistant`` line's inner message."""
    return _impl().extract_assistant_text(line)


def extract_assistant_message_id(line: str) -> str | None:
    """Extract ``message.id`` from a top-level ``assistant`` line, if present."""
    return _impl().extract_assistant_message_id(line)
