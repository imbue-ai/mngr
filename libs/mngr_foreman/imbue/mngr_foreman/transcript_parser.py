"""Parse raw Claude session JSONL into diff-capable transcript events.

Vendored and modified from
``default-workspace-template/apps/system_interface/imbue/system_interface/claude_session_parser.py``.
Changes versus the original:

1. **Diff-capable tool inputs.** For file-editing tools (Edit/Write/MultiEdit/
   NotebookEdit) the full, untruncated ``input`` dict is attached under
   ``input_full`` so the frontend can render a real +/- diff from
   ``old_string``/``new_string``/``content``/``edits``. Every other tool also
   gets ``input_full`` capped at ``max_tool_output_chars`` (so Bash commands and
   Task/Agent prompts render in full). The original 200-char ``input_preview``
   is kept for a compact one-line label.
2. **Configurable truncation.** The hardcoded 2000-char tool_result cap is
   replaced with the ``max_tool_output_chars`` argument (0 = unlimited).
3. **Dropped.** tk lifecycle decoration/preservation, subagent enrichment
   (agentId trailer), the codex path, and auth-error detection -- none are
   needed by foreman. Queued-command attachment parsing is kept.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from typing import cast

from loguru import logger

# Tools whose full input we always attach untruncated because the frontend
# reconstructs a diff from it. Their inputs (old_string/new_string/content) are
# exactly what we must not truncate.
_DIFF_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

_MAX_INPUT_PREVIEW_LENGTH = 200

# A tool result (e.g. Read on an image, a screenshot) can carry base64 image
# blocks. We pass them through as a dedicated field, bypassing text truncation,
# but cap count + per-image size so a screenshot dump can't bloat the payload.
_MAX_TOOL_RESULT_IMAGES = 6
_MAX_IMAGE_DATA_CHARS = 12_000_000

_SOURCE = "claude/foreman_transcript"

# Claude Code's resume bookkeeping: an ``isMeta`` user message with exactly this
# text, answered by a synthetic-model "No response requested." assistant
# message. The pair is inert and hidden by Claude Code's own UI, so we hide it.
_RESUME_CONTINUATION_TEXT = "Continue from where you left off."
_SYNTHETIC_MODEL = "<synthetic>"
_NO_RESPONSE_REQUESTED_TEXT = "No response requested."

# A message the user queued while the agent was busy is recorded as an
# ``attachment`` event of this type rather than a normal ``user`` line. Only the
# ``prompt`` command mode carries verbatim user text.
_QUEUED_COMMAND_ATTACHMENT_TYPE = "queued_command"
_QUEUED_COMMAND_PROMPT_MODE = "prompt"

# A slash command the user types (``/foo bar``) is expanded by Claude Code into
# an XML-ish block; we rebuild the original text so the label shows what was
# typed. Tags appear in varying order, so match them individually.
_COMMAND_NAME_PATTERN = re.compile(r"<command-name>(.*?)</command-name>", re.DOTALL)
_COMMAND_ARGS_PATTERN = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
# The captured stdout of a local slash command (e.g. ``/login`` ->
# "Login interrupted"). Claude Code wraps it in this tag inside a user message.
_LOCAL_STDOUT_PATTERN = re.compile(r"<local-command-stdout>(.*?)</local-command-stdout>", re.DOTALL)
# The boilerplate caveat Claude Code injects (isMeta) ahead of local-command
# output. Pure noise; we strip the wrapper so the collapsed label is readable.
_LOCAL_CAVEAT_PATTERN = re.compile(r"<local-command-caveat>(.*?)</local-command-caveat>", re.DOTALL)

# Longest a framework one-liner label may be before we clip it.
_MAX_FRAMEWORK_LABEL_LENGTH = 120


def _normalize_slash_command(text: str) -> str:
    """Rebuild ``/name args`` from a Claude Code slash-command expansion.

    Returns ``text`` unchanged when it is not a command expansion.
    """
    name_match = _COMMAND_NAME_PATTERN.search(text)
    if name_match is None:
        return text
    command = name_match.group(1).strip()
    if not command:
        return text
    args_match = _COMMAND_ARGS_PATTERN.search(text)
    args = args_match.group(1).strip() if args_match is not None else ""
    return f"{command} {args}".strip()


def _extract_text_content(content: str | list[dict[str, Any]] | Any) -> str:
    """Extract plain text from a message content field (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if text:
                parts.append(text)
    return "\n".join(parts)


def _has_tool_results_only(content: str | list[Any] | Any) -> bool:
    """Check if a content list contains only tool_result blocks (no user text)."""
    if isinstance(content, str):
        return False
    if not isinstance(content, list):
        return True
    for block in content:
        if isinstance(block, dict):
            if block.get("type", "") not in ("tool_result",):
                return False
        elif isinstance(block, str):
            return False
    return True


def _extract_image_block(item: dict[str, Any], image_id: str) -> dict[str, str] | None:
    """Pull a base64 image out of a content block, or None.

    Shape written by Claude Code (in tool results, human-pasted messages, and
    queued-command attachments alike):
    ``{"type":"image","source":{"type":"base64","media_type":"image/png","data":"<b64>"}}``.
    Oversize payloads and non-base64 sources are dropped. ``image_id`` is a stable
    handle the server can use to serve a large image by reference.
    """
    source = item.get("source")
    if not isinstance(source, dict) or source.get("type") != "base64":
        return None
    data = source.get("data")
    if not isinstance(data, str) or not data or len(data) > _MAX_IMAGE_DATA_CHARS:
        return None
    media_type = source.get("media_type")
    if not isinstance(media_type, str) or not media_type.startswith("image/"):
        media_type = "image/png"
    return {"id": image_id, "media_type": media_type, "data": data}


def _extract_images_from_content(content: Any, id_prefix: str) -> list[dict[str, str]]:
    """Collect base64 image blocks from a content list, capped in count.

    Handles the ``content`` list shared by tool results, human-pasted user
    messages, and queued-command attachment prompts. ``id_prefix`` is combined
    with the block index for a stable per-image id.
    """
    if not isinstance(content, list):
        return []
    images: list[dict[str, str]] = []
    for index, item in enumerate(content):
        if not isinstance(item, dict):
            continue
        block = cast("dict[str, Any]", item)
        if block.get("type") != "image":
            continue
        if len(images) >= _MAX_TOOL_RESULT_IMAGES:
            break
        image = _extract_image_block(block, f"{id_prefix}-{index}")
        if image is not None:
            images.append(image)
    return images


def _make_event_id(uuid: str, suffix: str) -> str:
    """Derive a deterministic event_id from the source UUID and a suffix."""
    return f"{uuid}-{suffix}"


def _emit(
    new_events: list[tuple[str, dict[str, Any]]],
    timestamp: str,
    event: dict[str, Any],
    session_id: str | None,
) -> None:
    """Stamp ``session_id`` onto ``event`` (if given) and queue it for output."""
    if session_id is not None:
        event["session_id"] = session_id
    new_events.append((timestamp, event))


def _truncate(content: str, max_chars: int) -> str:
    """Head-truncate ``content`` to ``max_chars`` (0 means unlimited)."""
    if max_chars <= 0 or len(content) <= max_chars:
        return content
    return content[:max_chars] + "..."


def _first_line(text: str, limit: int = _MAX_FRAMEWORK_LABEL_LENGTH) -> str:
    """First non-empty line of ``text``, clipped to ``limit`` chars."""
    for line in text.splitlines():
        if line.strip():
            clipped = line.strip()
            return clipped if len(clipped) <= limit else clipped[:limit] + "…"
    return ""


def _framework_label_and_detail(raw: dict[str, Any], text: str) -> tuple[str, str] | None:
    """Classify Claude Code framework noise, or None for a real user message.

    The chat page renders framework records (a slash-command invocation, its
    captured local stdout, or any ``isMeta`` bookkeeping message) as a dim,
    collapsed one-liner rather than a user bubble. This returns ``(label, detail)``
    -- the label is the collapsed summary; the detail is the full text shown when
    expanded -- or ``None`` when ``text`` is genuine user input to show normally.

    The signals all come straight from the JSONL, so we key on them rather than
    re-deriving in JS: the ``<command-name>`` / ``<local-command-stdout>`` wrappers
    Claude Code emits, and the ``isMeta`` flag it sets on injected messages.
    """
    stripped = text.strip()
    # A control marker Claude writes into a user record when a turn is interrupted
    # (the bare "[Request interrupted by user]" and the "...for tool use" variant)
    # -> a chip, never a user bubble.
    if stripped.startswith("[Request interrupted by user"):
        return "interrupted", stripped
    # Framework wrappers injected AROUND a user record (subagent/task notices,
    # ephemeral system reminders). Match only when the record STARTS with the tag,
    # so a user merely mentioning the tag in prose isn't swallowed.
    for _wrap_tag in ("system-reminder", "task-notification", "task_notification"):
        if stripped.startswith("<" + _wrap_tag + ">"):
            m = re.match(r"<" + _wrap_tag + r">\s*(.*?)\s*(?:</" + _wrap_tag + r">)?\s*\Z", stripped, re.DOTALL)
            inner = (m.group(1).strip() if m else "")
            return _wrap_tag.replace("_", "-"), (inner or _wrap_tag)
    # A slash-command invocation: "<command-name>login</command-name>...".
    if _COMMAND_NAME_PATTERN.search(text):
        command = _normalize_slash_command(text)
        label = "/" + command.lstrip("/") if command else "command"
        return label, label
    # The local stdout of a slash command: "<local-command-stdout>...".
    stdout_match = _LOCAL_STDOUT_PATTERN.search(text)
    if stdout_match is not None:
        out = stdout_match.group(1).strip()
        return (_first_line(out) or "(no output)"), (out or "(no output)")
    # The injected local-command caveat: strip the wrapper for a readable label.
    caveat_match = _LOCAL_CAVEAT_PATTERN.search(text)
    if caveat_match is not None:
        inner = caveat_match.group(1).strip()
        return (_first_line(inner) or "(caveat)"), (inner or "(caveat)")
    # Any other injected/bookkeeping message Claude Code marks meta.
    if raw.get("isMeta"):
        return (_first_line(text) or "(meta)"), (text.strip() or "(meta)")
    return None


def _is_resume_continuation_marker(raw: dict[str, Any]) -> bool:
    """True if ``raw`` is Claude Code's synthetic resume-continuation user message."""
    if not raw.get("isMeta"):
        return False
    text = _extract_text_content(raw.get("message", {}).get("content"))
    return text.strip() == _RESUME_CONTINUATION_TEXT


def _is_resume_no_response_reply(message: dict[str, Any]) -> bool:
    """True if ``message`` is the synthetic reply half of the resume turn-pair.

    Both the synthetic model AND the exact no-response text are required: the
    synthetic model alone also covers API-error/auth notices the user must see.
    """
    if message.get("model") != _SYNTHETIC_MODEL:
        return False
    return _extract_text_content(message.get("content")).strip() == _NO_RESPONSE_REQUESTED_TEXT


def parse_claude_session_lines(
    lines: list[str],
    existing_event_ids: set[str] | None = None,
    tool_name_by_call_id: dict[str, str] | None = None,
    max_tool_output_chars: int = 20000,
    session_id: str | None = None,
    queue_state: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Parse raw Claude session JSONL lines into transcript events.

    Args:
        lines: Raw JSONL lines from a Claude session file.
        existing_event_ids: Set of event IDs already emitted, for deduplication.
            Mutated in place. If None, a fresh set is used (no cross-call dedup).
        tool_name_by_call_id: Mutable mapping from tool_use_id to tool_name,
            carried across calls for cross-message tool-name resolution.
        max_tool_output_chars: Cap on tool_result output and non-diff tool input
            length. 0 means unlimited.
        session_id: If provided, stamped onto each event.

    Returns:
        List of transcript event dicts, sorted by timestamp.
    """
    if existing_event_ids is None:
        existing_event_ids = set()
    if tool_name_by_call_id is None:
        tool_name_by_call_id = {}
    if queue_state is None:
        queue_state = []

    new_events: list[tuple[str, dict[str, Any]]] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        # One malformed or oddly-shaped record must never kill the whole stream: the
        # transcript is external data, backfill re-reads it from byte 0 on every fresh
        # connection, so an uncaught error here would poison every future reconnect too.
        # Catch broadly, log, and skip the single line (covers non-dict JSON, a null
        # nested "message", and any future shape surprise in one place).
        try:
            raw = json.loads(line)
            if not isinstance(raw, dict):
                # Valid JSON that isn't an object (a bare null / number / list) carries
                # no record; skip rather than AttributeError on raw.get below.
                continue

            event_type: str = raw.get("type", "")
            timestamp: str = raw.get("timestamp", "")

            # queue-operation records track the live message queue in real time
            # (enqueue/remove/dequeue/popAll). They carry a timestamp but NO uuid, so
            # they must be handled BEFORE the uuid guard below.
            if event_type == "queue-operation":
                _parse_queue_operation(raw, timestamp, existing_event_ids, queue_state, new_events, session_id)
                continue

            uuid: str = raw.get("uuid", "")
            if not uuid or not timestamp:
                continue

            if event_type == "assistant":
                _parse_assistant_message(
                    raw,
                    uuid,
                    timestamp,
                    existing_event_ids,
                    tool_name_by_call_id,
                    new_events,
                    max_tool_output_chars,
                    session_id,
                )
            elif event_type == "user":
                _parse_user_message(
                    raw,
                    uuid,
                    timestamp,
                    existing_event_ids,
                    tool_name_by_call_id,
                    new_events,
                    max_tool_output_chars,
                    session_id,
                )
            elif event_type == "attachment":
                _parse_queued_command_attachment(raw, uuid, timestamp, existing_event_ids, new_events, session_id)
            # Skip: progress, file-history-snapshot, system, result, etc.
        except Exception as e:  # noqa: BLE001 - one bad line must not kill the whole transcript stream
            logger.debug("Skipping unparseable transcript line: {}", e)
            continue

    new_events.sort(key=lambda x: x[0])
    return [event for _, event in new_events]


def _parse_assistant_message(
    raw: dict[str, Any],
    uuid: str,
    timestamp: str,
    existing_event_ids: set[str],
    tool_name_by_call_id: dict[str, str],
    new_events: list[tuple[str, dict[str, Any]]],
    max_tool_output_chars: int,
    session_id: str | None = None,
) -> None:
    event_id = _make_event_id(uuid, "assistant")
    if event_id in existing_event_ids:
        return

    message: dict[str, Any] = raw.get("message", {})

    # Drop Claude Code's resume bookkeeping -- its own UI hides it, so do we.
    if _is_resume_no_response_reply(message):
        return

    content_blocks: list[Any] = message.get("content", [])
    model: str = message.get("model", "unknown")
    stop_reason: str | None = message.get("stop_reason")
    usage_raw: dict[str, Any] = message.get("usage", {})

    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type", "")
        if block_type == "text":
            text = block.get("text", "")
            if text:
                text_parts.append(text)
        elif block_type == "tool_use":
            call_id: str = block.get("id", "")
            tool_name: str = block.get("name", "")
            tool_input = block.get("input", {})
            input_preview = json.dumps(tool_input, separators=(",", ":"))
            if len(input_preview) > _MAX_INPUT_PREVIEW_LENGTH:
                input_preview = input_preview[:_MAX_INPUT_PREVIEW_LENGTH] + "..."

            if call_id and tool_name:
                tool_name_by_call_id[call_id] = tool_name

            tool_call: dict[str, Any] = {
                "tool_call_id": call_id,
                "tool_name": tool_name,
                "input_preview": input_preview,
            }
            # Diff tools keep their full input verbatim (old_string/new_string/
            # content/edits) so the frontend can render a real diff. Every other
            # tool gets a capped full input so Bash commands and Task/Agent
            # prompts still render in full.
            if isinstance(tool_input, dict):
                if tool_name in _DIFF_TOOLS:
                    tool_call["input_full"] = tool_input
                else:
                    tool_call["input_full"] = _capped_input(tool_input, max_tool_output_chars)
            tool_calls.append(tool_call)

    usage: dict[str, Any] | None = None
    if usage_raw:
        usage = {
            "input_tokens": usage_raw.get("input_tokens", 0),
            "output_tokens": usage_raw.get("output_tokens", 0),
            "cache_read_tokens": usage_raw.get("cache_read_input_tokens"),
            "cache_write_tokens": usage_raw.get("cache_creation_input_tokens"),
        }

    joined_text = "\n".join(text_parts)
    event: dict[str, Any] = {
        "timestamp": timestamp,
        "type": "assistant_message",
        "event_id": event_id,
        "source": _SOURCE,
        "role": "assistant",
        "model": model,
        "text": joined_text,
        "tool_calls": tool_calls,
        "stop_reason": stop_reason,
        "usage": usage,
        "message_uuid": uuid,
    }
    existing_event_ids.add(event_id)
    _emit(new_events, timestamp, event, session_id)


def _capped_input(tool_input: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Return a shallow copy of ``tool_input`` with each string value capped.

    Non-diff tools do not need byte-exact inputs; capping keeps a giant Bash
    heredoc or Task prompt from bloating the payload while still rendering fully
    in the common case.
    """
    if max_chars <= 0:
        return tool_input
    capped: dict[str, Any] = {}
    for key, value in tool_input.items():
        if isinstance(value, str):
            capped[key] = _truncate(value, max_chars)
        else:
            capped[key] = value
    return capped


def _parse_user_message(
    raw: dict[str, Any],
    uuid: str,
    timestamp: str,
    existing_event_ids: set[str],
    tool_name_by_call_id: dict[str, str],
    new_events: list[tuple[str, dict[str, Any]]],
    max_tool_output_chars: int,
    session_id: str | None = None,
) -> None:
    message: dict[str, Any] = raw.get("message", {})
    content = message.get("content")

    # Emit a user text / framework / paste-image message.
    if not _has_tool_results_only(content):
        event_id = _make_event_id(uuid, "user")
        if event_id not in existing_event_ids:
            raw_text = _extract_text_content(content)
            stripped = raw_text.strip()
            # Human-pasted images ride alongside the text in the same content list.
            paste_images = _extract_images_from_content(content, f"{uuid}-u")
            # Only Claude Code's synthetic resume-continuation marker is fully hidden
            # (its own UI hides it too); the interrupt sentinel instead renders as an
            # "interrupted" chip (see _framework_label_and_detail).
            is_hidden = _is_resume_continuation_marker(raw)
            if (stripped or paste_images) and not is_hidden:
                # Framework detection applies only to text-bearing messages.
                framework = _framework_label_and_detail(raw, raw_text) if stripped else None
                event: dict[str, Any]
                if framework is not None:
                    # Claude Code framework noise (/command, its stdout, meta) ->
                    # a collapsed one-liner, not a user bubble.
                    label, detail = framework
                    event = {
                        "timestamp": timestamp,
                        "type": "framework_message",
                        "event_id": event_id,
                        "source": _SOURCE,
                        "label": label,
                        "detail": detail,
                        "message_uuid": uuid,
                    }
                else:
                    event = {
                        "timestamp": timestamp,
                        "type": "user_message",
                        "event_id": event_id,
                        "source": _SOURCE,
                        "role": "user",
                        "content": _normalize_slash_command(raw_text),
                        "message_uuid": uuid,
                    }
                    if paste_images:
                        event["images"] = paste_images
                existing_event_ids.add(event_id)
                _emit(new_events, timestamp, event, session_id)

    # Emit tool result events for any tool_result blocks.
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            tool_call_id: str = block.get("tool_use_id", "")
            if not tool_call_id:
                continue

            event_id = _make_event_id(uuid, f"tool_result-{tool_call_id}")
            if event_id in existing_event_ids:
                continue

            raw_result_content = block.get("content", "")
            images = _extract_images_from_content(raw_result_content, f"{uuid}-{tool_call_id}")
            if isinstance(raw_result_content, list):
                parts: list[str] = []
                for item in raw_result_content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(item.get("text", ""))
                    elif isinstance(item, str):
                        parts.append(item)
                result_content = "\n".join(parts)
            elif isinstance(raw_result_content, str):
                result_content = raw_result_content
            else:
                result_content = str(raw_result_content)

            tool_name = tool_name_by_call_id.get(tool_call_id, "unknown")
            result_content = _truncate(result_content, max_tool_output_chars)

            event = {
                "timestamp": timestamp,
                "type": "tool_result",
                "event_id": event_id,
                "source": _SOURCE,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "output": result_content,
                "is_error": bool(block.get("is_error", False)),
                "message_uuid": uuid,
            }
            if images:
                event["images"] = images
            existing_event_ids.add(event_id)
            _emit(new_events, timestamp, event, session_id)


def _make_queue_event_id(timestamp: str, key: str, prefix: str) -> str:
    """Deterministic event_id for a queue event -- queue-operation lines have no uuid."""
    digest = hashlib.sha1(f"{timestamp}|{key}".encode()).hexdigest()[:16]
    return f"queue-{prefix}-{digest}"


def _parse_queue_operation(
    raw: dict[str, Any],
    timestamp: str,
    existing_event_ids: set[str],
    queue_state: list[dict[str, Any]],
    new_events: list[tuple[str, dict[str, Any]]],
    session_id: str | None = None,
) -> None:
    """Track Claude Code's live message queue and emit real-time queue events.

    Claude writes a ``queue-operation`` line the INSTANT a message is queued (typed
    while a turn is running) -- long before the delayed ``queued_command``
    attachment (measured lag: seconds to over a minute). Foreman used to render
    only that late attachment, so a queued message was invisible until accepted;
    keying off these operations makes it appear immediately.

    Replaying the operations in file order reconstructs the live queue (append-only,
    pure FIFO), so a full re-read always converges on the correct pending set:
      * ``enqueue`` (has content) -> a message entered the queue -> user_message(queued=True)
      * ``remove``  (has content) -> accepted at a turn boundary  -> queue_accepted
      * ``dequeue`` (no content)  -> accepted via interrupt (Esc) -> queue_accepted (FIFO head)
      * ``popAll``  (has content) -> pulled back into the editor   -> queue_removed (all)
    The later ``queued_command`` attachment and ``promptSource:"queued"`` user line
    carry the SAME text; the client reconciles them by content-key rather than
    drawing a second bubble.
    """
    if not timestamp:
        return
    op = raw.get("operation")
    content = raw.get("content")
    text = _extract_text_content(content) if content is not None else ""
    key = text.strip()

    if op == "enqueue":
        if not key:
            return
        # A framework message delivered THROUGH the queue while the agent is busy (a
        # <task-notification>/<system-reminder> the harness injects) already renders as
        # a collapsed chip when its real record arrives -- don't ALSO draw it as a raw
        # queued user bubble, that's the duplicate "spam". Start-anchored so a real user
        # message that merely quotes such a tag is still shown. Tracked in queue_state
        # regardless so the FIFO dequeue/accept below stays aligned with Claude's queue.
        is_framework = _framework_label_and_detail(raw, text) is not None
        queue_state.append({"key": key, "text": text, "framework": is_framework})
        if is_framework:
            return
        event_id = _make_queue_event_id(timestamp, key, "enq")
        if event_id in existing_event_ids:
            return
        existing_event_ids.add(event_id)
        event: dict[str, Any] = {
            "timestamp": timestamp,
            "type": "user_message",
            "event_id": event_id,
            "source": _SOURCE,
            "role": "user",
            "content": text,
            "queued": True,
        }
        _emit(new_events, timestamp, event, session_id)
    elif op in ("remove", "dequeue"):
        # Accepted: the model now sees this message. `remove` names it; the
        # interrupt-path `dequeue` carries no content, so pop the FIFO head.
        popped: dict[str, Any] | None = None
        if key:
            for i, item in enumerate(queue_state):
                if item["key"] == key:
                    popped = queue_state.pop(i)
                    break
        if popped is None and queue_state:
            popped = queue_state.pop(0)
        if popped is None:
            return
        if popped.get("framework"):
            return  # no bubble was ever drawn for it -> nothing to mark accepted
        event_id = _make_queue_event_id(timestamp, popped["key"], "acc")
        if event_id in existing_event_ids:
            return
        existing_event_ids.add(event_id)
        accepted: dict[str, Any] = {
            "timestamp": timestamp,
            "type": "queue_accepted",
            "event_id": event_id,
            "key": popped["key"],
        }
        _emit(new_events, timestamp, accepted, session_id)
    elif op == "popAll":
        # The whole queue was yanked back into the editor (the user is editing a
        # queued message); drop every pending bubble. Edited text re-arrives as a
        # fresh enqueue.
        for i, item in enumerate(list(queue_state)):
            if item.get("framework"):
                continue  # never rendered a bubble -> nothing to remove
            event_id = _make_queue_event_id(timestamp, item["key"], f"rm{i}")
            if event_id in existing_event_ids:
                continue
            existing_event_ids.add(event_id)
            removed: dict[str, Any] = {
                "timestamp": timestamp,
                "type": "queue_removed",
                "event_id": event_id,
                "key": item["key"],
            }
            _emit(new_events, timestamp, removed, session_id)
        queue_state.clear()


def _parse_queued_command_attachment(
    raw: dict[str, Any],
    uuid: str,
    timestamp: str,
    existing_event_ids: set[str],
    new_events: list[tuple[str, dict[str, Any]]],
    session_id: str | None = None,
) -> None:
    """Emit a ``user_message`` event for a message the user queued while busy.

    Claude Code writes such a message as a ``queued_command`` attachment rather
    than a normal ``user`` line. The ``prompt`` carries verbatim text (a string)
    or, when the queued message had a pasted image, a content list of text +
    image blocks.
    """
    attachment = raw.get("attachment")
    if not isinstance(attachment, dict):
        return
    if attachment.get("type") != _QUEUED_COMMAND_ATTACHMENT_TYPE:
        return
    if attachment.get("commandMode") != _QUEUED_COMMAND_PROMPT_MODE:
        return
    raw_prompt = attachment.get("prompt")
    images = _extract_images_from_content(raw_prompt, f"{uuid}-q")
    prompt_text = _extract_text_content(raw_prompt) if isinstance(raw_prompt, list) else raw_prompt
    prompt = _normalize_slash_command(prompt_text) if isinstance(prompt_text, str) else ""
    if not prompt.strip() and not images:
        return

    event_id = _make_event_id(uuid, "queued")
    if event_id in existing_event_ids:
        return

    event: dict[str, Any] = {
        "timestamp": timestamp,
        "type": "user_message",
        "event_id": event_id,
        "source": _SOURCE,
        "role": "user",
        "content": prompt,
        "message_uuid": uuid,
        # Distinguishes a message still sitting in claude's queue (typed while it
        # was generating) from one it has actually accepted as a turn. The client
        # styles queued messages differently and flips them when the delivered
        # user_message for the same content arrives.
        "queued": True,
    }
    if images:
        event["images"] = images
    existing_event_ids.add(event_id)
    _emit(new_events, timestamp, event, session_id)
