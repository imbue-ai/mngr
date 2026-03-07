#!/usr/bin/env python3
"""Web server for the ClaudeChangelingAgent web interface.

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
    MNG_AGENT_WORK_DIR   - Agent work directory (contains changelings.toml)
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

from imbue.mng_claude_changeling.resources.watcher_common import MngNotInstalledError
from imbue.mng_claude_changeling.resources.watcher_common import get_mng_command

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
    """Read conversations from the changeling_conversations table and return sorted by most recent activity."""
    conversations_by_id: dict[str, dict[str, str]] = {}

    # Read conversations from the llm database
    if LLM_DB_PATH and LLM_DB_PATH.is_file():
        try:
            conn = sqlite3.connect(f"file:{LLM_DB_PATH}?mode=ro", uri=True)
            try:
                rows = conn.execute(
                    "SELECT cc.conversation_id, c.model, cc.created_at, cc.tags "
                    "FROM changeling_conversations cc "
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
                _log(f"Failed to query changeling_conversations: {e}")
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
    """Background thread: periodically run mng list --json and cache results."""
    global _cached_agents
    while not _is_shutting_down:
        try:
            result = subprocess.run(
                [*get_mng_command(), "list", "--json", "--quiet"],
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
    html, body { height: 100%; font-family: system-ui, -apple-system, sans-serif; background: whitesmoke; }
    .header {
      display: flex; align-items: center; gap: 12px;
      padding: 8px 16px; background: rgb(26, 26, 46); color: white; height: 48px;
    }
    .header h1 { font-size: 16px; font-weight: 600; }
    .header-spacer { flex: 1; }
    .header a {
      color: rgba(255,255,255,0.8); text-decoration: none; font-size: 14px;
      padding: 4px 12px; border-radius: 4px; border: 1px solid rgba(255,255,255,0.2);
    }
    .header a:hover { background: rgba(255,255,255,0.1); color: white; }
    .header a.active { background: rgba(255,255,255,0.15); color: white; border-color: rgba(255,255,255,0.5); }
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
      display: inline-block; padding: 6px 14px; background: rgb(26, 26, 46);
      color: white; text-decoration: none; border-radius: 4px; font-size: 14px;
    }
    .link-btn:hover { background: rgb(42, 42, 78); }
    .link-btn.disabled { opacity: 0.5; pointer-events: none; }
    .link-btn.new { background: rgb(34, 120, 60); }
    .link-btn.new:hover { background: rgb(40, 150, 70); }
    .empty-state { color: #666; font-size: 15px; margin-top: 16px; }
"""


def _render_header(agent_name: str, active: str = "") -> str:
    """Render the common header bar with navigation links."""

    def _nav_link(href: str, label: str, key: str) -> str:
        cls = ' class="active"' if key == active else ""
        return f'<a{cls} href="{href}">{label}</a>'

    return (
        '<div class="header">'
        f"<h1>{agent_name}</h1>"
        '<div class="header-spacer"></div>'
        + _nav_link("conversations", "Conversations", "conversations")
        + _nav_link("terminal", "Terminal", "terminal")
        + _nav_link("agents-page", "Agents", "agents")
        + "</div>"
    )


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
    """Read the default chat model from changelings.toml, falling back to claude-opus-4.6."""
    if not AGENT_WORK_DIR:
        return "claude-opus-4.6"
    settings_path = Path(AGENT_WORK_DIR) / "changelings.toml"
    try:
        if settings_path.exists():
            raw = tomllib.loads(settings_path.read_text())
            model = raw.get("chat", {}).get("model")
            if model:
                return str(model)
    except (OSError, tomllib.TOMLDecodeError) as e:
        _log(f"Failed to load chat model from settings: {e}")
    return "claude-opus-4.6"


def _get_system_prompt() -> str:
    """Build the system prompt from GLOBAL.md and talking/PROMPT.md."""
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
    return "\n\n".join(parts)


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


