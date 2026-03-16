#!/usr/bin/env python3
"""Web server for the ClaudeMindAgent web interface.

Serves a web interface where all views (conversations, terminal) are displayed
in iframes below a persistent navigation header:
- Main page: shows the web chat for the most recent conversation (or conversation list if none)
- Chat page: web-based chat with SSE streaming for real-time responses
- Text Chat page: embeds a specific conversation's ttyd in an iframe (legacy terminal chat)
- Conversations page: lists all conversations with links to open them
- Terminal page: embeds the primary agent terminal in an iframe
- All Agents page: lists agents on this host with their states

The web chat uses SSE (Server-Sent Events) for streaming LLM responses and
receives messages via POST requests from the frontend (plain JavaScript).
It uses the llm library for calling LLMs and storing results.

The text chat (legacy) uses companion ttyd processes for terminal-based chat.

Environment:
    MNG_AGENT_STATE_DIR  - Agent state directory (contains events/)
    MNG_AGENT_NAME       - This agent's name
    MNG_HOST_NAME        - Name of the host this agent runs on
    MNG_AGENT_WORK_DIR   - Agent work directory (contains minds.toml)
    LLM_USER_PATH        - LLM data directory (contains logs.db)
"""

import hashlib
import html
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime
from datetime import timezone
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from typing import Final
from urllib.parse import parse_qs
from urllib.parse import urlparse

from imbue.mng_recursive.watcher_common import MngNotInstalledError
from imbue.mng_recursive.watcher_common import get_mng_command

# -- Environment and paths --

AGENT_STATE_DIR: Final[str] = os.environ.get("MNG_AGENT_STATE_DIR", "")
AGENT_NAME: Final[str] = os.environ.get("MNG_AGENT_NAME", "")
HOST_NAME: Final[str] = os.environ.get("MNG_HOST_NAME", "")

SERVERS_JSONL_PATH: Final[Path | None] = (
    Path(AGENT_STATE_DIR) / "events" / "servers" / "events.jsonl" if AGENT_STATE_DIR else None
)
MESSAGES_EVENTS_PATH: Final[Path | None] = (
    Path(AGENT_STATE_DIR) / "events" / "messages" / "events.jsonl" if AGENT_STATE_DIR else None
)
_LLM_USER_PATH: Final[str] = os.environ.get("LLM_USER_PATH", "")
LLM_DB_PATH: Final[Path | None] = Path(_LLM_USER_PATH) / "logs.db" if _LLM_USER_PATH else None
if not _LLM_USER_PATH:
    sys.stderr.write("[web-server] WARNING: LLM_USER_PATH not set, conversation features will be unavailable\n")

AGENT_WORK_DIR: Final[str] = os.environ.get("MNG_AGENT_WORK_DIR", "")

# -- Constants --

WEB_SERVER_NAME: Final[str] = "web"
AGENT_LIST_POLL_INTERVAL_SECONDS: Final[int] = 30

# -- Global state (protected by locks) --

_agent_list_lock = threading.Lock()
_cached_agents: list[dict[str, object]] = []

_is_shutting_down = False


# -- Utility functions --


def _html_escape(text: str) -> str:
    return html.escape(text, quote=True)


def _log(message: str) -> None:
    sys.stderr.write(f"[web-server] {message}\n")
    sys.stderr.flush()


# -- Server registration --


def _make_event_id(data: str) -> str:
    """Generate a deterministic event ID from content."""
    return "evt-" + hashlib.sha256(data.encode()).hexdigest()[:32]


