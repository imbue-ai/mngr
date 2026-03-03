"""Context gathering tool for changeling conversations.

This file is passed to `llm live-chat` via `--functions` and provides
the conversation agent with context about the current state of the changeling.

All event data follows the standard envelope format with timestamp, type,
event_id, and source fields. Events are read from logs/<source>/events.jsonl.

The tool tracks which events have already been returned, so each call only
returns new events since the last invocation. This makes conversations more
efficient by avoiding redundant context.

Settings are read from $MNG_AGENT_WORK_DIR/.changelings/settings.toml.
Missing file or keys fall back to built-in defaults.

NOTE: _format_events() is duplicated in extra_context_tool.py because these
files are deployed as standalone scripts to the agent host via --functions,
where they cannot import from each other or from the mng_claude_zygote package.
"""

import json
import os
import pathlib
import sys

_TAIL_CHUNK_SIZE = 8192


def _load_settings() -> dict:
    """Load settings from .changelings/settings.toml in the agent's work dir.

    NOTE: This function is intentionally duplicated (as _load_extra_settings)
    in extra_context_tool.py. These files are deployed as standalone scripts
    and cannot share imports.
    """
    try:
        import tomllib
    except ImportError:
        print("WARNING: tomllib not available, using default settings", file=sys.stderr)
        return {}
    work_dir = os.environ.get("MNG_AGENT_WORK_DIR", "")
    if not work_dir:
        return {}
    settings_path = pathlib.Path(work_dir) / ".changelings" / "settings.toml"
    if not settings_path.exists():
        return {}
    try:
        with settings_path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError) as e:
        print(f"WARNING: failed to load settings from {settings_path}: {e}", file=sys.stderr)
        return {}


_SETTINGS = _load_settings()
_CONTEXT = _SETTINGS.get("chat", {}).get("context", {})

_MAX_CONTENT_LENGTH = _CONTEXT.get("max_content_length", 200)
_MAX_TRANSCRIPT_LINES = _CONTEXT.get("max_transcript_line_count", 10)
_MAX_MESSAGES_LINES = _CONTEXT.get("max_messages_line_count", 20)
_MAX_MESSAGES_PER_CONVERSATION = _CONTEXT.get("max_messages_per_conversation", 3)
_MAX_TRIGGER_LINES = _CONTEXT.get("max_trigger_line_count", 5)

# State that persists between calls within the same llm live-chat session.
# llm loads the module once via exec() and keeps function objects in memory,
# so module-level state survives across invocations.
# Tracks byte offsets (file sizes) rather than line counts so that
# incremental reads can seek directly to new data.
_last_file_sizes: dict[str, int] = {}


def _read_tail_lines(file_path: pathlib.Path, n: int) -> list[str]:
    """Read the last n complete lines from a file by reading backwards from EOF.

    If the file doesn't end with a newline, the final partial line is dropped
    (incomplete write). Updates _last_file_sizes for incremental tracking.
    Returns lines in chronological order.
    """
    key = str(file_path)
    try:
        size = file_path.stat().st_size
    except OSError as e:
        print(f"WARNING: failed to stat {file_path}: {e}", file=sys.stderr)
        return []
    if size == 0:
        return []

    with file_path.open("rb") as f:
        data = b""
        pos = size

        while pos > 0:
            read_size = min(_TAIL_CHUNK_SIZE, pos)
            pos -= read_size
            f.seek(pos)
            data = f.read(read_size) + data
            # n+1 newlines guarantees n complete lines even when we start
            # mid-line (the first partial segment gets discarded by [-n:]).
            if data.count(b"\n") >= n + 1:
                break

    # If doesn't end with newline, drop the incomplete last line
    if data.endswith(b"\n"):
        _last_file_sizes[key] = size
    else:
        last_nl = data.rfind(b"\n")
        if last_nl == -1:
            return []
        # Track up to the end of the last complete line
        _last_file_sizes[key] = pos + last_nl + 1
        data = data[: last_nl + 1]

    text = data.decode("utf-8", errors="replace")
    lines = [line for line in text.split("\n") if line.strip()]
    return lines[-n:]


def _get_new_lines(file_path: pathlib.Path) -> list[str]:
    """Read new complete lines appended since the last call.

    Only returns lines terminated by a newline (complete writes).
    Updates _last_file_sizes to track the read position.
    """
    key = str(file_path)
    last_size = _last_file_sizes.get(key, 0)

    try:
        current_size = file_path.stat().st_size
    except OSError as e:
        print(f"WARNING: failed to stat {file_path}: {e}", file=sys.stderr)
        return []

    if current_size <= last_size:
        return []

    try:
        with file_path.open("rb") as f:
            f.seek(last_size)
            new_data = f.read()
    except OSError as e:
        print(f"WARNING: failed to read new data from {file_path}: {e}", file=sys.stderr)
        return []

    if new_data.endswith(b"\n"):
        _last_file_sizes[key] = current_size
    else:
        last_nl = new_data.rfind(b"\n")
        if last_nl == -1:
            return []
        _last_file_sizes[key] = last_size + last_nl + 1
        new_data = new_data[: last_nl + 1]

    text = new_data.decode("utf-8", errors="replace")
    return [line for line in text.split("\n") if line.strip()]


