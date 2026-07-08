"""Pure conversion of claude's native per-session JSONL into ``claude_agent_sdk`` message objects.

The mngr claude agent mirrors claude's per-session JSONL transcript to
``$MNGR_AGENT_STATE_DIR/logs/claude_transcript/events.jsonl`` (see
``stream_transcript.sh``); this is the same raw source the robinhood orchestrator already
tails. Each line is one anthropic-shaped event with a top-level ``type`` (``"user"`` /
``"assistant"`` / ``"summary"`` / ...), a ``uuid``, a ``sessionId``, and a nested ``message``
holding the model, content blocks, ``stop_reason``, and ``usage``.

The functions here turn those raw event dicts into the documented ``claude_agent_sdk``
dataclasses (``AssistantMessage`` / ``UserMessage`` and the content blocks) so that the
mngr-backed SDK can yield the exact same shapes as the real SDK. ``SystemMessage`` (init) and
``ResultMessage`` are NOT present in the session JSONL -- they are part of claude's stream-json
output -- so they are synthesized best-effort from the surrounding agent metadata by separate
helpers below.
"""

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final

from claude_agent_sdk import AssistantMessage
from claude_agent_sdk import ContentBlock
from claude_agent_sdk import ResultMessage
from claude_agent_sdk import SystemMessage
from claude_agent_sdk import TextBlock
from claude_agent_sdk import ThinkingBlock
from claude_agent_sdk import ToolResultBlock
from claude_agent_sdk import ToolUseBlock
from claude_agent_sdk import UserMessage

from imbue.imbue_common.pure import pure

# The two top-level session-JSONL event types that carry conversational content. Other
# types (``summary``, ``system``, ``file-history-snapshot``, ...) carry no assistant/user
# turn we surface and are skipped by :func:`parse_transcript_event`.
_ASSISTANT_EVENT_TYPE: Final[str] = "assistant"
_USER_EVENT_TYPE: Final[str] = "user"

# Content-block discriminators as they appear inside a message's ``content`` list.
_TEXT_BLOCK_TYPE: Final[str] = "text"
_THINKING_BLOCK_TYPE: Final[str] = "thinking"
_TOOL_USE_BLOCK_TYPE: Final[str] = "tool_use"
_TOOL_RESULT_BLOCK_TYPE: Final[str] = "tool_result"

# This parser only ever produces the four client-visible block types; ``ServerToolUseBlock`` /
# ``ServerToolResultBlock`` are never synthesized (the mngr transcript never contains them). The
# aggregate lists are still typed as the full ``ContentBlock`` union so they slot directly into
# ``AssistantMessage.content`` / ``UserMessage.content`` (whose element type is ``ContentBlock``).


@pure
def _parse_text_block(raw_block: Mapping[str, Any]) -> TextBlock | None:
    text = raw_block.get("text")
    if not isinstance(text, str):
        return None
    return TextBlock(text=text)


@pure
def _parse_thinking_block(raw_block: Mapping[str, Any]) -> ThinkingBlock | None:
    thinking = raw_block.get("thinking")
    if not isinstance(thinking, str):
        return None
    signature = raw_block.get("signature")
    return ThinkingBlock(thinking=thinking, signature=signature if isinstance(signature, str) else "")


@pure
def _parse_tool_use_block(raw_block: Mapping[str, Any]) -> ToolUseBlock | None:
    tool_use_id = raw_block.get("id")
    name = raw_block.get("name")
    tool_input = raw_block.get("input")
    if not isinstance(tool_use_id, str) or not isinstance(name, str):
        return None
    return ToolUseBlock(
        id=tool_use_id,
        name=name,
        input=dict(tool_input) if isinstance(tool_input, Mapping) else {},
    )


@pure
def _parse_tool_result_block(raw_block: Mapping[str, Any]) -> ToolResultBlock | None:
    tool_use_id = raw_block.get("tool_use_id")
    if not isinstance(tool_use_id, str):
        return None
    raw_content = raw_block.get("content")
    # The SDK models tool-result content as ``str | list[dict] | None``; coerce a block-list
    # into a list of plain dicts and pass strings / absent content through unchanged.
    content: str | list[dict[str, Any]] | None
    if raw_content is None or isinstance(raw_content, str):
        content = raw_content
    elif isinstance(raw_content, Sequence):
        content = [dict(item) for item in raw_content if isinstance(item, Mapping)]
    else:
        content = None
    is_error = raw_block.get("is_error")
    return ToolResultBlock(
        tool_use_id=tool_use_id,
        content=content,
        is_error=is_error if isinstance(is_error, bool) else None,
    )


@pure
def parse_content_blocks(raw_blocks: Sequence[Any]) -> list[ContentBlock]:
    """Convert a message's raw ``content`` list into typed SDK content blocks.

    Unknown or malformed blocks are skipped rather than raising, mirroring how the existing
    robinhood transcript parser tolerates partial / unexpected claude output.
    """
    blocks: list[ContentBlock] = []
    for raw_block in raw_blocks:
        if not isinstance(raw_block, Mapping):
            continue
        block_type = raw_block.get("type")
        if block_type == _TEXT_BLOCK_TYPE:
            text_block = _parse_text_block(raw_block)
            if text_block is not None:
                blocks.append(text_block)
        elif block_type == _THINKING_BLOCK_TYPE:
            thinking_block = _parse_thinking_block(raw_block)
            if thinking_block is not None:
                blocks.append(thinking_block)
        elif block_type == _TOOL_USE_BLOCK_TYPE:
            tool_use_block = _parse_tool_use_block(raw_block)
            if tool_use_block is not None:
                blocks.append(tool_use_block)
        elif block_type == _TOOL_RESULT_BLOCK_TYPE:
            tool_result_block = _parse_tool_result_block(raw_block)
            if tool_result_block is not None:
                blocks.append(tool_result_block)
        else:
            # Other block types (images, server tool blocks, ...) carry nothing this parser
            # surfaces and are intentionally dropped.
            pass
    return blocks


