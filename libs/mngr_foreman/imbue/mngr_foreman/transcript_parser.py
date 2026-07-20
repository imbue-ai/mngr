"""Parse raw Claude session JSONL into diff-capable transcript events.

Vendored and modified from
``default-workspace-template/apps/system_interface/imbue/system_interface/claude_session_parser.py``
(see static/vendor/VENDORED.md for provenance). Changes versus the original:

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

import json
import re
from typing import Any

from loguru import logger

# Tools whose full input we always attach untruncated because the frontend
# reconstructs a diff from it. Their inputs (old_string/new_string/content) are
# exactly what we must not truncate.
_DIFF_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

_MAX_INPUT_PREVIEW_LENGTH = 200

_SOURCE = "claude/foreman_transcript"

# Sentinel Claude writes to the user channel when the user interrupts a turn
# (e.g. Esc mid-tool-use). It is a control marker, not real user input.
_INTERRUPT_SENTINEL_TEXT = "[Request interrupted by user]"

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


def _make_event_id(uuid: str, suffix: str) -> str:
    """Derive a deterministic event_id from the source UUID and a suffix."""
    return f"{uuid}-{suffix}"


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

    new_events: list[tuple[str, dict[str, Any]]] = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            logger.debug("Skipping malformed JSONL line: {}", e)
            continue

        event_type: str = raw.get("type", "")
        uuid: str = raw.get("uuid", "")
        timestamp: str = raw.get("timestamp", "")

        if not uuid or not timestamp:
            continue

        if event_type == "assistant":
            _parse_assistant_message(
                raw, uuid, timestamp, existing_event_ids, tool_name_by_call_id, new_events, max_tool_output_chars, session_id
            )
        elif event_type == "user":
            _parse_user_message(
                raw, uuid, timestamp, existing_event_ids, tool_name_by_call_id, new_events, max_tool_output_chars, session_id
            )
        elif event_type == "attachment":
            _parse_queued_command_attachment(raw, uuid, timestamp, existing_event_ids, new_events, session_id)
        # Skip: progress, file-history-snapshot, system, result, etc.

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
    if session_id is not None:
        event["session_id"] = session_id
    existing_event_ids.add(event_id)
    new_events.append((timestamp, event))


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

    # Emit a user text or framework message if there is actual user text.
    if not _has_tool_results_only(content):
        event_id = _make_event_id(uuid, "user")
        if event_id not in existing_event_ids:
            raw_text = _extract_text_content(content)
            stripped = raw_text.strip()
            # Fully hidden: the interrupt sentinel and Claude Code's resume marker
            # (its own UI hides the latter, so we do too).
            if stripped and stripped != _INTERRUPT_SENTINEL_TEXT and not _is_resume_continuation_marker(raw):
                framework = _framework_label_and_detail(raw, raw_text)
                if framework is not None:
                    # Claude Code framework noise (/command, its stdout, meta) ->
                    # a collapsed one-liner, not a user bubble.
                    label, detail = framework
                    event: dict[str, Any] = {
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
                if session_id is not None:
                    event["session_id"] = session_id
                existing_event_ids.add(event_id)
                new_events.append((timestamp, event))

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

            result_content = block.get("content", "")
            if isinstance(result_content, list):
                parts: list[str] = []
                for item in result_content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        parts.append(item.get("text", ""))
                    elif isinstance(item, str):
                        parts.append(item)
                result_content = "\n".join(parts)
            elif not isinstance(result_content, str):
                result_content = str(result_content)

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
            if session_id is not None:
                event["session_id"] = session_id
            existing_event_ids.add(event_id)
            new_events.append((timestamp, event))


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
    than a normal ``user`` line. Only the ``prompt`` mode carries verbatim text.
    """
    attachment = raw.get("attachment")
    if not isinstance(attachment, dict):
        return
    if attachment.get("type") != _QUEUED_COMMAND_ATTACHMENT_TYPE:
        return
    if attachment.get("commandMode") != _QUEUED_COMMAND_PROMPT_MODE:
        return
    prompt = attachment.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return
    prompt = _normalize_slash_command(prompt)

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
    }
    if session_id is not None:
        event["session_id"] = session_id
    existing_event_ids.add(event_id)
    new_events.append((timestamp, event))
