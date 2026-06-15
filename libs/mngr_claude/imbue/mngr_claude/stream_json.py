"""Typed boundary for the Claude stream-json partial-message envelope.

The ``claude`` CLI's ``--output-format stream-json --include-partial-messages`` mode emits a
sequence of JSON lines. Some are the CLI's own wrapper constructs (``system``/``init``,
``assistant`` summaries, ``result``); those have no Anthropic-API counterpart and stay
hand-rolled at their call sites. The lines this module owns carry the *raw Anthropic API*
stream events inside a ``{"type": "stream_event", "event": <raw event>, "session_id": ...}``
wrapper -- the same shape ``claude_agent_sdk.StreamEvent`` documents (``event: dict[str, Any]``,
"the raw Anthropic API stream event").

We model those inner events against the ``anthropic`` SDK's discriminated union
``RawMessageStreamEvent`` (keyed on ``type``) plus ``anthropic.types.Message`` for the
assistant summary. Two payoffs:

- The emit side constructs the events via the SDK models and ``model_dump()``\\s them to the
  wire, so producers and the parser share one vocabulary instead of duplicating bare string
  literals (``"content_block_delta"``, ``"text_delta"``, ...). We dump the ``anthropic`` *Python*
  SDK models, whereas the real ``claude`` binary serializes the *TypeScript* SDK; the two have
  different optional-field sets, so our output is shape-compatible but not byte-identical (see
  "Known wire-shape departures" below). The hot path -- ``content_block_delta`` / ``text_delta`` --
  plus ``content_block_stop`` and ``message_stop`` are byte-identical to the real binary.
- The consume side validates into the union and dispatches with an exhaustive ``assert_never``
  match. When a future ``anthropic`` release adds an event variant, ``ty`` fails the
  exhaustiveness check and names the unhandled member -- an early warning our hand-written
  constants could never give.

The unavoidable caveat: the signal fires when we bump the ``anthropic`` *package*, which is
versioned independently of the ``claude`` CLI *binary* that emits the JSON at runtime. So the
runtime validation here degrades gracefully (an unmodeled variant validates to ``None`` and is
skipped, never raised) -- the static exhaustiveness check is the real new-variant tripwire.

Known wire-shape departures (measured against ``claude`` 2.1.177)
----------------------------------------------------------------
The Python and TypeScript SDKs carry different optional fields, so dumping the Python models does
not byte-match the real CLI:

- text blocks: the real binary emits ``{"type":"text","text":...}`` with no ``citations`` key; our
  ``TextBlock`` dump adds ``"citations":null`` (the Python model has the field, the TS wire omits
  it). Same for the empty text block inside ``content_block_start``.
- ``tool_use`` blocks: the real binary emits ``"caller":{"type":"direct"}``; mngr cannot observe
  the caller of a transcript-sourced tool call, so our ``ToolUseBlock`` dump emits ``"caller":null``.
- the assistant ``Message`` wrapper: our dump adds ``"container":null`` (absent on the wire) and
  omits ``diagnostics`` / ``context_management`` (present-but-null on the wire); neither field
  exists on the Python ``Message`` model.

These are cosmetic. The only consumer is mngr's own lenient ``Message.model_validate`` parser (the
consume side below), which accepts each field present, absent, or null. We keep the plain
``model_dump()`` rather than post-processing toward the exact TS shape because no single dump mode
matches it -- the real binary omits some nulls (``citations``) while keeping others
(``stop_reason`` / ``stop_details`` on the framing events) -- so chasing byte-fidelity would mean
per-field special-casing for a consumer that does not care.
"""

import json
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final
from typing import assert_never

from anthropic.types import Message
from anthropic.types import MessageDeltaUsage
from anthropic.types import RawContentBlockDeltaEvent
from anthropic.types import RawContentBlockStartEvent
from anthropic.types import RawContentBlockStopEvent
from anthropic.types import RawMessageDeltaEvent
from anthropic.types import RawMessageStartEvent
from anthropic.types import RawMessageStopEvent
from anthropic.types import RawMessageStreamEvent
from anthropic.types import StopReason
from anthropic.types import TextBlock
from anthropic.types import TextDelta
from anthropic.types import Usage
from anthropic.types.raw_message_delta_event import Delta as MessageDeltaDelta
from loguru import logger
from pydantic import TypeAdapter
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

# We reconstruct a single text content block per streamed message; tool-call and reasoning blocks
# never reach the partial stream, so index 0 always refers to the assistant-text block.
_DEFAULT_BLOCK_INDEX: Final[int] = 0