def _create_new_conversation() -> str:
    """Create a new conversation and register it in the changeling_conversations table."""
    conversation_id = f"conv-{int(time.time())}-{os.urandom(4).hex()}"
    created_at = _iso_timestamp()
    if LLM_DB_PATH:
        try:
            conn = sqlite3.connect(str(LLM_DB_PATH))
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS changeling_conversations ("
                    "conversation_id TEXT PRIMARY KEY, "
                    "tags TEXT NOT NULL DEFAULT '{}', "
                    "created_at TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO changeling_conversations "
                    "(conversation_id, tags, created_at) VALUES (?, ?, ?)",
                    (conversation_id, '{"name":"(new chat)"}', created_at),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as e:
            _log(f"Failed to create conversation record: {e}")
    return conversation_id


def _handle_chat_send(conversation_id: str, message: str, wfile: Any) -> None:
    """Send a message to the LLM and stream the response via SSE.

    Runs ``llm prompt`` as a subprocess with ``--cid`` to continue the conversation.
    The full response is sent as a single SSE "done" event (subprocess output is
    not line-buffered, so true streaming requires the llm library directly -- this
    approach keeps the implementation simple and avoids runtime dependencies).
    """
    model_id = _get_default_chat_model()

    # Build the llm command
    cmd = ["llm", "prompt", "-m", model_id, "--cid", conversation_id]

    system_prompt = _get_system_prompt()
    if system_prompt:
        cmd.extend(["-s", system_prompt])

    cmd.append(message)

    env = os.environ.copy()
    if _LLM_USER_PATH:
        env["LLM_USER_PATH"] = _LLM_USER_PATH

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=300)
    except FileNotFoundError:
        error_data = json.dumps({"error": "llm command not found"})
        wfile.write(f"event: error\ndata: {error_data}\n\n".encode())
        wfile.flush()
        return
    except subprocess.TimeoutExpired:
        error_data = json.dumps({"error": "LLM request timed out"})
        wfile.write(f"event: error\ndata: {error_data}\n\n".encode())
        wfile.flush()
        return

    if result.returncode != 0:
        _log(f"llm prompt failed (exit {result.returncode}): {result.stderr[:200]}")
        error_data = json.dumps({"error": f"LLM failed: {result.stderr[:200]}"})
        wfile.write(f"event: error\ndata: {error_data}\n\n".encode())
        wfile.flush()
        return

    full_text = result.stdout

    # Send the response as a single chunk followed by done
    if full_text:
        chunk_data = json.dumps({"chunk": full_text})
        wfile.write(f"event: chunk\ndata: {chunk_data}\n\n".encode())
        wfile.flush()

    done_data = json.dumps({"conversation_id": conversation_id, "full_text": full_text})
    wfile.write(f"event: done\ndata: {done_data}\n\n".encode())
    wfile.flush()


# -- Web chat page rendering --


_CHAT_CSS: Final[str] = """
    * { margin: 0; padding: 0; box-sizing: border-box; }
    html, body { height: 100%; font-family: system-ui, -apple-system, sans-serif; background: rgb(245, 245, 245); }
    .chat-layout { display: flex; flex-direction: column; height: 100%; }
    .chat-messages {
      flex: 1; overflow-y: auto; padding: 16px; max-width: 800px;
      margin: 0 auto; width: 100%;
    }
    .message { margin-bottom: 16px; display: flex; flex-direction: column; }
    .message.user { align-items: flex-end; }
    .message.assistant { align-items: flex-start; }
    .message-bubble {
      max-width: 80%; padding: 10px 14px; border-radius: 12px;
      font-size: 14px; line-height: 1.5; white-space: pre-wrap; word-wrap: break-word;
    }
    .message.user .message-bubble {
      background: rgb(26, 26, 46); color: white; border-bottom-right-radius: 4px;
    }
    .message.assistant .message-bubble {
      background: white; color: rgb(51, 51, 51); border: 1px solid rgb(221, 221, 221); border-bottom-left-radius: 4px;
    }
    .message-label { font-size: 11px; color: rgb(153, 153, 153); margin-bottom: 2px; padding: 0 4px; }
    .chat-input-area {
      border-top: 1px solid rgb(221, 221, 221); background: white; padding: 12px 16px;
    }
    .chat-input-container {
      max-width: 800px; margin: 0 auto; display: flex; gap: 8px; align-items: flex-end;
    }
    .chat-input-container textarea {
      flex: 1; padding: 10px 14px; border: 1px solid rgb(221, 221, 221); border-radius: 8px;
      font-size: 14px; font-family: inherit; resize: none; outline: none;
      min-height: 44px; max-height: 120px; line-height: 1.4;
    }
    .chat-input-container textarea:focus { border-color: rgb(26, 26, 46); }
    .chat-input-container button {
      padding: 10px 20px; background: rgb(26, 26, 46); color: white;
      border: none; border-radius: 8px; font-size: 14px; cursor: pointer;
      white-space: nowrap;
    }
    .chat-input-container button:hover { background: rgb(42, 42, 78); }
    .chat-input-container button:disabled { opacity: 0.5; cursor: not-allowed; }
    .streaming-indicator { font-size: 12px; color: rgb(153, 153, 153); padding: 4px 0; text-align: center; }
"""


