#!/usr/bin/env python3
"""Web server for the ClaudeChangelingAgent web interface.

Serves a web interface where all views (conversations, terminal) are displayed
in iframes below a persistent navigation header:
- Main page: shows the most recent conversation in an iframe (or conversation list if none)
- Conversation view: embeds a specific conversation's ttyd in an iframe
- Conversations page: lists all conversations with links to open them in iframe views
- Terminal page: embeds the primary agent terminal in an iframe
- All Agents page: lists agents on this host with their states

The actual terminal sessions are handled by companion ttyd processes
(started as separate tmux windows with --url-arg):
- Chat ttyd: ?arg=<conversation_id> to resume, ?arg=NEW to create

Environment:
    MNG_AGENT_STATE_DIR  - Agent state directory (contains events/)
    MNG_HOST_DIR         - Host data directory (contains commands/)
    MNG_AGENT_NAME       - This agent's name
    MNG_HOST_NAME        - Name of the host this agent runs on
"""

import hashlib
import html
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from datetime import timezone
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Final
from urllib.parse import parse_qs
from urllib.parse import urlparse

# -- Environment and paths --

AGENT_STATE_DIR: Final[str] = os.environ.get("MNG_AGENT_STATE_DIR", "")
HOST_DIR: Final[str] = os.environ.get("MNG_HOST_DIR", "")
AGENT_NAME: Final[str] = os.environ.get("MNG_AGENT_NAME", "")
HOST_NAME: Final[str] = os.environ.get("MNG_HOST_NAME", "")

SERVERS_JSONL_PATH: Final[Path | None] = (
    Path(AGENT_STATE_DIR) / "events" / "servers" / "events.jsonl" if AGENT_STATE_DIR else None
)
MESSAGES_EVENTS_PATH: Final[Path | None] = (
    Path(AGENT_STATE_DIR) / "events" / "messages" / "events.jsonl" if AGENT_STATE_DIR else None
)
CONVERSATIONS_EVENTS_PATH: Final[Path | None] = (
    Path(AGENT_STATE_DIR) / "events" / "conversations" / "events.jsonl" if AGENT_STATE_DIR else None
)

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
    """Read conversations from event logs and return sorted by most recent activity."""
    conversations_by_id: dict[str, dict[str, str]] = {}

    # Read conversation creation events
    if CONVERSATIONS_EVENTS_PATH and CONVERSATIONS_EVENTS_PATH.exists():
        for line in CONVERSATIONS_EVENTS_PATH.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                conversation_id = event.get("conversation_id", "")
                if conversation_id:
                    conversations_by_id[conversation_id] = {
                        "conversation_id": conversation_id,
                        "model": event.get("model", "unknown"),
                        "created_at": event.get("timestamp", ""),
                        "updated_at": event.get("timestamp", ""),
                    }
            except json.JSONDecodeError as e:
                _log(f"Skipping malformed conversation event line: {e}")
                continue

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
                ["uv", "run", "mng", "list", "--json", "--quiet"],
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
        except FileNotFoundError:
            _log("uv not found in PATH, cannot poll agent list")
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
      padding: 12px 16px; background: white; border: 1px solid #ddd;
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
        model = _html_escape(conv.get("model", ""))
        updated = _html_escape(conv.get("updated_at", ""))
        detail = model
        if updated:
            detail += f" -- {updated}"
        conv_items += (
            f'<li class="item">'
            f'<div class="item-info">'
            f'<span class="item-name">{conversation_id}</span>'
            f'<span class="item-detail">{detail}</span>'
            f"</div>"
            f'<a class="link-btn" href="chat?cid={conversation_id}">Open</a>'
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
                self._send_html(
                    _render_iframe_page(
                        agent_name,
                        conversation_id,
                        f"../chat/?arg={conversation_id}",
                        active="conversations",
                    )
                )
            else:
                self._send_html(_render_conversations_page())
        elif path == "/chat":
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
        else:
            self.send_error(404)

    def _send_html(self, content: str) -> None:
        encoded = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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