# Validators built once at import: pydantic recommends reusing a TypeAdapter rather than
# constructing one per call.
_STREAM_EVENT_ADAPTER: Final[TypeAdapter[RawMessageStreamEvent]] = TypeAdapter(RawMessageStreamEvent)
_MESSAGE_ADAPTER: Final[TypeAdapter[Message]] = TypeAdapter(Message)


# ---------------------------------------------------------------------------
# Emit side -- construct via anthropic models, dump to the wire dict.
# ---------------------------------------------------------------------------


@pure
def text_delta_event(text: str, index: int = _DEFAULT_BLOCK_INDEX) -> dict[str, Any]:
    """Build a ``content_block_delta`` carrying a ``text_delta``.

    This is the hot path (the only inner event the CLI token stream emits); its ``model_dump()``
    is byte-identical to the dict producers hand-rolled before. (The framing builders below are
    *not* byte-identical -- dumping anthropic's models adds the API's optional, semantically-null
    metadata fields. See the module docstring.)
    """
    return RawContentBlockDeltaEvent(
        type="content_block_delta", index=index, delta=TextDelta(type="text_delta", text=text)
    ).model_dump()


@pure
def message_start_event(message_id: str, model: str) -> dict[str, Any]:
    """Build the opening ``message_start`` event for a synthesized assistant message.

    ``usage`` is a zeroed stub: stream-time token counts are unknown on the mngr transport, and
    the authoritative usage stays on the transcript-derived result.
    """
    message = Message(
        id=message_id,
        type="message",
        role="assistant",
        model=model,
        content=[],
        stop_reason=None,
        stop_sequence=None,
        usage=Usage(input_tokens=0, output_tokens=0),
    )
    return RawMessageStartEvent(type="message_start", message=message).model_dump()


@pure
def content_block_start_event(index: int = _DEFAULT_BLOCK_INDEX) -> dict[str, Any]:
    """Build a ``content_block_start`` opening an (initially empty) text block."""
    return RawContentBlockStartEvent(
        type="content_block_start", index=index, content_block=TextBlock(type="text", text="", citations=None)
    ).model_dump()


@pure
def content_block_stop_event(index: int = _DEFAULT_BLOCK_INDEX) -> dict[str, Any]:
    """Build a ``content_block_stop`` closing a text block."""
    return RawContentBlockStopEvent(type="content_block_stop", index=index).model_dump()


@pure
def message_delta_event(stop_reason: str) -> dict[str, Any]:
    """Build a ``message_delta`` carrying the terminal ``stop_reason`` (zeroed usage stub).

    ``stop_reason`` is accepted as a plain ``str`` (so callers need not import anthropic's
    ``StopReason`` literal) and coerced to a recognized reason, degrading to ``None`` if unknown.
    """
    return RawMessageDeltaEvent(
        type="message_delta",
        delta=MessageDeltaDelta(stop_reason=_coerce_stop_reason(stop_reason), stop_sequence=None),
        usage=MessageDeltaUsage(output_tokens=0),
    ).model_dump()


@pure
def message_stop_event() -> dict[str, Any]:
    """Build a ``message_stop`` event."""
    return RawMessageStopEvent(type="message_stop").model_dump()


@pure
def wrap_stream_event(event: Mapping[str, Any], session_id: str) -> dict[str, Any]:
    """Wrap a raw inner ``event`` payload in the CLI's ``stream_event`` envelope.

    This outer envelope (``type``/``event``/``session_id``) is a ``claude`` CLI construct, not an
    Anthropic-API type, so it is assembled here as a plain dict rather than via an SDK model.
    """
    return {"type": "stream_event", "event": dict(event), "session_id": session_id}