def _iso_timestamp() -> str:
    """Return the current UTC time as an ISO 8601 timestamp with nanosecond precision."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond * 1000:09d}Z"


def _register_server(server_name: str, port: int) -> None:
    """Append a server record to servers/events.jsonl with proper event envelope fields."""
    if SERVERS_JSONL_PATH is None:
        return
    SERVERS_JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    url = f"http://127.0.0.1:{port}"
    record = json.dumps(
        {
            "timestamp": _iso_timestamp(),
            "type": "server_registered",
            "event_id": _make_event_id(f"{server_name}:{url}"),
            "source": "servers",
            "server": server_name,
            "url": url,
        }
    )
    with open(SERVERS_JSONL_PATH, "a") as f:
        f.write(record + "\n")


# -- Conversation reading --


def _read_conversations() -> list[dict[str, str]]:
    """Read conversations from the mind_conversations table and return sorted by most recent activity."""
    conversations_by_id: dict[str, dict[str, str]] = {}

    # Read conversations from the llm database
    if LLM_DB_PATH and LLM_DB_PATH.is_file():
        try:
            conn = sqlite3.connect(f"file:{LLM_DB_PATH}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT cc.conversation_id, c.model, cc.created_at, cc.tags "
                    "FROM mind_conversations cc "
                    "LEFT JOIN conversations c ON cc.conversation_id = c.id"
                ).fetchall()
                for conversation_id, model, created_at, tags_json in rows:
                    tags = json.loads(tags_json) if tags_json else {}
                    conversations_by_id[conversation_id] = {
                        "conversation_id": conversation_id,
                        "name": tags.get("name", ""),
                        "model": model or "unknown",
                        "created_at": created_at or "",
                        "updated_at": created_at or "",
                    }
            except sqlite3.Error as e:
                _log(f"Failed to query mind_conversations: {e}")
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"Failed to open llm database: {e}")

    # Update with latest message timestamps
    if MESSAGES_EVENTS_PATH and MESSAGES_EVENTS_PATH.exists():
        for line in MESSAGES_EVENTS_PATH.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
                conversation_id = message.get("conversation_id", "")
                ts = message.get("timestamp", "")
                if conversation_id and ts and conversation_id in conversations_by_id:
                    if ts > conversations_by_id[conversation_id]["updated_at"]:
                        conversations_by_id[conversation_id]["updated_at"] = ts
            except json.JSONDecodeError as e:
                _log(f"Skipping malformed message event line: {e}")
                continue

    # Sort by most recently updated first
    return sorted(
        conversations_by_id.values(),
        key=lambda c: c.get("updated_at", ""),
        reverse=True,
    )


# -- Agent list polling --


def _poll_agent_list_forever() -> None:
    """Background thread: periodically run mng list --format json and cache results."""
    global _cached_agents
    while not _is_shutting_down:
        try:
            result = subprocess.run(
                [*get_mng_command(), "list", "--format", "json", "--quiet"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                _log(f"mng list failed (exit {result.returncode}): {result.stderr.strip()}")
            elif result.stdout.strip():
                try:
                    data = json.loads(result.stdout)
                    agents_raw = data.get("agents", [])

                    # Filter to current host if possible
                    if HOST_NAME:
                        agents_raw = [a for a in agents_raw if a.get("host", {}).get("name", "") == HOST_NAME]

                    with _agent_list_lock:
                        _cached_agents = agents_raw
                except json.JSONDecodeError as e:
                    _log(f"Failed to parse mng list JSON output: {e}")
        except subprocess.TimeoutExpired:
            _log("mng list timed out")
        except (FileNotFoundError, MngNotInstalledError):
            _log("mng not found, cannot poll agent list")
        except OSError as e:
            _log(f"Failed to poll agent list: {e}")

        # Sleep in small increments to allow clean shutdown
        for _ in range(AGENT_LIST_POLL_INTERVAL_SECONDS):
            if _is_shutting_down:
                return
            time.sleep(1)


# -- Page rendering --

_CSS: Final[str] = """
    * { margin: 0; padding: 0; box-sizing: border-box; }
    html, body { height: 100%; font-family: system-ui, -apple-system, sans-serif; background: rgb(245, 245, 245); }
    .header {
      display: flex; align-items: center; gap: 8px;
      padding: 4px 12px; background: inherit; color: rgb(51, 51, 51);
      height: 40px;
    }
    .header h1 { font-size: 14px; font-weight: 500; }
    .header-spacer { flex: 1; }
    .header a {
      color: rgb(130, 130, 130); text-decoration: none; font-size: 14px;
      padding: 6px; border-radius: 6px; border: none;
      display: inline-flex; align-items: center; justify-content: center;
    }
    .header a:hover { background: rgb(230, 230, 230); color: rgb(51, 51, 51); }
    .header a.active { color: rgb(51, 51, 51); }
    .header a svg { width: 18px; height: 18px; }
    .content { padding: 24px; max-width: 800px; }
    .iframe-container { flex: 1; }
    .iframe-container iframe { width: 100%; height: 100%; border: none; }
    .iframe-layout { display: flex; flex-direction: column; height: 100%; }
    .item-list { list-style: none; margin-top: 16px; }
    .item {
      padding: 12px 16px; background: white; border: 1px solid rgb(221, 221, 221);
      border-radius: 6px; margin-bottom: 8px;
      display: flex; align-items: center; justify-content: space-between;
    }
    .item-info { display: flex; align-items: center; gap: 8px; }
    .item-name { font-weight: 600; font-size: 15px; }
    .item-detail { font-size: 13px; color: #666; }
    .badge {
      font-size: 13px; padding: 2px 8px; border-radius: 4px; background: #e8e8e8;
    }
    .badge.running { background: #d4edda; color: #155724; }
    .badge.stopped { background: #f8d7da; color: #721c24; }
    .badge.waiting { background: #fff3cd; color: #856404; }
    .link-btn {
      display: inline-block; padding: 6px 14px; background: rgb(51, 51, 51);
      color: white; text-decoration: none; border-radius: 4px; font-size: 14px;
    }
    .link-btn:hover { background: rgb(80, 80, 80); }
    .link-btn.disabled { opacity: 0.5; pointer-events: none; }
    .link-btn.new { background: rgb(34, 120, 60); }
    .link-btn.new:hover { background: rgb(40, 150, 70); }
    .empty-state { color: #666; font-size: 15px; margin-top: 16px; }
    .icon-btn {
      width: 32px; height: 32px; padding: 0; background: none; color: rgb(130, 130, 130);
      border: none; border-radius: 50%; cursor: pointer;
      display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0;
    }
    .icon-btn:hover { background: rgb(230, 230, 230); color: rgb(51, 51, 51); }
    .icon-btn:disabled { opacity: 0.35; cursor: not-allowed; }
    .icon-btn:disabled:hover { background: none; }
    .icon-btn svg { width: 18px; height: 18px; }
    .icon-btn.active { color: rgb(34, 120, 60); }
    .icon-btn.active:hover { color: rgb(40, 150, 70); }
    .conv-picker { position: relative; }
    .conv-picker-btn {
      display: inline-flex; align-items: center; gap: 6px;
      background: none; border: none; cursor: pointer; padding: 4px 8px;
      font-size: 14px; font-weight: 500; color: rgb(51, 51, 51);
      font-family: inherit; border-radius: 6px;
    }
    .conv-picker-btn:hover { background: rgb(230, 230, 230); }
    .conv-picker-btn svg { width: 14px; height: 14px; color: rgb(130, 130, 130); }
    .conv-picker-menu {
      display: none; position: absolute; top: 100%; left: 0; margin-top: 4px;
      background: white; border: 1px solid rgb(215, 215, 215); border-radius: 8px;
      box-shadow: 0 4px 12px rgba(0,0,0,0.1); min-width: 240px; max-width: 320px;
      max-height: 320px; overflow-y: auto; z-index: 100;
    }
    .conv-picker-menu.open { display: block; }
    .conv-picker-item {
      display: block; width: 100%; padding: 8px 12px; font-size: 13px;
      color: rgb(51, 51, 51); text-decoration: none; border: none; background: none;
      text-align: left; cursor: pointer; font-family: inherit;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .conv-picker-item:first-child { border-radius: 8px 8px 0 0; }
    .conv-picker-item:last-child { border-radius: 0 0 8px 8px; }
    .conv-picker-item:hover { background: rgb(245, 245, 245); }
    .conv-picker-item.active { font-weight: 600; }
    .conv-picker-item.new-conv {
      border-top: 1px solid rgb(230, 230, 230); color: rgb(100, 100, 100);
    }
    .app-layout { display: flex; height: 100%; }
    .sidebar {
      width: 48px; display: flex; flex-direction: column;
      padding: 8px 6px; gap: 4px; background: inherit; flex-shrink: 0;
      border-right: 1px solid rgb(230, 230, 230); transition: width 0.15s ease;
      overflow: hidden;
    }
    .sidebar.expanded { width: 220px; padding: 8px 10px; }
    .sidebar-top {
      display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
      width: 100%; flex-shrink: 0;
    }
    .sidebar-toggle {
      width: 36px; height: 36px; padding: 0; background: none; color: rgb(130, 130, 130);
      border: none; border-radius: 8px; cursor: pointer;
      display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0;
    }
    .sidebar-toggle:hover { background: rgb(230, 230, 230); color: rgb(51, 51, 51); }
    .sidebar-toggle svg { width: 20px; height: 20px; }
    .sidebar-agent-name {
      display: none; font-size: 15px; font-weight: 600; color: rgb(51, 51, 51);
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .sidebar.expanded .sidebar-agent-name { display: block; }
    .sidebar-link {
      width: 36px; height: 36px; padding: 0; color: rgb(130, 130, 130);
      text-decoration: none; border-radius: 8px;
      display: flex; align-items: center; justify-content: center; flex-shrink: 0;
      white-space: nowrap; overflow: hidden; font-family: inherit; font-size: 14px;
    }
    .sidebar-link:hover { background: rgb(230, 230, 230); color: rgb(51, 51, 51); }
    .sidebar-link.active { color: rgb(51, 51, 51); }
    .sidebar-link svg { width: 20px; height: 20px; flex-shrink: 0; }
    .sidebar.expanded .sidebar-link {
      width: 100%; justify-content: flex-start; padding: 0 8px; gap: 10px;
    }
    .sidebar-link-label { display: none; }
    .sidebar.expanded .sidebar-link-label { display: inline; }
    .app-main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
"""


_ICON_SIDEBAR: Final[str] = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
    ' stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="3" width="18" height="18" rx="2"/>'
    '<line x1="9" y1="3" x2="9" y2="21"/></svg>'
)

_ICON_CONVERSATIONS: Final[str] = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
    ' stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>'
)
_ICON_TERMINAL: Final[str] = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
    ' stroke-linecap="round" stroke-linejoin="round">'
    '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>'
)
_ICON_AGENTS: Final[str] = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
    ' stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>'
    '<circle cx="9" cy="7" r="4"/>'
    '<path d="M23 21v-2a4 4 0 0 0-3-3.87"/>'
    '<path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>'
)


def _sidebar_link(href: str, icon: str, label: str, key: str, active: str) -> str:
    """Render a single sidebar navigation link."""
    cls = "sidebar-link active" if key == active else "sidebar-link"
    return f'<a class="{cls}" href="{href}" title="{label}">{icon}<span class="sidebar-link-label">{label}</span></a>'


def _render_sidebar(active: str = "", agent_name: str = "") -> str:
    """Render a collapsible icon sidebar with agent name in the top row."""
    escaped_name = _html_escape(agent_name) if agent_name else ""
    return (
        '<nav class="sidebar" id="sidebar">'
        '<div class="sidebar-top">'
        f'<button class="sidebar-toggle" id="sidebar-toggle" title="Toggle sidebar"'
        f" onclick=\"document.getElementById('sidebar').classList.toggle('expanded')\">"
        f"{_ICON_SIDEBAR}</button>"
        f'<span class="sidebar-agent-name">{escaped_name}</span>'
        "</div>"
        + _sidebar_link("conversations", _ICON_CONVERSATIONS, "Conversations", "conversations", active)
        + _sidebar_link("terminal", _ICON_TERMINAL, "Terminal", "terminal", active)
        + _sidebar_link("agents-page", _ICON_AGENTS, "Agents", "agents", active)
        + "</nav>"
    )


def _render_header(
    agent_name: str,
    active: str = "",
    extra_right: str = "",
    left_content: str = "",
    show_nav: bool = True,
) -> str:
    """Render the common header bar with navigation icon links.

    left_content replaces the default ``<h1>`` when provided (e.g. a
    conversation dropdown).  extra_right is raw HTML inserted between
    the spacer and the nav icons.  Set show_nav=False when a sidebar
    already provides navigation.
    """

    def _nav_link(href: str, icon: str, title: str, key: str) -> str:
        cls = ' class="active"' if key == active else ""
        return f'<a{cls} href="{href}" title="{title}">{icon}</a>'

    left = left_content or f"<h1>{agent_name}</h1>"

    nav = ""
    if show_nav:
        nav = (
            _nav_link("conversations", _ICON_CONVERSATIONS, "Conversations", "conversations")
            + _nav_link("terminal", _ICON_TERMINAL, "Terminal", "terminal")
            + _nav_link("agents-page", _ICON_AGENTS, "Agents", "agents")
        )

    return '<div class="header">' + left + '<div class="header-spacer"></div>' + extra_right + nav + "</div>"


def _render_iframe_page(agent_name: str, title: str, iframe_src: str, active: str = "") -> str:
    """Render a full-height page with header and an iframe filling the remaining space."""
    escaped_title = _html_escape(title)
    return f"""<!DOCTYPE html>
<html>
<head><title>{escaped_title} - {agent_name}</title><style>{_CSS}</style></head>
<body class="iframe-layout">
  {_render_header(agent_name, active=active)}
  <div class="iframe-container">
    <iframe src="{_html_escape(iframe_src)}"></iframe>
  </div>
</body>
</html>"""


def _render_conversations_page() -> str:
    """Render the conversations page with conversation links (server-side)."""
    agent_name = _html_escape(AGENT_NAME or "Agent")
    conversations = _read_conversations()

    conv_items = ""
    for conv in conversations:
        conversation_id = _html_escape(conv["conversation_id"])
        name = _html_escape(conv.get("name", "")) or conversation_id
        model = _html_escape(conv.get("model", ""))
        updated = _html_escape(conv.get("updated_at", ""))
        detail = conversation_id
        if model:
            detail += f" -- {model}"
        if updated:
            detail += f" -- {updated}"
        conv_items += (
            f'<li class="item">'
            f'<div class="item-info">'
            f'<span class="item-name">{name}</span>'
            f'<span class="item-detail">{detail}</span>'
            f"</div>"
            f'<div style="display:flex;gap:6px;">'
            f'<a class="link-btn" href="chat?cid={conversation_id}">Chat</a>'
            f'<a class="link-btn" href="text_chat?cid={conversation_id}" '
            f'style="background:rgb(80,80,100);">Terminal</a>'
            f"</div>"
            f"</li>\n"
        )

    empty_section = ""
    if not conversations:
        empty_section = '<p class="empty-state">No conversations yet.</p>'

    return f"""<!DOCTYPE html>
<html>
<head><title>{agent_name}</title><style>{_CSS}</style></head>
<body>
  {_render_header(agent_name, active="conversations")}
  <div class="content">
    <a class="link-btn new" href="chat?cid=NEW">+ New Conversation</a>
    {empty_section}
    <ul class="item-list">{conv_items}</ul>
  </div>
</body>
</html>"""


def _render_agents_page() -> str:
    """Render the agents page listing agents on this host (server-side)."""
    agent_name = _html_escape(AGENT_NAME or "Agent")

    with _agent_list_lock:
        agents = list(_cached_agents)

    agent_items = ""
    for agent in agents:
        name = _html_escape(str(agent.get("name", "unnamed")))
        state = str(agent.get("state", "unknown")).lower()
        state_escaped = _html_escape(state.upper())

        agent_items += (
            f'<li class="item">'
            f'<div class="item-info">'
            f'<span class="item-name">{name}</span>'
            f'<span class="badge {_html_escape(state)}">{state_escaped}</span>'
            f"</div>"
            f"</li>\n"
        )

    empty_section = ""
    if not agents:
        empty_section = '<p class="empty-state">No agents found on this host.</p>'

    return f"""<!DOCTYPE html>
<html>
<head><title>All Agents - {agent_name}</title><style>{_CSS}</style></head>
<body>
  {_render_header(agent_name, active="agents")}
  <div class="content">
    {empty_section}
    <ul class="item-list">{agent_items}</ul>
  </div>
</body>
</html>"""


def _get_most_recent_conversation_id() -> str | None:
    """Return the conversation ID of the most recent conversation, or None if none exist."""
    conversations = _read_conversations()
    if not conversations:
        return None
    return conversations[0]["conversation_id"]


# -- LLM chat support --


def _get_default_chat_model() -> str:
    """Read the default chat model from minds.toml, falling back to claude-opus-4.6."""
    if not AGENT_WORK_DIR:
        return "claude-opus-4.6"
    settings_path = Path(AGENT_WORK_DIR) / "minds.toml"
    try:
        if settings_path.exists():
            raw = tomllib.loads(settings_path.read_text())
            model = raw.get("chat", {}).get("model")
            if model:
                return str(model)
    except (OSError, tomllib.TOMLDecodeError) as e:
        _log(f"Failed to load chat model from settings: {e}")
    return "claude-opus-4.6"


def _build_template() -> str | None:
    """Build an llm template file from GLOBAL.md and talking/PROMPT.md.

    Writes the template atomically (write to tmp, then rename) to
    ``$MNG_AGENT_STATE_DIR/plugin/llm/template.yml``, matching the
    approach used by chat.sh.

    Returns the template file path on success, or None if no system prompt
    files were found.
    """
    if not AGENT_STATE_DIR:
        return None

    parts: list[str] = []
    if AGENT_WORK_DIR:
        global_md = Path(AGENT_WORK_DIR) / "GLOBAL.md"
        if global_md.is_file():
            try:
                parts.append(global_md.read_text())
            except OSError as e:
                _log(f"Failed to read GLOBAL.md: {e}")
        talking_prompt = Path(AGENT_WORK_DIR) / "talking" / "PROMPT.md"
        if talking_prompt.is_file():
            try:
                parts.append(talking_prompt.read_text())
            except OSError as e:
                _log(f"Failed to read talking/PROMPT.md: {e}")

    if not parts:
        return None

    system_prompt = "\n\n".join(parts)
    template_dir = Path(AGENT_STATE_DIR) / "plugin" / "llm"
    template_path = template_dir / "template.yml"
    tmp_path = template_dir / "template.yml.tmp"

    try:
        template_dir.mkdir(parents=True, exist_ok=True)
        indented = "\n".join("  " + line for line in system_prompt.splitlines())
        tmp_path.write_text(f"system: |\n{indented}\n")
        tmp_path.rename(template_path)
        _log(f"Built template at {template_path} ({len(system_prompt)} chars)")
        return str(template_path)
    except OSError as e:
        _log(f"Failed to write template file: {e}")
        return None


def _read_message_history(conversation_id: str) -> list[dict[str, str]]:
    """Read message history for a conversation from the llm database."""
    if not LLM_DB_PATH or not LLM_DB_PATH.is_file():
        return []
    messages: list[dict[str, str]] = []
    try:
        conn = sqlite3.connect(f"file:{LLM_DB_PATH}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT prompt, response, datetime_utc FROM responses "
                "WHERE conversation_id = ? ORDER BY datetime_utc ASC",
                (conversation_id,),
            ).fetchall()
            for prompt, response, ts in rows:
                if prompt and prompt != "...":
                    messages.append({"role": "user", "content": prompt, "timestamp": ts or ""})
                if response and response.strip():
                    messages.append({"role": "assistant", "content": response, "timestamp": ts or ""})
        except sqlite3.Error as e:
            _log(f"Failed to read message history: {e}")
        finally:
            conn.close()
    except sqlite3.Error as e:
        _log(f"Failed to open database for message history: {e}")
    return messages


_NEW_CONVERSATION_GREETING: Final[str] = (
    "Hi, I'm Selene. Welcome to the future!\n"
    "\n"
    "> You can interrupt at any time if you want to focus on something else\n"
    "\n"
    "Is it ok if I get to know you a little bit?\n"
    "\n"
    "> This simply generates a document for you to review (to save you time)\n"
    "> \n"
    "> None of your data ever leaves your device. [Learn more](https://imbue.com/help/) about why Imbue is the best option for privacy and security\n"
)


def _create_greeting_conversation() -> str | None:
    """Create a new conversation with a greeting message via ``llm inject``.

    Returns the conversation ID on success, or None on failure.
    """
    from imbue.concurrency_group.concurrency_group import ConcurrencyGroup

    model_id = _get_default_chat_model()
    cmd = ["llm", "inject", "-m", model_id, "--prompt", "", _NEW_CONVERSATION_GREETING]

    env = dict(os.environ)
    if _LLM_USER_PATH:
        env["LLM_USER_PATH"] = _LLM_USER_PATH

    _log(f"[new-conv] creating greeting conversation: model={model_id}")
    try:
        with ConcurrencyGroup(name="web-new-conv") as cg:
            result = cg.run_process_to_completion(cmd, timeout=30.0, is_checked_after=False, env=env)
    except FileNotFoundError:
        _log("[new-conv] llm command not found")
        return None

    if result.returncode != 0:
        _log(f"[new-conv] llm inject failed (exit {result.returncode}): {result.stderr.strip()}")
        return None

    # Parse conversation ID from output like "Injected message into conversation <id>"
    stdout = result.stdout.strip()
    parts = stdout.rsplit(" ", 1)
    if len(parts) == 2:
        conversation_id = parts[1]
        _log(f"[new-conv] created conversation: {conversation_id}")
        _register_conversation(conversation_id)
        return conversation_id

    _log(f"[new-conv] could not parse conversation ID from: {stdout}")
    return None


def _register_conversation(conversation_id: str) -> None:
    """Register a conversation in the mind_conversations table.

    Creates the table if it doesn't exist, and inserts the conversation
    (ignoring if it already exists).
    """
    if not LLM_DB_PATH:
        return
    try:
        conn = sqlite3.connect(str(LLM_DB_PATH))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mind_conversations ("
                "conversation_id TEXT PRIMARY KEY, "
                "tags TEXT NOT NULL DEFAULT '{}', "
                "created_at TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO mind_conversations (conversation_id, tags, created_at) VALUES (?, ?, ?)",
                (conversation_id, '{"name":"(new chat)"}', _iso_timestamp()),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        _log(f"Failed to register conversation {conversation_id}: {e}")


class _SseOutputCallback:
    """Callable that streams stdout lines as SSE chunk events and collects them."""

    def __init__(self, wfile: Any, lines: list[str]) -> None:
        self.wfile = wfile
        self.lines = lines
        self.write_failed = False

    def __call__(self, line: str, is_stdout: bool) -> None:
        if not is_stdout:
            _log(f"[chat-stream] stderr line: {line.rstrip()}")
            return
        self.lines.append(line)
        _log(f"[chat-stream] got stdout line ({len(line)} chars): {line[:80].rstrip()!r}")
        chunk_data = json.dumps({"chunk": line})
        try:
            self.wfile.write(f"event: chunk\ndata: {chunk_data}\n\n".encode())
            self.wfile.flush()
            _log("[chat-stream] SSE chunk written and flushed")
        except OSError as e:
            _log(f"[chat-stream] SSE write failed (client may have disconnected): {e}")
            self.write_failed = True


def _get_max_response_rowid() -> int:
    """Return the current max rowid in the responses table, or 0 if empty/missing."""
    if not LLM_DB_PATH or not LLM_DB_PATH.is_file():
        return 0
    try:
        conn = sqlite3.connect(f"file:{LLM_DB_PATH}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT MAX(rowid) FROM responses").fetchone()
            return row[0] or 0 if row else 0
        finally:
            conn.close()
    except sqlite3.Error as e:
        _log(f"Failed to query max response rowid: {e}")
        return 0


def _find_conversation_id_after_rowid(after_rowid: int) -> str | None:
    """Find the conversation_id of the first response inserted after the given rowid."""
    if not LLM_DB_PATH or not LLM_DB_PATH.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{LLM_DB_PATH}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT conversation_id FROM responses WHERE rowid > ? ORDER BY rowid ASC LIMIT 1",
                (after_rowid,),
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except sqlite3.Error as e:
        _log(f"Failed to find conversation after rowid {after_rowid}: {e}")
        return None


def _handle_chat_send(conversation_id: str, message: str, wfile: Any) -> None:
    """Send a message to the LLM and stream the response via SSE.

    Runs ``llm prompt`` via ConcurrencyGroup with an on_output callback that
    sends each stdout line as an SSE "chunk" event. Line-buffered: chunks are
    sent per-line as the LLM produces newlines in its output.
    """
    from imbue.concurrency_group.concurrency_group import ConcurrencyGroup

    is_new_conversation = conversation_id == "NEW"
    model_id = _get_default_chat_model()

    # Snapshot the max response rowid before running llm so we can find
    # exactly which conversation was created by this request (race-safe).
    rowid_before = _get_max_response_rowid() if is_new_conversation else 0

    cmd = ["stdbuf", "-oL", "llm", "prompt", "-m", model_id]

    # Pass --cid to continue an existing conversation. For new conversations
    # (cid=NEW), omit --cid so llm creates a new one; we discover its real ID
    # after the prompt completes.
    if not is_new_conversation:
        cmd.extend(["--cid", conversation_id])

    template_path = _build_template()
    if template_path:
        cmd.extend(["-t", template_path])

    cmd.append(message)

    env = dict(os.environ)
    if _LLM_USER_PATH:
        env["LLM_USER_PATH"] = _LLM_USER_PATH
    env["PYTHONUNBUFFERED"] = "1"

    _log(f"[chat-stream] starting llm prompt: cid={conversation_id} model={model_id}")
    _log(f"[chat-stream] cmd={cmd}")

    collected_lines: list[str] = []
    callback = _SseOutputCallback(wfile, collected_lines)

    try:
        with ConcurrencyGroup(name="web-chat-send") as cg:
            _log("[chat-stream] ConcurrencyGroup created, calling run_process_to_completion")
            result = cg.run_process_to_completion(
                cmd,
                timeout=300.0,
                is_checked_after=False,
                on_output=callback,
                env=env,
            )
        _log(
            f"[chat-stream] process finished: returncode={result.returncode} "
            f"stdout_len={len(result.stdout)} stderr_len={len(result.stderr)} "
            f"callback_lines={len(collected_lines)} timed_out={result.is_timed_out}"
        )
    except FileNotFoundError as e:
        _log(f"[chat-stream] command not found: {e}")
        error_data = json.dumps({"error": f"command not found: {e}"})
        wfile.write(f"event: error\ndata: {error_data}\n\n".encode())
        wfile.flush()
        return

    if result.returncode != 0:
        stderr_text = result.stderr[:500] if result.stderr else "(empty)"
        _log(f"[chat-stream] llm prompt failed (exit {result.returncode}): {stderr_text}")
        error_data = json.dumps({"error": f"LLM failed (exit {result.returncode}): {result.stderr[:200]}"})
        try:
            wfile.write(f"event: error\ndata: {error_data}\n\n".encode())
            wfile.flush()
        except OSError as e:
            _log(f"[chat-stream] SSE error write failed: {e}")
        return

    # If lines came via the callback they were already streamed. If the callback
    # missed some (e.g. incomplete trailing line), send the full stdout as a
    # final chunk so the client gets the complete text.
    full_text_from_callback = "".join(collected_lines)
    full_text_from_result = result.stdout
    _log(
        f"[chat-stream] callback_text_len={len(full_text_from_callback)} "
        f"result_stdout_len={len(full_text_from_result)}"
    )

    if len(full_text_from_result) > len(full_text_from_callback):
        remainder = full_text_from_result[len(full_text_from_callback) :]
        _log(f"[chat-stream] sending remainder chunk ({len(remainder)} chars)")
        chunk_data = json.dumps({"chunk": remainder})
        try:
            wfile.write(f"event: chunk\ndata: {chunk_data}\n\n".encode())
            wfile.flush()
        except OSError as e:
            _log(f"[chat-stream] SSE remainder write failed: {e}")

    # For new conversations, discover the real conversation_id that llm created
    # and register it in mind_conversations so it appears in the list and
    # persists across reloads.
    if is_new_conversation and result.returncode == 0:
        real_cid = _find_conversation_id_after_rowid(rowid_before)
        if real_cid:
            _log(f"[chat-stream] new conversation created by llm: {real_cid}")
            conversation_id = real_cid
            _register_conversation(real_cid)
        else:
            _log("[chat-stream] WARNING: llm prompt succeeded but no new response found in database")

    done_data = json.dumps({"conversation_id": conversation_id, "full_text": full_text_from_result})
    _log("[chat-stream] sending done event")
    try:
        wfile.write(f"event: done\ndata: {done_data}\n\n".encode())
        wfile.flush()
        _log("[chat-stream] done event sent successfully")
    except OSError as e:
        _log(f"[chat-stream] SSE done write failed: {e}")


# -- Web chat page rendering --


_CHAT_CSS: Final[str] = """
    .chat-layout { display: flex; flex-direction: column; height: 100%; font-family: 'Nunito', sans-serif; }
    .chat-messages {
      flex: 1; overflow-y: auto; max-width: 800px;
      margin: 0 auto; width: 100%;
    }
    .message { margin-bottom: 16px; display: flex; flex-direction: column; }
    .message.user { align-items: flex-end; }
    .message.assistant { align-items: flex-start; }
    .message-bubble {
      padding: 10px 14px; border-radius: 12px;
      font-size: 12px; line-height: 1.5; white-space: pre-wrap; word-wrap: break-word;
    }
    .message.user .message-bubble {
      background: rgb(235, 233, 228); color: rgb(51, 51, 51); border-bottom-right-radius: 4px;
    }
    .message.assistant .message-bubble {
      background: transparent; color: rgb(51, 51, 51); border-bottom-left-radius: 4px;
      font-family: 'Crimson Text', serif; font-size: 16px; width: 100%;
    }
    .message-bubble blockquote {
      background: rgb(235, 235, 235); border-radius: 10px; border: none;
      padding: 10px 14px; margin: 8px 0; color: rgb(100, 100, 100);
      font-family: 'Crimson Text', serif; font-size: 13px;
    }
    .message-bubble a { color: inherit; text-decoration: underline; }
    .message-bubble a:hover { opacity: 0.7; }
    .chat-input-area {
      background: inherit; padding: 8px 16px 12px;
    }
    .chat-input-container {
      max-width: 800px; margin: 0 auto; position: relative;
    }
    .chat-input-container textarea {
      width: 100%; padding: 12px 48px 12px 14px; border: 1px solid rgb(215, 215, 215);
      border-radius: 18px; font-size: 14px; font-family: inherit; resize: none;
      outline: none; min-height: 72px; max-height: 160px; line-height: 1.4; background: white;
    }
    .chat-input-container textarea:focus { border-color: rgb(160, 160, 160); }
    .chat-input-container .input-btn-left {
      position: absolute; left: 8px; bottom: 8px;
    }
    .chat-input-container .input-btn-right {
      position: absolute; right: 8px; bottom: 8px;
    }
    .streaming-indicator { font-size: 12px; color: rgb(153, 153, 153); padding: 4px 0; text-align: left; }
"""


def _render_web_chat_page(agent_name: str, conversation_id: str) -> str:
    """Render the web-based chat page with SSE streaming support."""
    escaped_agent = _html_escape(agent_name)
    # Use json.dumps for safe embedding in JavaScript string context
    # (html.escape is insufficient inside <script> tags).
    # Also escape </ to prevent premature script tag closing.
    js_safe_cid = json.dumps(conversation_id).replace("</", r"<\/")

    _chevron_svg = (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
        ' stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>'
    )
    conv_dropdown = (
        '<div class="conv-picker">'
        f'<button class="conv-picker-btn" id="conv-picker-btn" onclick="toggleConvMenu()">'
        f'<span id="conv-picker-label">Conversation</span>{_chevron_svg}</button>'
        '<div class="conv-picker-menu" id="conv-picker-menu"></div>'
        "</div>"
    )
    audio_btn = (
        '<button id="audio-btn" class="icon-btn" onclick="alert(\'Not implemented\')" title="Toggle audio">'
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
        ' stroke-linecap="round" stroke-linejoin="round">'
        '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/>'
        '<path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>'
        '<path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg></button>'
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<title>Chat - {escaped_agent}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Crimson+Text:ital,wght@0,400;0,600;1,400&family=Nunito:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
{_CSS}
{_CHAT_CSS}
</style>
</head>
<body class="app-layout">
  {_render_sidebar(active="conversations", agent_name=agent_name)}
  <div class="app-main chat-layout">
    {_render_header(agent_name, extra_right=audio_btn, left_content=conv_dropdown, show_nav=False)}
    <div class="chat-messages" id="messages"></div>
    <div class="chat-input-area">
      <div class="chat-input-container">
        <button class="icon-btn input-btn-left" onclick="alert('Coming soon!')" title="Attach">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
          </svg>
        </button>
        <textarea id="chat-input" placeholder="Reply..." rows="3"></textarea>
        <button class="icon-btn input-btn-right" onclick="alert('Coming soon!')" title="Voice input">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
            <path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/>
            <line x1="8" y1="23" x2="16" y2="23"/>
          </svg>
        </button>
      </div>
    </div>
  </div>
<script>
var conversationId = {js_safe_cid};
var isStreaming = false;

// -- Conversation picker --
function toggleConvMenu() {{
  document.getElementById("conv-picker-menu").classList.toggle("open");
}}
// Close menu when clicking outside
document.addEventListener("click", function(e) {{
  var picker = document.querySelector(".conv-picker");
  if (picker && !picker.contains(e.target)) {{
    document.getElementById("conv-picker-menu").classList.remove("open");
  }}
}});
function loadConversations() {{
  fetch("api/conversations").then(function(r) {{ return r.json(); }}).then(function(data) {{
    var menu = document.getElementById("conv-picker-menu");
    var label = document.getElementById("conv-picker-label");
    menu.innerHTML = "";
    var convs = data.conversations || [];
    var foundCurrent = false;
    convs.forEach(function(c) {{
      var btn = document.createElement("button");
      btn.className = "conv-picker-item";
      btn.textContent = c.name || c.conversation_id;
      if (c.conversation_id === conversationId) {{
        btn.classList.add("active");
        label.textContent = c.name || c.conversation_id;
        foundCurrent = true;
      }}
      btn.onclick = function() {{
        window.location.href = "chat?cid=" + encodeURIComponent(c.conversation_id);
      }};
      menu.appendChild(btn);
    }});
    if (!foundCurrent && conversationId === "NEW") {{
      label.textContent = "New conversation";
    }}
    // "New conversation" option at the bottom
    var newBtn = document.createElement("button");
    newBtn.className = "conv-picker-item new-conv";
    newBtn.textContent = "+ New conversation";
    newBtn.onclick = function() {{ window.location.href = "chat?cid=NEW"; }};
    menu.appendChild(newBtn);
  }}).catch(function(e) {{ console.error("Failed to load conversations:", e); }});
}}
loadConversations();

function scrollToBottom() {{
  var el = document.getElementById("messages");
  el.scrollTop = el.scrollHeight;
}}

function escapeHtml(text) {{
  var d = document.createElement("div");
  d.textContent = text;
  return d.innerHTML;
}}

function renderMarkdown(text) {{
  // Split into lines, group consecutive "> " lines into blockquotes
  var lines = text.split("\\n");
  var parts = [];
  var quoteLines = [];
  function flushQuote() {{
    if (quoteLines.length > 0) {{
      parts.push("<blockquote>" + quoteLines.join("<br>") + "</blockquote>");
      quoteLines = [];
    }}
  }}
  for (var i = 0; i < lines.length; i++) {{
    if (lines[i].substring(0, 2) === "> ") {{
      quoteLines.push(inlineMarkdown(escapeHtml(lines[i].substring(2))));
    }} else {{
      flushQuote();
      parts.push(inlineMarkdown(escapeHtml(lines[i])));
    }}
  }}
  flushQuote();
  return parts.join("<br>");
}}

function inlineMarkdown(html) {{
  // Links: [text](url)
  html = html.replace(/\\[([^\\]]+)\\]\\(([^)]+)\\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  // Bold: **text**
  html = html.replace(/\\*\\*([^*]+)\\*\\*/g, "<strong>$1</strong>");
  // Italic: *text*
  html = html.replace(/\\*([^*]+)\\*/g, "<em>$1</em>");
  return html;
}}

function appendMessage(role, content) {{
  var messages = document.getElementById("messages");
  var div = document.createElement("div");
  div.className = "message " + role;
  var bubble = document.createElement("div");
  bubble.className = "message-bubble";
  bubble.innerHTML = renderMarkdown(content);
  div.appendChild(bubble);
  messages.appendChild(div);
  scrollToBottom();
  return bubble;
}}

function loadHistory() {{
  fetch("api/chat/history?cid=" + encodeURIComponent(conversationId))
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      if (data.messages) {{
        for (var i = 0; i < data.messages.length; i++) {{
          appendMessage(data.messages[i].role, data.messages[i].content);
        }}
      }}
    }})
    .catch(function(e) {{ console.error("Failed to load history:", e); }});
}}

function sendMessage() {{
  var input = document.getElementById("chat-input");
  var message = input.value.trim();
  if (!message || isStreaming) return;

  appendMessage("user", message);
  input.value = "";
  input.style.height = "auto";

  isStreaming = true;
  var indicator = document.createElement("div");
  indicator.id = "streaming-indicator";
  indicator.className = "streaming-indicator";
  indicator.textContent = "Thinking...";
  document.getElementById("messages").appendChild(indicator);
  scrollToBottom();

  var currentBubble = null;
  var fullText = "";

  fetch("api/chat/send", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{conversation_id: conversationId, message: message}})
  }}).then(function(response) {{
    var reader = response.body.getReader();
    var decoder = new TextDecoder();
    var buffer = "";

    function processChunk(result) {{
      if (result.done) {{
        finishStreaming();
        return;
      }}
      buffer += decoder.decode(result.value, {{stream: true}});
      var lines = buffer.split("\\n");
      buffer = lines.pop();

      for (var i = 0; i < lines.length; i++) {{
        var line = lines[i];
        if (line.startsWith("event: ")) {{
          var eventType = line.substring(7).trim();
          // next line should be data:
          i++;
          if (i < lines.length && lines[i].startsWith("data: ")) {{
            var dataStr = lines[i].substring(6);
            try {{
              var data = JSON.parse(dataStr);
              if (eventType === "chunk") {{
                if (!currentBubble) {{
                  currentBubble = appendMessage("assistant", "");
                  // Re-append indicator so it stays below the streaming bubble
                  var ind = document.getElementById("streaming-indicator");
                  if (ind) document.getElementById("messages").appendChild(ind);
                }}
                fullText += data.chunk;
                currentBubble.innerHTML = renderMarkdown(fullText);
                scrollToBottom();
              }} else if (eventType === "done") {{
                if (data.conversation_id && data.conversation_id !== conversationId) {{
                  conversationId = data.conversation_id;
                  history.replaceState(null, "", "chat?cid=" + encodeURIComponent(conversationId));
                }}
              }} else if (eventType === "error") {{
                if (!currentBubble) {{
                  currentBubble = appendMessage("assistant", "");
                }}
                currentBubble.innerHTML = escapeHtml("Error: " + (data.error || "Unknown error"));
                currentBubble.style.color = "#c00";
              }}
            }} catch(e) {{
              console.error("Failed to parse SSE data:", e);
            }}
          }}
        }}
      }}
      return reader.read().then(processChunk);
    }}

    return reader.read().then(processChunk);
  }}).catch(function(e) {{
    console.error("Send failed:", e);
    appendMessage("assistant", "Error: Failed to send message");
    finishStreaming();
  }});

  function finishStreaming() {{
    isStreaming = false;
    var ind = document.getElementById("streaming-indicator");
    if (ind) ind.remove();
  }}
}}

// Auto-resize textarea
var textarea = document.getElementById("chat-input");
textarea.addEventListener("input", function() {{
  this.style.height = "auto";
  this.style.height = Math.min(this.scrollHeight, 120) + "px";
}});

// Send on Enter (Shift+Enter for newline)
textarea.addEventListener("keydown", function(e) {{
  if (e.key === "Enter" && !e.shiftKey) {{
    e.preventDefault();
    sendMessage();
  }}
}});

// Load history on page load
if (conversationId && conversationId !== "NEW") {{
  loadHistory();
}}
</script>
</body>
</html>"""


# -- HTTP Handler --


class _WebServerHandler(BaseHTTPRequestHandler):
    """Handles HTTP requests for the agent web interface."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        _log(format % args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        agent_name = _html_escape(AGENT_NAME or "Agent")

        if path == "/" or path == "/index.html":
            conversation_id = _get_most_recent_conversation_id()
            if conversation_id is not None:
                self._send_html(_render_web_chat_page(agent_name, conversation_id))
            else:
                self._send_html(_render_conversations_page())
        elif path == "/chat":
            conversation_id = (query.get("cid") or [""])[0]
            if not conversation_id:
                self._send_redirect("conversations")
            elif conversation_id == "NEW":
                new_cid = _create_greeting_conversation()
                if new_cid:
                    self._send_redirect(f"chat?cid={new_cid}")
                else:
                    self._send_html(_render_web_chat_page(agent_name, "NEW"))
            else:
                self._send_html(_render_web_chat_page(agent_name, conversation_id))
        elif path == "/text_chat":
            conversation_id = (query.get("cid") or [""])[0]
            if not conversation_id:
                self._send_redirect("conversations")
            else:
                self._send_html(
                    _render_iframe_page(
                        agent_name,
                        conversation_id,
                        f"../chat/?arg={conversation_id}",
                        active="conversations",
                    )
                )
        elif path == "/conversations":
            self._send_html(_render_conversations_page())
        elif path == "/terminal":
            self._send_html(_render_iframe_page(agent_name, "Terminal", "../agent/", active="terminal"))
        elif path == "/agents-page":
            self._send_html(_render_agents_page())
        elif path == "/fonts":
            try:
                font_html = Path("font_preview.html").read_text()
                self._send_html(font_html)
            except OSError:
                self.send_error(404, "font_preview.html not found in working directory")
        elif path == "/api/chat/history":
            conversation_id = (query.get("cid") or [""])[0]
            if not conversation_id:
                self._send_json({"error": "Missing cid parameter"}, status=400)
            else:
                messages = _read_message_history(conversation_id)
                self._send_json({"messages": messages, "conversation_id": conversation_id})
        elif path == "/api/conversations":
            conversations = _read_conversations()
            self._send_json({"conversations": conversations})
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/chat/send":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, status=400)
                return

            conversation_id = data.get("conversation_id", "")
            message = data.get("message", "")
            if not conversation_id or not message:
                self._send_json({"error": "Missing conversation_id or message"}, status=400)
                return

            # Start SSE streaming response.
            # Connection: close ensures the TCP connection is closed after the
            # handler finishes, signaling end-of-stream to the client.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            _handle_chat_send(conversation_id, message, self.wfile)
            self.close_connection = True
        else:
            self.send_error(404)

    def _send_html(self, content: str) -> None:
        encoded = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, data: dict[str, Any], status: int = 200) -> None:
        encoded = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()


# -- Main --


def main() -> None:
    global _is_shutting_down

    if not AGENT_STATE_DIR:
        _log("MNG_AGENT_STATE_DIR must be set")
        sys.exit(1)

    # Start background thread for agent list polling
    poll_thread = threading.Thread(target=_poll_agent_list_forever, daemon=True)
    poll_thread.start()

    # Start HTTP server. Use WEB_SERVER_PORT if set, otherwise pick a random port.
    requested_port = int(os.environ.get("WEB_SERVER_PORT", "0"))
    server = ThreadingHTTPServer(("127.0.0.1", requested_port), _WebServerHandler)
    port = server.server_address[1]

    _log(f"Listening on port {port}")

    # Register this web server in servers/events.jsonl
    _register_server(WEB_SERVER_NAME, port)

    # Handle shutdown signals.
    # server.shutdown() must be called from a different thread than
    # serve_forever() to avoid deadlock.
    def _shutdown_handler(signum: int, frame: object) -> None:
        global _is_shutting_down
        _is_shutting_down = True
        _log("Shutting down...")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    try:
        server.serve_forever()
    finally:
        _is_shutting_down = True


if __name__ == "__main__":
    main()