def _render_web_chat_page(agent_name: str, conversation_id: str) -> str:
    """Render the web-based chat page with SSE streaming support."""
    escaped_agent = _html_escape(agent_name)
    # Use json.dumps for safe embedding in JavaScript string context
    # (html.escape is insufficient inside <script> tags).
    # Also escape </ to prevent premature script tag closing.
    js_safe_cid = json.dumps(conversation_id).replace("</", r"<\/")

    return f"""<!DOCTYPE html>
<html>
<head>
<title>Chat - {escaped_agent}</title>
<style>
{_CSS}
{_CHAT_CSS}
</style>
</head>
<body class="chat-layout">
  {_render_header(agent_name, active="conversations")}
  <div class="chat-messages" id="messages"></div>
  <div id="streaming-indicator" class="streaming-indicator" style="display:none;">Thinking...</div>
  <div class="chat-input-area">
    <div class="chat-input-container">
      <textarea id="chat-input" placeholder="Type a message..." rows="1"></textarea>
      <button id="send-btn" onclick="sendMessage()">Send</button>
    </div>
  </div>
<script>
var conversationId = {js_safe_cid};
var isStreaming = false;

function scrollToBottom() {{
  var el = document.getElementById("messages");
  el.scrollTop = el.scrollHeight;
}}

function appendMessage(role, content) {{
  var messages = document.getElementById("messages");
  var div = document.createElement("div");
  div.className = "message " + role;
  var label = document.createElement("div");
  label.className = "message-label";
  label.textContent = role === "user" ? "You" : "Assistant";
  var bubble = document.createElement("div");
  bubble.className = "message-bubble";
  bubble.textContent = content;
  div.appendChild(label);
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
  document.getElementById("send-btn").disabled = true;
  document.getElementById("streaming-indicator").style.display = "block";

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
                }}
                fullText += data.chunk;
                currentBubble.textContent = fullText;
                scrollToBottom();
              }} else if (eventType === "done") {{
                if (data.conversation_id) {{
                  conversationId = data.conversation_id;
                }}
              }} else if (eventType === "error") {{
                if (!currentBubble) {{
                  currentBubble = appendMessage("assistant", "");
                }}
                currentBubble.textContent = "Error: " + (data.error || "Unknown error");
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
    document.getElementById("send-btn").disabled = false;
    document.getElementById("streaming-indicator").style.display = "none";
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
                new_cid = _create_new_conversation()
                self._send_redirect(f"chat?cid={new_cid}")
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
        elif path == "/api/chat/history":
            conversation_id = (query.get("cid") or [""])[0]
            if not conversation_id:
                self._send_json({"error": "Missing cid parameter"}, status=400)
            else:
                messages = _read_message_history(conversation_id)
                self._send_json({"messages": messages, "conversation_id": conversation_id})
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

            # If conversation_id is "NEW", create a new one
            if conversation_id == "NEW":
                conversation_id = _create_new_conversation()

            # Start SSE streaming response
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            _handle_chat_send(conversation_id, message, self.wfile)
        elif path == "/api/chat/new":
            new_cid = _create_new_conversation()
            self._send_json({"conversation_id": new_cid})
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

    # Start HTTP server on a random port
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WebServerHandler)
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
