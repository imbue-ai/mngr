"""Extra context gathering tool for changeling conversations.

This file is passed to `llm live-chat` via `--functions` and provides
deeper context information beyond what gather_context() returns.

All event data follows the standard envelope format with timestamp, type,
event_id, and source fields. Events are read from logs/<source>/events.jsonl.

Settings are read from $MNG_AGENT_STATE_DIR/settings.toml (provisioned
during agent setup). Missing file or keys fall back to built-in defaults.

NOTE: _format_events() is duplicated in context_tool.py because these
files are deployed as standalone scripts to the agent host via --functions,
where they cannot import from each other or from the mng_claude_zygote package.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _load_settings() -> dict:
    """Load settings from $MNG_AGENT_STATE_DIR/settings.toml.

    NOTE: This function is intentionally duplicated in context_tool.py.
    These files are deployed as standalone scripts and cannot share imports.
    """
    try:
        import tomllib
    except ImportError:
        print("WARNING: tomllib not available, using default settings", file=sys.stderr)
        return {}
    settings_path = Path(os.environ.get("MNG_AGENT_STATE_DIR", "")) / "settings.toml"
    try:
        with settings_path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError) as e:
        print(f"WARNING: failed to load settings from {settings_path}: {e}", file=sys.stderr)
        return {}


_SETTINGS = _load_settings()
_EXTRA = _SETTINGS.get("chat", {}).get("extra_context", {})

_MNG_LIST_HARD_TIMEOUT = _EXTRA.get("mng_list_hard_timeout_seconds", 120)
_MNG_LIST_WARN_THRESHOLD = _EXTRA.get("mng_list_warn_threshold_seconds", 15)
_MAX_CONTENT_LENGTH = _EXTRA.get("max_content_length", 300)
_TRANSCRIPT_LINE_COUNT = _EXTRA.get("transcript_line_count", 50)


def gather_extra_context() -> str:
    """Gather extra context including agent status and extended inner monologue history.

    Returns a formatted string with:
    - Current mng agent list (active agents and their states)
    - Extended inner monologue history (from logs/transcript/events.jsonl)
    - Full conversation list (from logs/conversations/events.jsonl)

    Use this when you need deeper context than gather_context() provides.
    """
    sections: list[str] = []

    # Current mng agent list
    try:
        start = time.monotonic()
        result = subprocess.run(
            ["uv", "run", "mng", "list", "--json"],
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
    except (FileNotFoundError, OSError):
        sections.append("## Current Agents\n(Unable to retrieve agent list)")

    agent_data_dir_str = os.environ.get("MNG_AGENT_STATE_DIR", "")
    if agent_data_dir_str:
        agent_data_dir = Path(agent_data_dir_str)

        # Extended inner monologue (from logs/claude_transcript/events.jsonl)
        transcript = agent_data_dir / "logs" / "claude_transcript" / "events.jsonl"
        if transcript.exists():
            try:
                lines = transcript.read_text().strip().split("\n")
                recent = lines[-_TRANSCRIPT_LINE_COUNT:] if len(lines) > _TRANSCRIPT_LINE_COUNT else lines
                if recent and recent[0]:
                    formatted = _format_events(recent)
                    sections.append(
                        f"## Extended Inner Monologue (last {len(recent)} of {len(lines)} entries)\n{formatted}"
                    )
            except OSError as e:
                print(f"WARNING: failed to read transcript file {transcript}: {e}", file=sys.stderr)

        # Full conversation list (from logs/conversations/events.jsonl)
        conversations_file = agent_data_dir / "logs" / "conversations" / "events.jsonl"
        if conversations_file.exists():
            try:
                lines = conversations_file.read_text().strip().split("\n")
                convs: dict[str, dict[str, str]] = {}
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        cid = event["conversation_id"]
                        convs[cid] = event
                    except (json.JSONDecodeError, KeyError) as e:
                        print(f"WARNING: malformed conversation event: {e}", file=sys.stderr)
                        continue
                if convs:
                    conv_lines = []
                    for cid, event in convs.items():
                        conv_lines.append(
                            f"  {cid}: model={event.get('model', '?')}, created={event.get('timestamp', '?')}"
                        )
                    sections.append("## All Conversations\n" + "\n".join(conv_lines))
            except OSError as e:
                print(f"WARNING: failed to read conversations file {conversations_file}: {e}", file=sys.stderr)

    return "\n\n".join(sections) if sections else "No extra context available."


def _format_events(lines: list[str]) -> str:
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