@pure
def build_assistant_message(raw_event: Mapping[str, Any]) -> AssistantMessage | None:
    """Build an ``AssistantMessage`` from a raw ``type=="assistant"`` session-JSONL event."""
    message = raw_event.get("message")
    if not isinstance(message, Mapping):
        return None
    raw_content = message.get("content")
    content = parse_content_blocks(raw_content) if isinstance(raw_content, Sequence) else []
    model = message.get("model")
    usage = message.get("usage")
    message_id = message.get("id")
    stop_reason = message.get("stop_reason")
    return AssistantMessage(
        content=content,
        model=model if isinstance(model, str) else "",
        usage=dict(usage) if isinstance(usage, Mapping) else None,
        message_id=message_id if isinstance(message_id, str) else None,
        stop_reason=stop_reason if isinstance(stop_reason, str) else None,
        session_id=_event_session_id(raw_event),
        uuid=_event_uuid(raw_event),
    )


@pure
def build_user_message(raw_event: Mapping[str, Any]) -> UserMessage | None:
    """Build a ``UserMessage`` from a raw ``type=="user"`` session-JSONL event.

    ``content`` is preserved as the documented ``str | list[ContentBlock]`` union: a plain
    string prompt stays a string, while a block list (e.g. tool results delivered back to the
    model) becomes typed content blocks.
    """
    message = raw_event.get("message")
    if not isinstance(message, Mapping):
        return None
    raw_content = message.get("content")
    content: str | list[ContentBlock]
    if isinstance(raw_content, str):
        content = raw_content
    elif isinstance(raw_content, Sequence):
        content = parse_content_blocks(raw_content)
    else:
        return None
    return UserMessage(content=content, uuid=_event_uuid(raw_event))


@pure
def parse_transcript_event(raw_event: Mapping[str, Any]) -> AssistantMessage | UserMessage | None:
    """Dispatch one raw session-JSONL event to its SDK message, or ``None`` if not surfaced."""
    event_type = raw_event.get("type")
    if event_type == _ASSISTANT_EVENT_TYPE:
        return build_assistant_message(raw_event)
    if event_type == _USER_EVENT_TYPE:
        # Framework-injected meta messages (stop-hook output, etc.) are not real user turns.
        if bool(raw_event.get("isMeta", False)):
            return None
        return build_user_message(raw_event)
    return None


@pure
def parse_transcript_events(raw_events: Sequence[Mapping[str, Any]]) -> list[AssistantMessage | UserMessage]:
    """Convert a batch of raw session-JSONL events into SDK messages, preserving order."""
    messages: list[AssistantMessage | UserMessage] = []
    for raw_event in raw_events:
        message = parse_transcript_event(raw_event)
        if message is not None:
            messages.append(message)
    return messages


@pure
def build_system_init_message(
    session_id: str,
    model: str,
    cwd: str,
    tools: Sequence[str],
) -> SystemMessage:
    """Synthesize the ``system`` / ``init`` message that the real SDK emits first.

    The session JSONL does not contain an init event, so the SDK driver supplies the fields it
    knows (session id, selected model, working directory) and any tool list it can determine.
    """
    return SystemMessage(
        subtype="init",
        data={
            "session_id": session_id,
            "model": model,
            "cwd": cwd,
            "tools": list(tools),
            "mcp_servers": [],
        },
    )


@pure
def build_result_message(
    session_id: str,
    is_error: bool,
    result_text: str | None,
    duration_ms: int,
    duration_api_ms: int,
    turn_count: int,
    usage: Mapping[str, Any] | None,
    total_cost_usd: float | None,
    model_usage: Mapping[str, Any] | None,
    permission_denials: Sequence[Any] | None,
    result_uuid: str,
) -> ResultMessage:
    """Synthesize the terminal ``result`` message that the real SDK emits last.

    The first six values are passed positionally to match ``ResultMessage``'s required-field
    order (subtype, duration_ms, duration_api_ms, is_error, turn count, session_id).
    """
    return ResultMessage(
        "error" if is_error else "success",
        duration_ms,
        duration_api_ms,
        is_error,
        turn_count,
        session_id,
        result=result_text,
        usage=dict(usage) if usage is not None else None,
        total_cost_usd=total_cost_usd,
        model_usage=dict(model_usage) if model_usage is not None else None,
        permission_denials=list(permission_denials) if permission_denials is not None else None,
        uuid=result_uuid,
    )


@pure
def collect_assistant_text(messages: Sequence[Any]) -> str:
    """Concatenate the text of every ``TextBlock`` across all ``AssistantMessage`` objects."""
    texts: list[str] = []
    for message in messages:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    texts.append(block.text)
    return "\n".join(texts)


@pure
def _event_uuid(raw_event: Mapping[str, Any]) -> str | None:
    uuid = raw_event.get("uuid")
    return uuid if isinstance(uuid, str) else None


@pure
def _event_session_id(raw_event: Mapping[str, Any]) -> str | None:
    # claude writes the session id as ``sessionId`` in the per-session JSONL.
    session_id = raw_event.get("sessionId")
    return session_id if isinstance(session_id, str) else None
