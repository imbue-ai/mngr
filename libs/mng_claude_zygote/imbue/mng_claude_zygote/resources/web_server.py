#!/usr/bin/env python3
"""Web server for the ClaudeZygoteAgent web interface.

Serves a web interface with:
- Main page: conversation selector dropdown + iframe for the active conversation
- All Agents page: list of agents on this host with links to connect

The actual terminal sessions are handled by companion ttyd processes (started
as separate tmux windows with --url-arg):
- Chat ttyd: accessed via ?arg=<conversation_id> to resume a conversation
- Agent-tmux ttyd: accessed via ?arg=<agent_name> to attach to an agent's tmux

Environment:
    MNG_AGENT_STATE_DIR  - Agent state directory (contains logs/)
    MNG_HOST_DIR         - Host data directory (contains commands/)
    MNG_AGENT_NAME       - This agent's name
    MNG_HOST_NAME        - Name of the host this agent runs on
"""

import html
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Final
from urllib.parse import urlparse

# -- Environment and paths --

AGENT_STATE_DIR: Final[str] = os.environ.get("MNG_AGENT_STATE_DIR", "")
HOST_DIR: Final[str] = os.environ.get("MNG_HOST_DIR", "")
AGENT_NAME: Final[str] = os.environ.get("MNG_AGENT_NAME", "")
HOST_NAME: Final[str] = os.environ.get("MNG_HOST_NAME", "")

