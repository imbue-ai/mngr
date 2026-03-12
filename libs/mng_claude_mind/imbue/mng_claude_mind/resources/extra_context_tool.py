"""Extra context gathering tool for mind conversations.

This file is passed to `llm live-chat` via `--functions` and provides
deeper context information beyond what gather_context() returns.

All event data follows the standard envelope format with timestamp, type,
event_id, and source fields. Events are read from events/<source>/events.jsonl.
Conversation metadata is read from the mind_conversations table in the
llm sqlite database at $LLM_USER_PATH/logs.db.

Settings are read from $MNG_AGENT_WORK_DIR/minds.toml.
Missing file or keys fall back to built-in defaults.

NOTE: _format_extra_events() is duplicated in context_tool.py because these
files are deployed as standalone scripts to the agent host via --functions,
where they cannot import from each other or from the mng_claude_mind package.
"""

import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time


def _load_extra_settings() -> dict:
    """Load settings from minds.toml in the agent's work dir.

    NOTE: This is the same logic as _load_settings in context_tool.py,
    renamed to avoid duplicate tool registration by llm --functions.
    """
    try:
        import tomllib
    except ImportError:
        print("WARNING: tomllib not available, using default settings", file=sys.stderr)
        return {}
    work_dir = os.environ.get("MNG_AGENT_WORK_DIR", "")
    if not work_dir:
        return {}
    settings_path = pathlib.Path(work_dir) / "minds.toml"
    if not settings_path.exists():
        return {}
    try:
        with settings_path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError) as e:
        print(f"WARNING: failed to load settings from {settings_path}: {e}", file=sys.stderr)
        return {}


_SETTINGS = _load_extra_settings()
_EXTRA = _SETTINGS.get("chat", {}).get("extra_context", {})


def _get_mng_command() -> list[str]:
    """Return the command for invoking the per-agent mng binary.

    Looks for the mng binary in ``$UV_TOOL_BIN_DIR/mng``. Raises RuntimeError
    if the binary cannot be found.

    NOTE: This is a standalone copy of watcher_common.get_mng_command()
    because this file is deployed to commands/llm_tools/ and cannot import
    from the commands/ directory.
    """
    bin_dir = os.environ.get("UV_TOOL_BIN_DIR", "")
    if not bin_dir:
        raise RuntimeError("UV_TOOL_BIN_DIR is not set. The per-agent mng binary cannot be located without it.")
    mng_bin = os.path.join(bin_dir, "mng")
    if not os.path.isfile(mng_bin):
        raise RuntimeError(
            f"Per-agent mng binary not found at {mng_bin}. "
            "Ensure the mng_recursive plugin is enabled and provisioning completed successfully."
        )
    return [mng_bin]


_MNG_LIST_HARD_TIMEOUT = _EXTRA.get("mng_list_hard_timeout_seconds", 120)
_MNG_LIST_WARN_THRESHOLD = _EXTRA.get("mng_list_warn_threshold_seconds", 15)
_MAX_CONTENT_LENGTH = _EXTRA.get("max_content_length", 300)
_TRANSCRIPT_LINE_COUNT = _EXTRA.get("transcript_line_count", 50)


def gather_extra_context() -> str:
    """Gather extra context including agent status and extended inner monologue history.

    Returns a formatted string with:
    - Current mng agent list (active agents and their states)
    - Extended inner monologue history (from events/transcript/events.jsonl)
    - Full conversation list (from mind_conversations table in the llm DB)

    Use this when you need deeper context than gather_context() provides.
    """
    sections: list[str] = []

    # Current mng agent list
    try:
        start = time.monotonic()
        result = subprocess.run(
            [*_get_mng_command(), "list", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=_MNG_LIST_HARD_TIMEOUT,
        )
        elapsed = time.monotonic() - start
        if elapsed > _MNG_LIST_WARN_THRESHOLD:
            print(
                f"WARNING: mng list took {elapsed:.1f}s (expected <{_MNG_LIST_WARN_THRESHOLD}s)",
                file=sys.stderr,
            )
        if result.returncode == 0 and result.stdout.strip():
            sections.append(f"## Current Agents\n```\n{result.stdout.strip()}\n```")
        else:
            sections.append("## Current Agents\n(No agents or unable to retrieve)")
    except subprocess.TimeoutExpired:
        sections.append(f"## Current Agents\n(Timed out after {_MNG_LIST_HARD_TIMEOUT}s -- mng list may be hanging)")
    except (FileNotFoundError, OSError, RuntimeError):
        sections.append("## Current Agents\n(Unable to retrieve agent list)")

    agent_data_dir_str = os.environ.get("MNG_AGENT_STATE_DIR", "")
    if agent_data_dir_str:
        agent_data_dir = pathlib.Path(agent_data_dir_str)

        # Extended inner monologue (from logs/claude_transcript/events.jsonl)
        transcript = agent_data_dir / "logs" / "claude_transcript" / "events.jsonl"
        if transcript.exists():
            try:
                lines = transcript.read_text().strip().split("\n")
                recent = lines[-_TRANSCRIPT_LINE_COUNT:] if len(lines) > _TRANSCRIPT_LINE_COUNT else lines
                if recent and recent[0]:
                    formatted = _format_extra_events(recent)
                    sections.append(
                        f"## Extended Inner Monologue (last {len(recent)} of {len(lines)} entries)\n{formatted}"
                    )
            except OSError as e:
                print(f"WARNING: failed to read transcript file {transcript}: {e}", file=sys.stderr)

        # Full conversation list (from mind_conversations table in llm DB)
        llm_user_path = os.environ.get("LLM_USER_PATH", "")
        if not llm_user_path:
            sys.stderr.write("WARNING: LLM_USER_PATH not set, skipping conversation list\n")
        else:
            db_path = pathlib.Path(llm_user_path) / "logs.db"
            if db_path.is_file():
                try:
                    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                    try:
                        rows = conn.execute(
                            "SELECT cc.conversation_id, c.model, cc.created_at "
                            "FROM mind_conversations cc "
                            "LEFT JOIN conversations c ON cc.conversation_id = c.id"
                        ).fetchall()
                        if rows:
                            conv_lines = []
                            for conversation_id, model, created_at in rows:
                                conv_lines.append(
                                    f"  {conversation_id}: model={model or '?'}, created={created_at or '?'}"
                                )
                            sections.append("## All Conversations\n" + "\n".join(conv_lines))
                    except sqlite3.Error as e:
                        print(f"WARNING: failed to query mind_conversations: {e}", file=sys.stderr)
                    finally:
                        conn.close()
                except (sqlite3.Error, OSError) as e:
                    print(f"WARNING: failed to open llm database: {e}", file=sys.stderr)

    return "\n\n".join(sections) if sections else "No extra context available."


def _format_extra_events(lines: list[str]) -> str:
    """Format event JSONL lines into a readable summary.

    NOTE: This function is intentionally duplicated in context_tool.py.
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
                conversation_id = event.get("conversation_id", "?")
                formatted_parts.append(f"  [{ts}] [{event.get('role', '?')}@{conversation_id}] {content}")
            elif "data" in event:
                formatted_parts.append(
                    f"  [{ts}] [{event_type}] {json.dumps(event.get('data', {}))[:_MAX_CONTENT_LENGTH]}"
                )
            else:
                formatted_parts.append(f"  [{ts}] [{event_type}] {line[:_MAX_CONTENT_LENGTH]}")
        except json.JSONDecodeError:
            formatted_parts.append(f"  {line[:_MAX_CONTENT_LENGTH]}")
    return "\n".join(formatted_parts)