@pure
def build_assistant_message(
    *,
    message_id: str,
    model: str,
    content: Sequence[Mapping[str, Any]],
    stop_reason: str | None,
    usage: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the inner ``message`` of an ``assistant`` line, validated against ``anthropic.types.Message``.

    ``content`` is a sequence of plain content-block dicts (``text`` / ``tool_use``); pydantic
    validates each into the typed block union. Two best-effort coercions handle data the mngr
    transport cannot fully observe:

    - ``usage`` is validated into ``anthropic.types.Usage`` when present (real transcript token
      counts are preserved), falling back to a zeroed stub when absent -- ``Usage`` requires
      ``input_tokens``/``output_tokens`` and screen-scraped synthesis has neither.
    - a ``tool_use`` block whose ``input`` is not a JSON object is coerced to ``{}``: ``anthropic``
      requires an object there, and robinhood's tool-input previews may be truncated mid-JSON.

    Raises ``pydantic.ValidationError`` if ``content`` contains a block this ``anthropic`` package
    cannot model -- callers synthesizing only text/tool_use blocks never hit this, and surfacing
    it is preferable to silently emitting a malformed message.
    """
    # Validate the assembled dict (plain content-block dicts and a usage dict included) in one pass
    # rather than constructing Message() with mixed dict/model args -- model_validate takes Any, so
    # the block union does not need a per-argument type cast.
    message = Message.model_validate(
        {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [_coerce_content_block(block) for block in content],
            "stop_reason": _coerce_stop_reason(stop_reason),
            "stop_sequence": None,
            "usage": _usage_or_zeroed(usage),
        }
    )
    return message.model_dump()


@pure
def _coerce_content_block(block: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``block`` as a plain dict, coercing a non-object ``tool_use`` ``input`` to ``{}``."""
    result = dict(block)
    if result.get("type") == "tool_use" and not isinstance(result.get("input"), dict):
        result["input"] = {}
    return result


def _coerce_stop_reason(stop_reason: str | None) -> StopReason | None:
    """Pass a recognized ``stop_reason`` through; map any unknown/non-null value to ``None``.

    ``Message.stop_reason`` is an optional ``StopReason`` literal. Transcript-sourced values are
    overwhelmingly the known reasons, but we cannot statically guarantee it, so an unrecognized
    string degrades to ``None`` rather than failing validation.
    """
    if stop_reason is not None and stop_reason in _STOP_REASONS:
        # `in` over the literal's members narrows the runtime value to a valid StopReason.
        return stop_reason  # ty: ignore[invalid-return-type]
    return None


_STOP_REASONS: Final[frozenset[str]] = frozenset(StopReason.__args__)


def _usage_or_zeroed(usage: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a ``usage`` dict validated through ``Usage`` when present and well-formed, else zeroed."""
    if usage is not None:
        try:
            return Usage.model_validate(dict(usage)).model_dump()
        except ValidationError:
            logger.trace("assistant-message usage {!r} did not validate; using a zeroed stub", usage)
    return {"input_tokens": 0, "output_tokens": 0}


# ---------------------------------------------------------------------------
# Consume side -- validate into the union, dispatch exhaustively.
# ---------------------------------------------------------------------------


class StreamEventText(FrozenModel):
    """The bits mngr extracts from a typed stream event.

    A single event yields at most one of these: a ``message_start`` carries a new-message id, a
    ``content_block_delta``/``text_delta`` carries delta text, and every other variant carries
    neither.
    """

    delta_text: str | None = None
    message_start_id: str | None = None


@pure
def decode_stream_line(line: str) -> dict[str, Any] | None:
    """Decode a single stream-json line into a dict, or ``None`` if it is not a JSON object.

    Non-JSON lines (blank lines, debug output ``claude`` sometimes leaks to stdout) are expected
    and decode to ``None``.
    """
    try:
        parsed = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def validate_stream_event(payload: object) -> RawMessageStreamEvent | None:
    """Validate a raw inner ``event`` payload against the anthropic stream-event union.

    Returns the typed event, or ``None`` when ``payload`` is not a dict or does not match any
    modeled variant. The latter is how a live CLI that runs ahead of our pinned ``anthropic``
    package degrades gracefully: an unmodeled event is skipped, not raised. (The new-variant
    *signal* is the static exhaustiveness check in :func:`classify_stream_event`, which trips when
    we bump the package -- not this runtime path.)

    Unlike the ``assistant`` summary (see :func:`assistant_text`), there is no separate lenient
    raw-dict fallback here: a ``message_start`` carries empty ``content``, so there are no evolving
    content-block types to drift, and the only field we read from it (``message.id``) is non-load-
    bearing -- if validation ever fails, the caller simply loses the deltas-vs-summary correlation
    optimization while text still streams. The assistant summary, by contrast, carries populated,
    evolving content blocks, so it keeps a fallback to avoid dropping real text.
    """
    try:
        return _STREAM_EVENT_ADAPTER.validate_python(payload)
    except ValidationError:
        logger.trace("stream_event payload did not match any modeled anthropic variant: {!r}", payload)
        return None


def classify_stream_event(event: RawMessageStreamEvent) -> StreamEventText:
    """Exhaustively dispatch a typed stream event to the text/id mngr cares about.

    The ``assert_never`` arm is the exhaustiveness tripwire: when ``anthropic`` adds a union
    member, ``ty`` errors here and names it, forcing us to decide how to handle it.
    """
    match event:
        case RawContentBlockDeltaEvent():
            delta = event.delta
            return StreamEventText(delta_text=delta.text if isinstance(delta, TextDelta) else None)
        case RawMessageStartEvent():
            return StreamEventText(message_start_id=event.message.id)
        case RawContentBlockStartEvent() | RawContentBlockStopEvent() | RawMessageDeltaEvent() | RawMessageStopEvent():
            return StreamEventText()
        case _:
            assert_never(event)


def parse_stream_event(line: str) -> RawMessageStreamEvent | None:
    """Decode a full ``stream_event`` line and validate its inner event into the typed union.

    Returns ``None`` for non-``stream_event`` lines, undecodable lines, and events this
    ``anthropic`` package does not model (see :func:`validate_stream_event`).
    """
    parsed = decode_stream_line(line)
    if parsed is None or parsed.get("type") != "stream_event":
        return None
    return validate_stream_event(parsed.get("event"))


@pure
def extract_text_delta(line: str) -> str | None:
    """Extract delta text from a ``stream_event`` / ``content_block_delta`` / ``text_delta`` line."""
    event = parse_stream_event(line)
    if event is None:
        return None
    return classify_stream_event(event).delta_text


@pure
def extract_message_start_id(line: str) -> str | None:
    """Extract ``message.id`` from a ``stream_event`` / ``message_start`` line, if present.

    The id lets the caller correlate subsequent text deltas with a later top-level ``assistant``
    summary that carries the same id.
    """
    event = parse_stream_event(line)
    if event is None:
        return None
    return classify_stream_event(event).message_start_id


def parse_assistant_message(message: dict[str, Any] | None) -> Message | None:
    """Validate the inner ``message`` of an ``assistant`` line against ``anthropic.types.Message``.

    Returns ``None`` if ``message`` is not a dict or fails validation. Validation failure is
    expected when a live CLI emits a content-block type newer than our pinned ``anthropic``
    package; callers fall back to a lenient text scan (:func:`assistant_text`) so a valid response
    is never dropped just because one block type is unmodeled.
    """
    if not isinstance(message, dict):
        return None
    try:
        return _MESSAGE_ADAPTER.validate_python(message)
    except ValidationError:
        logger.trace("assistant message did not validate against anthropic.types.Message: {!r}", message)
        return None


def assistant_text(message: dict[str, Any] | None) -> str | None:
    """Concatenate the text of every text block in an ``assistant`` line's inner ``message``.

    Validates into ``anthropic.types.Message`` when possible; on validation failure (a CLI ahead
    of our ``anthropic`` package), falls back to a lenient scan over the raw block dicts so text
    is still surfaced. Returns ``None`` when there is no text.
    """
    typed = parse_assistant_message(message)
    if typed is not None:
        joined = "".join(block.text for block in typed.content if isinstance(block, TextBlock))
        return joined or None
    return _lenient_assistant_text(message)


def assistant_message_id(message: dict[str, Any] | None) -> str | None:
    """Extract ``id`` from an ``assistant`` line's inner ``message`` (typed, with lenient fallback)."""
    typed = parse_assistant_message(message)
    if typed is not None:
        return typed.id
    if message is not None:
        message_id = message.get("id")
        if isinstance(message_id, str):
            return message_id
    return None


@pure
def _lenient_assistant_text(message: dict[str, Any] | None) -> str | None:
    """Scan raw ``content`` block dicts for text, ignoring non-text and malformed blocks.

    The forward-compatible fallback for :func:`assistant_text`: used only when typed validation
    fails, it mirrors the API's text-block shape without requiring every sibling block to be a
    type this ``anthropic`` package knows.
    """
    if message is None:
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    if not parts:
        return None
    return "".join(parts)


def extract_assistant_text(line: str) -> str | None:
    """Extract concatenated text from a top-level ``assistant`` line's inner message.

    Without ``--include-partial-messages``, ``claude --output-format stream-json`` emits one
    ``{"type":"assistant","message":{"content":[{"type":"text","text":...},...]}}`` line per
    assistant turn. Returns the concatenation of all text blocks, or ``None`` if the line is not an
    ``assistant`` event with at least one text block.
    """
    return assistant_text(_assistant_message_payload(line))


def extract_assistant_message_id(line: str) -> str | None:
    """Extract ``message.id`` from a top-level ``assistant`` line, if present."""
    return assistant_message_id(_assistant_message_payload(line))


@pure
def _assistant_message_payload(line: str) -> dict[str, Any] | None:
    """Return the inner ``message`` dict of an ``assistant`` line, or ``None``."""
    parsed = decode_stream_line(line)
    if parsed is None or parsed.get("type") != "assistant":
        return None
    message = parsed.get("message")
    return message if isinstance(message, dict) else None