SERVERS_JSONL_PATH: Final[Path | None] = Path(AGENT_STATE_DIR) / "logs" / "servers.jsonl" if AGENT_STATE_DIR else None
MESSAGES_EVENTS_PATH: Final[Path | None] = (
    Path(AGENT_STATE_DIR) / "logs" / "messages" / "events.jsonl" if AGENT_STATE_DIR else None
)
CONVERSATIONS_EVENTS_PATH: Final[Path | None] = (
    Path(AGENT_STATE_DIR) / "logs" / "conversations" / "events.jsonl" if AGENT_STATE_DIR else None
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


def _register_server(server_name: str, port: int) -> None:
    """Append a server record to servers.jsonl."""
    if SERVERS_JSONL_PATH is None:
        return
    SERVERS_JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = json.dumps({"server": server_name, "url": f"http://127.0.0.1:{port}"})
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
                cid = event.get("conversation_id", "")
                if cid:
                    conversations_by_id[cid] = {
                        "conversation_id": cid,
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
                msg = json.loads(line)
                cid = msg.get("conversation_id", "")
                ts = msg.get("timestamp", "")
                if cid and ts and cid in conversations_by_id:
                    if ts > conversations_by_id[cid]["updated_at"]:
                        conversations_by_id[cid]["updated_at"] = ts
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


# -- HTML Templates --
#
# The main page shows a conversation selector with an iframe that loads the
# chat ttyd via ../chat/?arg=<cid>. The agents page lists all agents on
# this host with links to ../agent-tmux/?arg=<name>.

_MAIN_PAGE_HTML: Final[str] = """<!DOCTYPE html>
<html>
<head>
  <title>{agent_name}</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    html, body {{ height: 100%; overflow: hidden; font-family: system-ui, -apple-system, sans-serif; }}

    .header {{
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 8px 16px;
      background: rgb(26, 26, 46);
      color: white;
      height: 48px;
    }}

    .header-title {{
      font-weight: 600;
      font-size: 15px;
      margin-right: 8px;
      white-space: nowrap;
    }}

    .header select {{
      padding: 4px 8px;
      border-radius: 4px;
      border: 1px solid rgba(255,255,255,0.2);
      background: rgba(255,255,255,0.1);
      color: white;
      font-size: 14px;
      max-width: 400px;
      cursor: pointer;
    }}

    .header select option {{
      background: rgb(26, 26, 46);
      color: white;
    }}

    .header button {{
      padding: 4px 12px;
      border-radius: 4px;
      border: 1px solid rgba(255,255,255,0.3);
      background: rgba(255,255,255,0.15);
      color: white;
      font-size: 14px;
      cursor: pointer;
    }}
    .header button:hover {{ background: rgba(255,255,255,0.25); }}

    .header-spacer {{ flex: 1; }}

    .header a {{
      color: rgba(255,255,255,0.8);
      text-decoration: none;
      font-size: 14px;
      padding: 4px 12px;
      border-radius: 4px;
      border: 1px solid rgba(255,255,255,0.2);
    }}
    .header a:hover {{ background: rgba(255,255,255,0.1); color: white; }}

    .status-area {{
      display: flex;
      align-items: center;
      justify-content: center;
      height: calc(100% - 48px);
      color: #666;
      font-size: 16px;
    }}

    .empty-state {{
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      height: calc(100% - 48px);
      color: #666;
      font-size: 16px;
      gap: 16px;
    }}

    .empty-state button {{
      padding: 8px 20px;
      border-radius: 6px;
      border: none;
      background: rgb(26, 26, 46);
      color: white;
      font-size: 15px;
      cursor: pointer;
    }}
    .empty-state button:hover {{ background: rgb(42, 42, 78); }}

    #conv-frame {{
      width: 100%;
      height: calc(100% - 48px);
      border: none;
    }}
  </style>
</head>
<body>
  <div class="header">
    <span class="header-title">{agent_name}</span>
    <select id="conv-select" onchange="onConversationSelected()">
      <option value="">Loading...</option>
    </select>
    <button onclick="onNewConversation()">+ New</button>
    <div class="header-spacer"></div>
    <a href="agents-page">All Agents</a>
  </div>

  <div id="status" class="status-area">Loading conversations...</div>
  <div id="empty" class="empty-state" style="display:none">
    <p>No conversations yet.</p>
    <button onclick="onNewConversation()">Start a conversation</button>
  </div>
  <iframe id="conv-frame" style="display:none"></iframe>

  <script>
    function showStatus(msg) {{
      document.getElementById('status').textContent = msg;
      document.getElementById('status').style.display = 'flex';
      document.getElementById('empty').style.display = 'none';
      document.getElementById('conv-frame').style.display = 'none';
    }}

    function showEmpty() {{
      document.getElementById('status').style.display = 'none';
      document.getElementById('empty').style.display = 'flex';
      document.getElementById('conv-frame').style.display = 'none';
    }}

    function showFrame(src) {{
      var frame = document.getElementById('conv-frame');
      frame.src = src;
      frame.style.display = 'block';
      document.getElementById('status').style.display = 'none';
      document.getElementById('empty').style.display = 'none';
    }}

    async function loadConversations() {{
      try {{
        var resp = await fetch('api/conversations');
        var conversations = await resp.json();
        var select = document.getElementById('conv-select');
        var previousValue = select.value;
        select.innerHTML = '';

        if (conversations.length === 0) {{
          showEmpty();
          return;
        }}

        conversations.forEach(function(conv) {{
          var opt = document.createElement('option');
          opt.value = conv.conversation_id;
          var updated = conv.updated_at || '';
          var label = conv.conversation_id;
          if (updated) {{
            try {{ label += ' (' + new Date(updated).toLocaleString() + ')'; }}
            catch(e) {{ label += ' (' + updated + ')'; }}
          }}
          opt.textContent = label;
          select.appendChild(opt);
        }});

        // Restore previous selection if it still exists, otherwise select first
        if (previousValue && Array.from(select.options).some(function(o) {{ return o.value === previousValue; }})) {{
          select.value = previousValue;
        }} else {{
          select.selectedIndex = 0;
          onConversationSelected();
        }}
      }} catch (e) {{
        console.error('Failed to load conversations:', e);
        showStatus('Failed to load conversations. Retrying...');
        setTimeout(loadConversations, 3000);
      }}
    }}

    function onConversationSelected() {{
      var select = document.getElementById('conv-select');
      var cid = select.value;
      if (!cid) return;
      // Load the chat ttyd with the conversation id as a URL arg
      showFrame('../chat/?arg=' + encodeURIComponent(cid));
    }}

    function onNewConversation() {{
      // Load the chat ttyd with arg=NEW (starts a new conversation)
      showFrame('../chat/?arg=NEW');
      // Refresh the conversation list after a delay to pick up the new one
      setTimeout(loadConversations, 5000);
    }}

    // Periodically refresh the conversation list
    setInterval(loadConversations, 15000);

    // Initial load
    loadConversations();
  </script>
</body>
</html>"""


_AGENTS_PAGE_HTML: Final[str] = """<!DOCTYPE html>
<html>
<head>
  <title>All Agents - {agent_name}</title>
  <style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: system-ui, -apple-system, sans-serif; background: whitesmoke; }}

    .header {{
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 8px 16px;
      background: rgb(26, 26, 46);
      color: white;
      height: 48px;
    }}

    .header h1 {{ font-size: 16px; font-weight: 600; }}
    .header-spacer {{ flex: 1; }}
    .header a {{
      color: rgba(255,255,255,0.8);
      text-decoration: none;
      font-size: 14px;
      padding: 4px 12px;
      border-radius: 4px;
      border: 1px solid rgba(255,255,255,0.2);
    }}
    .header a:hover {{ background: rgba(255,255,255,0.1); color: white; }}

    .content {{ padding: 24px; max-width: 800px; }}

    .agent-list {{ list-style: none; margin-top: 16px; }}
    .agent-item {{
      padding: 12px 16px;
      background: white;
      border: 1px solid #ddd;
      border-radius: 6px;
      margin-bottom: 8px;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }}
    .agent-info {{ display: flex; align-items: center; gap: 8px; }}
    .agent-name {{ font-weight: 600; font-size: 15px; }}
    .agent-state {{
      font-size: 13px;
      padding: 2px 8px;
      border-radius: 4px;
      background: #e8e8e8;
    }}
    .agent-state.running {{ background: #d4edda; color: #155724; }}
    .agent-state.stopped {{ background: #f8d7da; color: #721c24; }}
    .agent-state.waiting {{ background: #fff3cd; color: #856404; }}
    .agent-state.done {{ background: #cce5ff; color: #004085; }}
    .agent-connect {{
      display: inline-block;
      padding: 6px 14px;
      background: rgb(26, 26, 46);
      color: white;
      text-decoration: none;
      border-radius: 4px;
      font-size: 14px;
    }}
    .agent-connect:hover {{ background: rgb(42, 42, 78); }}
    .agent-connect.disabled {{ opacity: 0.5; pointer-events: none; }}
    .empty-state {{ color: #666; font-size: 15px; margin-top: 16px; }}
    .loading {{ color: #666; font-size: 15px; margin-top: 16px; }}
  </style>
</head>
<body>
  <div class="header">
    <h1>All Agents</h1>
    <div class="header-spacer"></div>
    <a href="./">Back to Conversations</a>
  </div>

  <div class="content">
    <div id="loading" class="loading">Loading agents...</div>
    <ul id="agent-list" class="agent-list" style="display:none"></ul>
    <div id="empty" class="empty-state" style="display:none">No agents found on this host.</div>
  </div>

  <script>
    async function loadAgents() {{
      try {{
        var resp = await fetch('api/agents');
        var agents = await resp.json();

        document.getElementById('loading').style.display = 'none';

        if (agents.length === 0) {{
          document.getElementById('empty').style.display = 'block';
          document.getElementById('agent-list').style.display = 'none';
          return;
        }}

        var list = document.getElementById('agent-list');
        list.innerHTML = '';
        list.style.display = 'block';

        agents.forEach(function(agent) {{
          var li = document.createElement('li');
          li.className = 'agent-item';

          var info = document.createElement('div');
          info.className = 'agent-info';

          var name = document.createElement('span');
          name.className = 'agent-name';
          name.textContent = agent.name || 'unnamed';
          info.appendChild(name);

          var state = (agent.state || 'unknown').toLowerCase();
          var stateSpan = document.createElement('span');
          stateSpan.className = 'agent-state ' + state;
          stateSpan.textContent = state.toUpperCase();
          info.appendChild(stateSpan);

          li.appendChild(info);

          var link = document.createElement('a');
          link.className = 'agent-connect';
          link.textContent = 'Connect';
          if (state === 'running' || state === 'waiting') {{
            // Link to the agent-tmux ttyd with the agent name as a URL arg
            link.href = '../agent-tmux/?arg=' + encodeURIComponent(agent.name);
          }} else {{
            link.className += ' disabled';
            link.title = 'Agent is not running';
          }}
          li.appendChild(link);

          list.appendChild(li);
        }});
      }} catch (e) {{
        document.getElementById('loading').textContent = 'Failed to load agents. Retrying...';
        setTimeout(loadAgents, 5000);
      }}
    }}

    loadAgents();
    setInterval(loadAgents, 30000);
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

        if path == "/" or path == "/index.html":
            self._serve_main_page()
        elif path == "/agents-page":
            self._serve_agents_page()
        elif path == "/api/conversations":
            self._serve_json(_read_conversations())
        elif path == "/api/agents":
            with _agent_list_lock:
                self._serve_json(list(_cached_agents))
        else:
            self.send_error(404)

    def _serve_main_page(self) -> None:
        page_html = _MAIN_PAGE_HTML.format(agent_name=_html_escape(AGENT_NAME or "Agent"))
        self._send_html(page_html)

    def _serve_agents_page(self) -> None:
        page_html = _AGENTS_PAGE_HTML.format(agent_name=_html_escape(AGENT_NAME or "Agent"))
        self._send_html(page_html)

    def _send_html(self, content: str) -> None:
        encoded = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _serve_json(self, data: object) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


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

    # Register this web server in servers.jsonl
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