def gather_context() -> str:
    """Gather NEW context since the last call for this conversation.

    On the first call, returns recent events from all sources. On subsequent
    calls, returns only events that appeared since the previous invocation.

    Returns context from:
    - New messages from other active conversations (from logs/messages/events.jsonl)
    - New inner monologue entries (from logs/claude_transcript/events.jsonl)
    - New trigger events (from logs/scheduled/, mng_agents/, stop/, monitor/)

    Call this at the start of each conversation turn for situational awareness.
    If it returns "No new context", nothing has changed since the last call.
    """
    agent_data_dir_str = os.environ.get("MNG_AGENT_STATE_DIR", "")
    if not agent_data_dir_str:
        return "No agent data directory configured."

    agent_data_dir = pathlib.Path(agent_data_dir_str)
    if not agent_data_dir.exists():
        return "Agent data directory does not exist."

    sections: list[str] = []
    is_first_call = len(_last_file_sizes) == 0

    # Inner monologue (from logs/claude_transcript/events.jsonl)
    transcript = agent_data_dir / "logs" / "claude_transcript" / "events.jsonl"
    if is_first_call:
        recent = _read_tail_lines(transcript, _MAX_TRANSCRIPT_LINES)
        if recent:
            formatted = _format_events(recent)
            sections.append(f"## Recent Inner Monologue ({len(recent)} entries)\n{formatted}")
    else:
        new_lines = _get_new_lines(transcript)[-_MAX_TRANSCRIPT_LINES:]
        if new_lines:
            formatted = _format_events(new_lines)
            sections.append(f"## New Inner Monologue ({len(new_lines)} entries)\n{formatted}")

    # Messages from other conversations (from logs/messages/events.jsonl)
    messages_file = agent_data_dir / "logs" / "messages" / "events.jsonl"
    current_cid = os.environ.get("LLM_CONVERSATION_ID", "")
    if is_first_call:
        recent_msgs = _read_tail_lines(messages_file, _MAX_MESSAGES_LINES)
        if recent_msgs:
            other_convs: dict[str, list[str]] = {}
            for line in recent_msgs:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    cid = event.get("conversation_id", "")
                    if cid and cid != current_cid:
                        other_convs.setdefault(cid, []).append(line)
                except json.JSONDecodeError as e:
                    print(f"WARNING: malformed JSON in messages events: {e}", file=sys.stderr)
                    continue
            for cid, msgs in other_convs.items():
                recent = msgs[-_MAX_MESSAGES_PER_CONVERSATION:]
                formatted = _format_events(recent)
                sections.append(f"## Conversation {cid} (last {len(recent)} messages)\n{formatted}")
    else:
        new_msg_lines = _get_new_lines(messages_file)[-_MAX_MESSAGES_LINES:]
        if new_msg_lines:
            other_msgs = []
            for line in new_msg_lines:
                try:
                    event = json.loads(line.strip())
                    if event.get("conversation_id", "") != current_cid:
                        other_msgs.append(line)
                except json.JSONDecodeError as e:
                    print(f"WARNING: malformed JSON in new messages: {e}", file=sys.stderr)
                    continue
            if other_msgs:
                formatted = _format_events(other_msgs)
                sections.append(f"## New messages from other conversations ({len(other_msgs)})\n{formatted}")

    # Trigger events from all sources
    for source in ("scheduled", "mng_agents", "stop", "monitor"):
        events_file = agent_data_dir / "logs" / source / "events.jsonl"
        if is_first_call:
            recent = _read_tail_lines(events_file, _MAX_TRIGGER_LINES)
            if recent:
                formatted = _format_events(recent)
                sections.append(f"## Recent {source} events ({len(recent)})\n{formatted}")
        else:
            new_lines = _get_new_lines(events_file)[-_MAX_TRIGGER_LINES:]
            if new_lines:
                formatted = _format_events(new_lines)
                sections.append(f"## New {source} events ({len(new_lines)})\n{formatted}")

    if not sections:
        return "No new context since last call." if not is_first_call else "No context available."

    return "\n\n".join(sections)


def _format_events(lines: list[str]) -> str:
    """Format event JSONL lines into a readable summary.

    NOTE: This function is intentionally duplicated in extra_context_tool.py.
    These files are deployed as standalone scripts and cannot share imports.
    """
    formatted_parts: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            event_type = event.get("type", "?")
            ts = event.get("timestamp", "?")
            if "role" in event and "content" in event:
                content = str(event["content"])[:_MAX_CONTENT_LENGTH]
                cid = event.get("conversation_id", "?")
                formatted_parts.append(f"  [{ts}] [{event.get('role', '?')}@{cid}] {content}")
            elif "data" in event:
                formatted_parts.append(
                    f"  [{ts}] [{event_type}] {json.dumps(event.get('data', {}))[:_MAX_CONTENT_LENGTH]}"
                )
            else:
                formatted_parts.append(f"  [{ts}] [{event_type}] {line[:_MAX_CONTENT_LENGTH]}")
        except json.JSONDecodeError:
            formatted_parts.append(f"  {line[:_MAX_CONTENT_LENGTH]}")
    return "\n".join(formatted_parts)
